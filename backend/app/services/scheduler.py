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
    "league_sync":    {"name": "联赛数据同步",   "start_hour": 8,   "fn": sync_leagues},
    "team_sync":      {"name": "球队数据同步",   "start_hour": 8.2, "fn": sync_teams, "enabled": 0},
    "player_sync":    {"name": "球员数据同步",   "start_hour": 8.25, "weekday": 0, "enabled": 0, "fn": sync_players},
    "standing_sync":  {"name": "积分榜数据同步", "start_hour": 9,   "fn": sync_standings},
    "fixture_daily":  {"name": "赛程每日同步",   "start_hour": 8.5,   "fn": sync_fixtures},
    "fixture_live":   {"name": "赛程实时同步",   "start_hour": None, "fn": sync_live_fixtures,
                       "interval_seconds": LIVE_INTERVAL_SECONDS},
    "backfill_pred":  {"name": "预测结果回填",   "start_hour": 9,   "fn": backfill_results,
                       "interval_seconds": None},
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
            en = v.get("enabled", 1)
            wd = v.get("weekday", None)
            # 用 ON DUPLICATE KEY UPDATE 让代码里的最新默认时间始终生效
            # （INSERT IGNORE 遇到已存在的主键会静默跳过，导致改了 _DEFAULT_TASKS
            #  之后数据库里仍是旧时间，任务仍按旧时间执行）
            # 注意：is_enabled / weekday 也同步，因为部分任务（如 team_sync）默认停用、
            #  player_sync 默认每周一跑。重启后需恢复默认状态。
            db.execute(
                text(
                    """INSERT INTO scheduler_tasks
                          (id, name, interval_seconds, start_hour, is_enabled, weekday)
                       VALUES (:id, :name, :iv, :sh, :en, :wd)
                       ON DUPLICATE KEY UPDATE
                          name = VALUES(name),
                          interval_seconds = VALUES(interval_seconds),
                          start_hour = VALUES(start_hour),
                          is_enabled = VALUES(is_enabled),
                          weekday = VALUES(weekday)"""
                ),
                {"id": k, "name": v["name"], "iv": iv, "sh": sh, "en": en, "wd": wd},
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


def run_task(task_id: str, task_name: str, sync_fn, db=None):
    """执行任务（供定时 loop 和手动触发共用）"""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        sync_fn(db)
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
            wd = r.get("weekday", None)
            if sh is not None:
                hh = f"{int(sh):02d}:{int((sh % 1) * 60):02d}"
                desc = f"每周{_WEEKDAY_CN[wd]} {hh}" if wd is not None else f"每天 {hh}"
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


def _desc_interval(sec: int) -> str:
    if sec < 120:
        return f"每 {sec} 秒"
    if sec < 3600:
        return f"每 {sec // 60} 分钟"
    return f"每 {sec // 3600} 小时"


# weekday: 0=周一 .. 6=周日
_WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]


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
                is_enabled: bool = None, weekday: int = None) -> bool:
    """weekday: 0=周一..6=周日（None=每天）。传 None 给 weekday 参数不会清空，
    需显式传 -1 表示恢复为每天。"""
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
    if weekday is not None:
        kwargs["weekday"] = None if weekday < 0 else weekday
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
        wd = row.get("weekday", None)

        # 计算下一执行时间
        if sh is not None:
            # 定时任务：每天或每周固定时间
            now = datetime.now()
            target = now.replace(hour=int(sh), minute=int((sh % 1) * 60), second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            # weekday 指定：0=周一..6=周日，None=每天
            if wd is not None:
                days_ahead = (wd - target.weekday()) % 7
                if days_ahead:
                    target = target + timedelta(days=days_ahead)
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
            await asyncio.to_thread(run_task, task_key, row['name'], sync_fn)
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
