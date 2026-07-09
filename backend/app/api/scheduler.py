import asyncio
import queue
import sys
import io
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from loguru import logger

from app.db.session import get_db
from app.services.scheduler import (
    get_scheduler_status, stop_scheduled_task, start_scheduled_task,
    update_task, run_task_with_log,
)
from app.services.league_service import sync_leagues
from app.services.team_service import sync_teams
from app.services.player_service import sync_players
from app.services.standing_service import sync_standings
from app.services.fixture_service import sync_fixtures, sync_live_fixtures
from app.services.prediction_result_service import backfill_results
from app.services.auto_predict_service import auto_predict

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
        push_task_message(task_id, "__DONE__")


@router.get("/scheduler/status")
async def scheduler_status():
    return await asyncio.to_thread(get_scheduler_status)


@router.post("/scheduler/{task_id}/trigger")
async def trigger_task(task_id: str, background_tasks: BackgroundTasks):
    if task_id not in TASK_MAP:
        raise HTTPException(status_code=404, detail="任务不存在")
    name, fn = TASK_MAP[task_id]
    logger.info(f"手动触发: {name}（后台执行）")

    def _run():
        _run_task_with_stream(task_id, name, fn)

    background_tasks.add_task(_run)
    return {"status": "ok", "message": f"{name} 已触发，正在后台执行"}


@router.get("/scheduler/{task_id}/stream")
async def stream_task_logs(task_id: str):
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
@router.post("/scheduler/leagues/sync")
async def trigger_league_sync(db: Session = Depends(get_db)):
    logger.info("手动触发: 联赛数据同步")
    sync_leagues(db)
    return {"status": "ok"}

@router.post("/scheduler/teams/sync")
async def trigger_team_sync(db: Session = Depends(get_db)):
    logger.info("手动触发: 球队数据同步")
    sync_teams(db)
    return {"status": "ok"}

@router.post("/scheduler/players/sync")
async def trigger_player_sync(db: Session = Depends(get_db)):
    logger.info("手动触发: 球员数据同步")
    sync_players(db)
    return {"status": "ok"}

@router.post("/scheduler/standings/sync")
async def trigger_standing_sync(db: Session = Depends(get_db)):
    logger.info("手动触发: 积分榜数据同步")
    sync_standings(db)
    return {"status": "ok"}

@router.post("/scheduler/fixtures/sync")
async def trigger_fixture_sync(db: Session = Depends(get_db)):
    logger.info("手动触发: 赛程每日同步")
    sync_fixtures(db)
    return {"status": "ok"}

@router.post("/scheduler/fixtures/live")
async def trigger_live_sync(db: Session = Depends(get_db)):
    logger.info("手动触发: 赛程实时同步")
    sync_live_fixtures(db)
    return {"status": "ok"}


@router.post("/scheduler/{task_id}/stop")
async def stop_task(task_id: str):
    ok = stop_scheduled_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或无需停止")
    return {"status": "ok", "message": f"任务 {task_id} 已停止"}


@router.post("/scheduler/{task_id}/start")
async def start_task(task_id: str):
    ok = start_scheduled_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或已在运行")
    return {"status": "ok", "message": f"任务 {task_id} 已启动"}


@router.patch("/scheduler/{task_id}")
async def patch_task(task_id: str, start_hour: float | None = None,
                     interval_seconds: int | None = None, is_enabled: bool | None = None):
    ok = update_task(task_id, start_hour=start_hour, interval_seconds=interval_seconds,
                     is_enabled=is_enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"status": "ok", "message": f"任务 {task_id} 已更新"}


@router.get("/scheduler/logs")
async def get_logs(page: int = 1, page_size: int = 20, task_id: str | None = None):
    return await asyncio.to_thread(_get_logs_sync, page, page_size, task_id)


def _get_logs_sync(page, page_size, task_id):
    from app.db.session import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        where, params = "", {}
        if task_id: where, params["tid"] = "WHERE task_id = :tid", task_id
        total = db.execute(text(f"SELECT COUNT(*) FROM scheduler_logs {where}"), params).scalar()
        rows = db.execute(text(f"SELECT * FROM scheduler_logs {where} ORDER BY id DESC LIMIT :limit OFFSET :offset"),
                          {**params, "limit": page_size, "offset": (page - 1) * page_size}).fetchall()
        data = []
        for r in rows:
            d = dict(r._mapping)
            data.append({"id": d["id"], "task_id": d["task_id"], "task_name": d["task_name"],
                         "status": d["status"], "message": d["message"],
                         "started_at": d["started_at"].isoformat() if d["started_at"] else None,
                         "finished_at": d["finished_at"].isoformat() if d["finished_at"] else None})
        return {"data": data, "total": total, "page": page, "page_size": page_size}
    finally:
        db.close()
