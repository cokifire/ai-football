import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_

from app.db.session import get_db
from app.models.team import Team
from app.schemas.team import TeamDetailSchema
from app.schemas.league import PaginatedResponse
from app.core.zh import zh_swap

router = APIRouter()


@router.get("/teams", response_model=PaginatedResponse[TeamDetailSchema])
async def get_teams(
    page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100),
    search: str | None = Query(None), db: Session = Depends(get_db),
):
    return await asyncio.to_thread(_get_teams_sync, db, page, page_size, search)


def _get_teams_sync(db, page, page_size, search):
    query = db.query(Team)
    if search:
        query = query.filter(or_(
            Team.name.contains(search), Team.name_zh.contains(search),
            Team.country.contains(search), Team.country_zh.contains(search),
        ))
    total = query.count()
    teams = (query.options(joinedload(Team.venue)).order_by(Team.id)
             .offset((page - 1) * page_size).limit(page_size).all())
    for t in teams:
        zh_swap(t)
        if t.venue:
            zh_swap(t.venue)
    return {"data": teams, "total": total, "page": page, "page_size": page_size}


@router.get("/teams/{team_id}", response_model=TeamDetailSchema)
async def get_team(team_id: int, db: Session = Depends(get_db)):
    return await asyncio.to_thread(_get_team_sync, db, team_id)


def _get_team_sync(db, team_id):
    team = (db.query(Team).options(joinedload(Team.venue))
            .filter(Team.id == team_id).first())
    if team is None:
        raise HTTPException(status_code=404, detail="球队不存在")
    zh_swap(team)
    if team.venue:
        zh_swap(team.venue)
    return team
