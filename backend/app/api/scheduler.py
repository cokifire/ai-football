import asyncio
import queue
import sys
import io
import re
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from loguru import logger

from app.db.session import get_db
from app.services.scheduler import (
    get_scheduler_status, stop_scheduled_task, start_scheduled_task,
    update_task, _update_db, _prepare_task_run, run_task,
)
from app.services.league_service import sync_leagues
from app.services.team_service import sync_teams
from app.services.player_service import sync_players
from app.services.standing_service import sync_standings
from app.services.fixture_service import sync_fixtures, sync_live_fixtures
from app.services.prediction_result_service import backfill_results
from app.services.auto_predict_service import auto_predict
from app.core.security import AdminAuth, ReadAuth, require_admin
from app.services.api_status_service import (
    get_status as get_api_status,
    reset_provider as reset_api_status,
)

router = APIRouter()

# ──────────── 实时日志流 ────────────

_task_queues: dict[str, queue.Queue] = {}


def _ensure_queue(task_id: str) -> queue.Queue:
    """获取或创建任务的 SSE 消息队列"""
    if task_id not in _task_queues:
        _task_queues[task_id] = queue.Queue()
    return _task_queues[task_id]


def push_task_message(task_id: str, msg: str):
    """向任务消息队列推送一行消息（线程安全）"""
    _ensure_queue(task_id).put(msg)


class _TaskLogSink:
    """loguru sink：将任务期间的日志消息推送到队列"""

    def __init__(self, task_id: str):
        self.task_id = task_id

    def write(self, message: str):
        msg = message.strip()
        if msg:
            push_task_message(self.task_id, msg)


class _StdoutWriter(io.TextIOBase):
    """捕获 print() 输出到任务队列"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._buffer = ""

    def write(self, s: str) -> int:
        self._buffer += s
        if "\n" in self._buffer:
            lines = self._buffer.split("\n")
            self._buffer = lines.pop()
            for line in lines:
                if line.strip():
                    push_task_message(self.task_id, line.strip())
        return len(s)

    def flush(self):
        if self._buffer.strip():
            push_task_message(self.task_id, self._buffer.strip())
            self._buffer = ""

TASK_MAP = {
    "league_sync":   ("联赛数据同步", sync_leagues),
    "team_sync":     ("球队数据同步", sync_teams),
    "player_sync":   ("球员数据同步", sync_players),
    "standing_sync": ("积分榜数据同步", sync_standings),
    "fixture_daily": ("赛程每日同步", sync_fixtures),
    "fixture_live":  ("赛程实时同步", sync_live_fixtures),
    "backfill_pred": ("预测结果回填", backfill_results),
    "auto_predict":  ("赛前自动预测", auto_predict),
}


def _run_task_with_stream(task_id: str, task_name: str, sync_fn):
    """在日志捕获下执行任务，所有 loguru + print 输出推送到 SSE 队列"""
    from app.db.session import SessionLocal

    # 与定时任务共用 is_running 占位：任务正在跑则直接放弃。否则手动触发会和
    # 08:30 的定时同步并发跑第二遍 sync_fixtures，重复拉子数据且 _do_fetch 的
    # "已存在则跳过"检查与插入之间存在竞态。
    #
    # 必须复用 ``_prepare_task_run`` 而不是直接调 ``_claim_task``：它额外负责复位
    # API 额度闩锁。手动触发曾经漏掉复位，导致当天标记过"额度耗尽"后，之后所有
    # 手动触发都被陈旧的 ``api_status`` 状态拦住（控制面板显示额度仍有剩余）。
    if not _prepare_task_run(task_id, task_name):
        push_task_message(task_id, f"⚠ {task_name} 已在运行中，本次手动触发已跳过")
        push_task_message(task_id, "__DONE__")
        return

    # 捕获 loguru 日志
    sink = _TaskLogSink(task_id)
    fmt = "{time:HH:mm:ss} | {level: <8} | {message}"
    handler_id = logger.add(sink, format=fmt, level="INFO", colorize=False)

    # 捕获 stdout (print)
    stdout_writer = _StdoutWriter(task_id)

    try:
        push_task_message(task_id, f"▶ 开始执行: {task_name}")

        old_stdout = sys.stdout
        sys.stdout = stdout_writer  # type: ignore[assignment]
        try:
            db = SessionLocal()
            try:
                sync_fn(db)
            finally:
                db.close()
        finally:
            sys.stdout = old_stdout

        push_task_message(task_id, f"✓ {task_name} 执行完成")
    except Exception as e:
        push_task_message(task_id, f"✗ {task_name} 执行失败: {e}")
    finally:
        logger.remove(handler_id)
        _update_db(task_id, is_running=0)
        push_task_message(task_id, "__DONE__")


@router.get("/scheduler/status")
async def scheduler_status(_: ReadAuth):
    return await asyncio.to_thread(get_scheduler_status)


# ──────────── API-Football 额度状态 (api_status 表) ────────────
@router.get("/api-status/{provider}")
async def api_status_get(provider: str, _: ReadAuth):
    """读取某个 provider 的 ``api_status``（如 ``api-football``）。"""
    return await asyncio.to_thread(get_api_status, provider)


@router.post("/api-status/{provider}/reset")
async def api_status_reset(provider: str, _: AdminAuth):
    """管理员手动复位: 强制 ``quota_exhausted=0``, 用于"今天日期还没变但
    升级了套餐/换了 key"这类场景。"""
    await asyncio.to_thread(reset_api_status, provider)
    return {"status": "ok", "message": f"{provider} 额度状态已复位"}


# ──────────── 按条件回填缺数据比赛 (backfill_fixtures_by_id.py) ────────────
# 以子进程方式跑脚本, 逐行读取 stdout 推到与定时任务共用的 SSE 队列, 前端
# 直接复用 GET /scheduler/{task_id}/stream 就能看到实时日志。
BACKFILL_TASK_ID = "backfill_fixtures"
BACKFILL_SCRIPT = "tools/backfill_fixtures_by_id.py"
# app/api/scheduler.py -> app/api -> app -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]

_backfill_running = False
_backfill_proc = None


class BackfillFixturesRequest(BaseModel):
    """按月份/联赛回填缺统计数据的比赛。

    month: YYYY-MM 或 None(不限月份)
    leagues: 联赛 id 列表, None/空 = 不限联赛
    sleep: 每次请求间隔秒数
    max_requests: 硬性请求上限, 0 = 不限制(仅靠额度停止)
    """

    month: str | None = None
    leagues: list[int] | None = None
    sleep: float = 4.0
    max_requests: int = 0


def _build_backfill_cmd(body: BackfillFixturesRequest) -> list[str]:
    """组装回填脚本命令行。一律走 argv 列表(不经 shell), 入参已先校验。"""
    cmd = [sys.executable, BACKFILL_SCRIPT]
    if body.month:
        cmd += ["--month", body.month]
    if body.leagues:
        cmd += ["--leagues", ",".join(str(x) for x in body.leagues)]
    cmd += ["--sleep", str(body.sleep)]
    if body.max_requests:
        cmd += ["--max-requests", str(body.max_requests)]
    cmd += ["--with-sub-data"]
    return cmd


def _run_backfill_with_stream(cmd: list[str]) -> None:
    """后台执行回填脚本，并把输出逐行推到 SSE 队列。"""
    global _backfill_running, _backfill_proc
    import subprocess
    try:
        push_task_message(BACKFILL_TASK_ID, f"▶ 启动: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, cwd=str(BACKEND_ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        _backfill_proc = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                push_task_message(BACKFILL_TASK_ID, line)
        proc.wait()
        code = proc.returncode
        if code == 0:
            push_task_message(BACKFILL_TASK_ID, "✓ 回填脚本执行完成")
        elif code == -15:  # SIGTERM：用户点击了「停止」
            push_task_message(BACKFILL_TASK_ID, "⏹ 回填进程已停止")
        else:
            push_task_message(BACKFILL_TASK_ID, f"✗ 回填脚本退出码 {code}")
    except Exception as e:
        push_task_message(BACKFILL_TASK_ID, f"✗ 启动失败: {e}")
    finally:
        _backfill_running = False
        _backfill_proc = None
        push_task_message(BACKFILL_TASK_ID, "__DONE__")


@router.post("/scheduler/backfill_fixtures/trigger")
async def trigger_backfill_fixtures(
    body: BackfillFixturesRequest,
    background_tasks: BackgroundTasks,
    _: AdminAuth,
):
    """按月份/联赛回填已完赛但缺统计数据的比赛。"""
    global _backfill_running
    if _backfill_running:
        raise HTTPException(status_code=409, detail="回填脚本正在运行中，请等待其结束")

    # 入参严格校验兜底: 前端只给下拉框, 这里再挡一层, 防止拼出危险命令行
    if body.month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", body.month):
        raise HTTPException(status_code=400, detail="month 必须是 YYYY-MM 格式")
    if not body.month and not body.leagues:
        raise HTTPException(
            status_code=400,
            detail="请至少选择「月份」或「联赛」之一，避免无界回填",
        )
    if body.leagues and any(x <= 0 for x in body.leagues):
        raise HTTPException(status_code=400, detail="leagues 必须为正整数")
    if not 0 < body.sleep <= 60:
        raise HTTPException(status_code=400, detail="sleep 必须大于 0 且不超过 60 秒")
    if not 0 <= body.max_requests <= 1000:
        raise HTTPException(status_code=400, detail="max_requests 必须在 0~1000 之间")

    cmd = _build_backfill_cmd(body)
    logger.info(f"手动触发: 按条件回填（后台执行）: {' '.join(cmd)}")
    _backfill_running = True
    background_tasks.add_task(_run_backfill_with_stream, cmd)
    return {"status": "ok", "message": "回填脚本已启动，正在后台执行"}


@router.post("/scheduler/backfill_fixtures/stop")
async def stop_backfill_fixtures(_: AdminAuth):
    """终止正在运行的回填子进程（对应页面上的「停止」按钮）。"""
    global _backfill_proc
    if _backfill_proc is None or _backfill_proc.poll() is not None:
        raise HTTPException(status_code=404, detail="当前没有正在运行的回填进程")
    push_task_message(BACKFILL_TASK_ID, "⏹ 收到停止指令，正在终止回填进程…")
    _backfill_proc.terminate()
    try:
        _backfill_proc.wait(timeout=10)
    except Exception:
        _backfill_proc.kill()
    return {"status": "ok", "message": "回填进程已终止"}


@router.post("/scheduler/{task_id}/trigger")
async def trigger_task(task_id: str, background_tasks: BackgroundTasks, _: AdminAuth):
    if task_id not in TASK_MAP:
        raise HTTPException(status_code=404, detail="任务不存在")
    name, fn = TASK_MAP[task_id]
    logger.info(f"手动触发: {name}（后台执行）")

    def _run():
        _run_task_with_stream(task_id, name, fn)

    background_tasks.add_task(_run)
    return {"status": "ok", "message": f"{name} 已触发，正在后台执行"}


@router.get("/scheduler/{task_id}/stream")
async def stream_task_logs(task_id: str, _: ReadAuth):
    """SSE 端点：实时推送后台任务的执行日志"""
    q = _ensure_queue(task_id)

    async def event_generator():
        while True:
            msg = await asyncio.to_thread(q.get)
            if msg == "__DONE__":
                _task_queues.pop(task_id, None)
                yield f"data: {msg}\n\n"
                break
            yield f"data: {msg}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

# ═══ 兼容旧接口 ═══
# 统一改走 ``run_task``：它内部复用 ``_prepare_task_run``（原子占位 + 复位 API
# 额度闩锁），与定时 loop 和 ``/{task_id}/trigger`` 共用同一段准备逻辑。
# 之前这里直连 ``sync_fn(db)``，既不占位也不复位，会踩到和手动触发一样的陈旧
# 额度状态陷阱（当天标记过"额度耗尽"后就一直被拦住，哪怕额度还有剩余）。
# 任务名与函数统一取 ``TASK_MAP``，避免再出现第三份映射表。
@router.post("/scheduler/leagues/sync")
async def trigger_league_sync(_: AdminAuth, db: Session = Depends(get_db)):
    name, fn = TASK_MAP["league_sync"]
    logger.info(f"手动触发: {name}")
    run_task("league_sync", name, fn, db=db)
    return {"status": "ok"}

@router.post("/scheduler/teams/sync")
async def trigger_team_sync(_: AdminAuth, db: Session = Depends(get_db)):
    name, fn = TASK_MAP["team_sync"]
    logger.info(f"手动触发: {name}")
    run_task("team_sync", name, fn, db=db)
    return {"status": "ok"}

@router.post("/scheduler/players/sync")
async def trigger_player_sync(_: AdminAuth, db: Session = Depends(get_db)):
    name, fn = TASK_MAP["player_sync"]
    logger.info(f"手动触发: {name}")
    run_task("player_sync", name, fn, db=db)
    return {"status": "ok"}

@router.post("/scheduler/standings/sync")
async def trigger_standing_sync(_: AdminAuth, db: Session = Depends(get_db)):
    name, fn = TASK_MAP["standing_sync"]
    logger.info(f"手动触发: {name}")
    run_task("standing_sync", name, fn, db=db)
    return {"status": "ok"}

@router.post("/scheduler/fixtures/sync")
async def trigger_fixture_sync(_: AdminAuth, db: Session = Depends(get_db)):
    """同步执行的遗留端点：同样要先占位，否则会和定时任务并发跑两遍子数据。"""
    name, fn = TASK_MAP["fixture_daily"]
    logger.info(f"手动触发: {name}")
    # 保留原本的 409 语义：占位失败（定时任务正在跑）时明确告知调用方。
    if not run_task("fixture_daily", name, fn, db=db):
        raise HTTPException(status_code=409, detail="赛程每日同步正在运行中，请稍后再试")
    return {"status": "ok"}

@router.post("/scheduler/fixtures/live")
async def trigger_live_sync(_: AdminAuth, db: Session = Depends(get_db)):
    name, fn = TASK_MAP["fixture_live"]
    logger.info(f"手动触发: {name}")
    run_task("fixture_live", name, fn, db=db)
    return {"status": "ok"}


@router.post("/scheduler/{task_id}/stop")
async def stop_task(task_id: str, _: AdminAuth):
    ok = stop_scheduled_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或无需停止")
    return {"status": "ok", "message": f"任务 {task_id} 已停止"}


@router.post("/scheduler/{task_id}/start")
async def start_task(task_id: str, _: AdminAuth):
    ok = start_scheduled_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或已在运行")
    return {"status": "ok", "message": f"任务 {task_id} 已启动"}


@router.patch("/scheduler/{task_id}")
async def patch_task(task_id: str, start_hour: float | None = None,
                     interval_seconds: int | None = None, is_enabled: bool | None = None,
                     weekday: int | None = None, _: str = Depends(require_admin)):
    # weekday: 0=周一..6=周日，-1 表示恢复为每天（NULL）
    ok = update_task(task_id, start_hour=start_hour, interval_seconds=interval_seconds,
                     is_enabled=is_enabled, weekday=weekday)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"status": "ok", "message": f"任务 {task_id} 已更新"}



