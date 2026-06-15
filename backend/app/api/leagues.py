import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_

from app.db.session import get_db
from app.models.league import League
from app.schemas.league import LeagueDetailSchema, PaginatedResponse
from app.core.zh import zh_swap, zh_swap_many

router = APIRouter()


@router.get("/leagues", response_model=PaginatedResponse[LeagueDetailSchema])
async def get_leagues(
    page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=200),
    search: str | None = Query(None), enabled: bool | None = Query(None),
    db: Session = Depends(get_db),
):
    return await asyncio.to_thread(_get_leagues_sync, db, page, page_size, search, enabled)


def _get_leagues_sync(db, page, page_size, search, enabled):
    query = db.query(League)
    if enabled is not None:
        query = query.filter(League.enabled == enabled)
    if search:
        query = query.filter(or_(
            League.name.contains(search), League.name_zh.contains(search),
            League.country_name.contains(search), League.country_name_zh.contains(search),
        ))
    total = query.count()
    leagues = (query.options(joinedload(League.seasons)).order_by(League.id)
               .offset((page - 1) * page_size).limit(page_size).all())
    zh_swap_many(leagues)
    return {"data": leagues, "total": total, "page": page, "page_size": page_size}


@router.get("/leagues/{league_id}", response_model=LeagueDetailSchema)
async def get_league(league_id: int, db: Session = Depends(get_db)):
    return await asyncio.to_thread(_get_league_sync, db, league_id)


def _get_league_sync(db, league_id):
    league = (db.query(League).options(joinedload(League.seasons))
              .filter(League.id == league_id).first())
    if league is None:
        raise HTTPException(status_code=404, detail="联赛不存在")
    zh_swap(league)
    return league


@router.patch("/leagues/{league_id}/toggle")
async def toggle_league(league_id: int, db: Session = Depends(get_db)):
    return await asyncio.to_thread(_toggle_league_sync, db, league_id)


def _toggle_league_sync(db, league_id):
    league = db.query(League).filter(League.id == league_id).first()
    if league is None:
        raise HTTPException(status_code=404, detail="联赛不存在")
    league.enabled = not league.enabled
    db.commit()
    return {"id": league_id, "enabled": league.enabled}
