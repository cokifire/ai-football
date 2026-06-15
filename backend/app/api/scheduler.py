import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query
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


@router.get("/scheduler/status")
async def scheduler_status():
    return await asyncio.to_thread(get_scheduler_status)


@router.post("/scheduler/{task_id}/trigger")
async def trigger_task(task_id: str):
    if task_id not in TASK_MAP:
        raise HTTPException(status_code=404, detail="任务不存在")
    name, fn = TASK_MAP[task_id]
    logger.info(f"手动触发: {name}")
    import asyncio
    await asyncio.to_thread(run_task_with_log, task_id, name, fn)
    return {"status": "ok", "message": f"{name} 已完成"}

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
