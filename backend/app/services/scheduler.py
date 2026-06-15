import asyncio
from datetime import datetime, timedelta

from sqlalchemy import text
from loguru import logger

from app.db.session import SessionLocal
from app.services.league_service import sync_leagues
from app.services.team_service import sync_teams
from app.services.player_service import sync_players
from app.services.standing_service import sync_standings
from app.services.fixture_service import sync_fixtures, sync_live_fixtures
from app.services.prediction_result_service import backfill_results
from app.services.auto_predict_service import auto_predict

LIVE_INTERVAL_SECONDS = 2 * 60

# ──── 默认任务定义 ────
_DEFAULT_TASKS = {
    "league_sync":    {"name": "联赛数据同步",   "start_hour": 2,   "fn": sync_leagues},
    "team_sync":      {"name": "球队数据同步",   "start_hour": 2.5, "fn": sync_teams},
    "player_sync":    {"name": "球员数据同步",   "start_hour": 3,   "fn": sync_players},
    "standing_sync":  {"name": "积分榜数据同步", "start_hour": 4,   "fn": sync_standings},
    "fixture_daily":  {"name": "赛程每日同步",   "start_hour": 5,   "fn": sync_fixtures},
    "fixture_live":   {"name": "赛程实时同步",   "start_hour": None, "fn": sync_live_fixtures,
                       "interval_seconds": LIVE_INTERVAL_SECONDS},
    "backfill_pred":  {"name": "预测结果回填",   "start_hour": None, "fn": backfill_results,
                       "interval_seconds": 3600},
    "auto_predict":   {"name": "赛前自动预测",   "start_hour": 12,  "fn": auto_predict,
                       "interval_seconds": None},
}

_active_loops: dict[str, asyncio.Task] = {}


def _seed_defaults():
    db = SessionLocal()
    try:
        for k, v in _DEFAULT_TASKS.items():
            sh = v.get("start_hour")
            iv = v.get("interval_seconds")
            db.execute(
                text(
                    """INSERT IGNORE INTO scheduler_tasks
                       (id, name, interval_seconds, start_hour, is_enabled)
                       VALUES (:id, :name, :iv, :sh, 1)"""
                ),
                {"id": k, "name": v["name"], "iv": iv, "sh": sh},
            )
        db.commit()
    finally:
        db.close()


def _read_db_row(task_key: str) -> dict | None:
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT * FROM scheduler_tasks WHERE id = :id"), {"id": task_key}
        ).fetchone()
        return dict(row._mapping) if row else None
    finally:
        db.close()


def _update_db(task_key: str, **kwargs):
    if not kwargs:
        return
    sets = ", ".join(f"{k} = :{k}" for k in kwargs)
    db = SessionLocal()
    try:
        db.execute(
            text(f"UPDATE scheduler_tasks SET {sets} WHERE id = :id"),
            {"id": task_key, **kwargs},
        )
        db.commit()
    finally:
        db.close()


def run_task_with_log(task_id: str, task_name: str, sync_fn, db=None):
    """执行任务并记录日志（供定时 loop 和手动触发共用）"""
    log_id = _log_start(task_id, task_name)
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        sync_fn(db)
        _log_finish(log_id, 'success', None)
    except Exception as e:
        _log_finish(log_id, 'failed', str(e))
        raise
    finally:
        if own_db:
            db.close()


def get_scheduler_status() -> dict:
    db = SessionLocal()
    try:
        rows = db.execute(text("SELECT * FROM scheduler_tasks ORDER BY id")).fetchall()
        tasks = []
        for row in rows:
            r = dict(row._mapping)
            sh = r.get("start_hour")
            iv = r.get("interval_seconds")
            if sh is not None:
                desc = f"每天 {int(sh):02d}:{int((sh % 1) * 60):02d}"
            else:
                desc = _desc_interval(iv or 0)
            tasks.append({
                "task_id": r["id"],
                "name": r["name"],
                "start_hour": sh,
                "interval_seconds": iv,
                "interval_desc": desc,
                "is_enabled": bool(r["is_enabled"]),
                "last_run": r["last_run"].isoformat() if r["last_run"] else None,
                "next_run": r["next_run"].isoformat() if r["next_run"] else None,
                "is_running": bool(r["is_running"]),
            })
        return {"tasks": tasks}
    finally:
        db.close()


def _log_start(task_id: str, task_name: str) -> int:
    """记录任务开始执行，返回 log id"""
    db = SessionLocal()
    try:
        result = db.execute(
            text(
                """INSERT INTO scheduler_logs (task_id, task_name, status, started_at)
                   VALUES (:tid, :name, 'running', :now)"""
            ),
            {"tid": task_id, "name": task_name, "now": datetime.now()},
        )
        db.commit()
        return result.lastrowid
    finally:
        db.close()


def _log_finish(log_id: int, status: str, message: str | None):
    db = SessionLocal()
    try:
        db.execute(
            text(
                """UPDATE scheduler_logs SET status=:st, message=:msg, finished_at=:now
                   WHERE id=:id"""
            ),
            {"st": status, "msg": message, "now": datetime.now(), "id": log_id},
        )
        db.commit()
    finally:
        db.close()


def _desc_interval(sec: int) -> str:
    if sec < 120:
        return f"每 {sec} 秒"
    if sec < 3600:
        return f"每 {sec // 60} 分钟"
    return f"每 {sec // 3600} 小时"


def stop_scheduled_task(task_key: str) -> bool:
    row = _read_db_row(task_key)
    if row is None:
        return False
    task = _active_loops.get(task_key)
    if task and not task.done():
        task.cancel()
    _update_db(task_key, is_enabled=0, is_running=0, next_run=None)
    logger.info(f"[Scheduler] {row['name']} 已停止")
    return True


def start_scheduled_task(task_key: str) -> bool:
    row = _read_db_row(task_key)
    if row is None:
        return False
    if row["is_enabled"]:
        return False
    _update_db(task_key, is_enabled=1)
    _start_loop(task_key)
    logger.info(f"[Scheduler] {row['name']} 已启动")
    return True


def update_task(task_key: str, start_hour: float = None, interval_seconds: int = None,
                is_enabled: bool = None) -> bool:
    row = _read_db_row(task_key)
    if row is None:
        return False

    kwargs = {}
    if start_hour is not None:
        kwargs["start_hour"] = start_hour
    if interval_seconds is not None:
        kwargs["interval_seconds"] = interval_seconds
    if is_enabled is not None:
        kwargs["is_enabled"] = 1 if is_enabled else 0
    if kwargs:
        _update_db(task_key, **kwargs)

    # 重启 loop
    task = _active_loops.get(task_key)
    if task and not task.done():
        task.cancel()

    if is_enabled is False:
        _update_db(task_key, is_running=0, next_run=None)
    elif is_enabled is True or kwargs:
        new_row = _read_db_row(task_key)
        if new_row and new_row["is_enabled"]:
            _start_loop(task_key)

    logger.info(f"[Scheduler] {row['name']} 已更新: {kwargs}")
    return True


# ──── Loop ────

def _start_loop(task_key: str):
    definition = _DEFAULT_TASKS.get(task_key)
    if not definition:
        return
    loop = asyncio.create_task(_run_loop(task_key))
    _active_loops[task_key] = loop


async def _run_loop(task_key: str):
    definition = _DEFAULT_TASKS[task_key]
    sync_fn = definition["fn"]

    while True:
        row = _read_db_row(task_key)
        if not row or not row["is_enabled"]:
            break

        sh = row.get("start_hour")
        iv = row.get("interval_seconds")

        # 计算下一执行时间
        if sh is not None:
            # 定时任务：每天固定时间
            now = datetime.now()
            target = now.replace(hour=int(sh), minute=int((sh % 1) * 60), second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            delay = (target - now).total_seconds()
            next_run = target
        else:
            # 间隔任务
            delay = iv or 60
            next_run = datetime.now() + timedelta(seconds=delay)

        _update_db(task_key, next_run=next_run)
        logger.info(
            f"[Scheduler] {row['name']} 已调度"
            + (f", 下次 {next_run.strftime('%H:%M')}" if sh else f", 间隔 {_desc_interval(delay)}")
        )

        await asyncio.sleep(delay)

        row = _read_db_row(task_key)
        if not row or not row["is_enabled"]:
            break

        _update_db(task_key, is_running=1, last_run=datetime.now())
        logger.info(f"[Scheduler] 触发: {row['name']}")

        try:
            await asyncio.to_thread(run_task_with_log, task_key, row['name'], sync_fn)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[Scheduler] {row['name']} 异常: {e}")
        finally:
            _update_db(task_key, is_running=0)


# ──── 初始化 ────

def init_scheduler():
    _seed_defaults()
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT id FROM scheduler_tasks WHERE is_enabled = 1")
        ).fetchall()
        for row in rows:
            _start_loop(row[0])
            logger.info(f"[Scheduler] 加载: {row[0]}")
    finally:
        db.close()


async def shutdown_scheduler():
    for key, task in list(_active_loops.items()):
        if not task.done():
            task.cancel()
    for key, task in list(_active_loops.items()):
        try:
            await task
        except asyncio.CancelledError:
            pass
    _active_loops.clear()
    logger.info("[Scheduler] 全部任务已取消")


league_sync_loop = None
team_sync_loop = None
player_sync_loop = None
standing_sync_loop = None
fixture_daily_loop = None
fixture_live_loop = None
