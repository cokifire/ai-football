import asyncio
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.standing import Standing
from app.schemas.standing import StandingSchema
from app.schemas.league import PaginatedResponse
from app.core.zh import standings_apply_denorm_zh

router = APIRouter()


@router.get("/standings", response_model=PaginatedResponse[StandingSchema])
async def get_standings(
    league_id: int = Query(...), season: int | None = Query(None),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
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
