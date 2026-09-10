import asyncio
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.standing import Standing
from app.schemas.standing import StandingSchema
from app.schemas.league import PaginatedResponse
from app.core.zh import standings_apply_denorm_zh
from app.services.standing_service import get_league_draw, has_league_draw

router = APIRouter()


@router.get("/standings/draw/available")
async def get_draw_available(league_id: int = Query(...)):
    """该联赛是否配置了淘汰赛对阵图(用于前端决定是否显示「淘汰赛」页签)"""
    return {"league_id": league_id, "available": has_league_draw(league_id)}


@router.get("/standings/draw")
async def get_draw(
    league_id: int = Query(...),
    refresh: bool = Query(False, description="忽略缓存重新抓取"),
    db: Session = Depends(get_db),
):
    """返回该联赛的淘汰赛对阵图(Flashscore /draw/ 页)。

    首次抓取约需 10 秒, 结果在服务端缓存 10 分钟。
    """
    return await asyncio.to_thread(get_league_draw, db, league_id, refresh)


@router.get("/standings/seasons")
async def get_standing_seasons(
    league_id: int = Query(...),
    db: Session = Depends(get_db),
):
    """返回该联赛在积分榜表中实际存在的赛季列表（降序）"""
    rows = (
        db.query(Standing.season)
        .filter(Standing.league_id == league_id)
        .distinct()
        .order_by(Standing.season.desc())
        .all()
    )
    return [r[0] for r in rows]


@router.get("/standings", response_model=PaginatedResponse[StandingSchema])
async def get_standings(
    league_id: int = Query(...), season: int | None = Query(None),
    page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return await asyncio.to_thread(_get_standings_sync, db, league_id, season, page, page_size)


def _get_standings_sync(db, league_id, season, page, page_size):
    query = db.query(Standing).filter(Standing.league_id == league_id)
    if season is not None:
        query = query.filter(Standing.season == season)
    else:
        sub = (db.query(Standing.season).filter(Standing.league_id == league_id)
               .order_by(Standing.season.desc()).limit(1).scalar_subquery())
        query = query.filter(Standing.season == sub)
    total = query.count()
    standings = (query.order_by(Standing.group_name, Standing.rank)
                 .offset((page - 1) * page_size).limit(page_size).all())
    standings_apply_denorm_zh(db, standings)
    return {"data": standings, "total": total, "page": page, "page_size": page_size}
