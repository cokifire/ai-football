import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import or_

from app.db.session import get_db
from app.models.player import Player, PlayerStats
from app.schemas.player import PlayerDetailSchema
from app.schemas.league import PaginatedResponse
from app.core.zh import zh_swap

router = APIRouter()


@router.get("/players", response_model=PaginatedResponse[PlayerDetailSchema])
async def get_players(
    page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100),
    search: str | None = Query(None), db: Session = Depends(get_db),
):
    return await asyncio.to_thread(_get_players_sync, db, page, page_size, search)


def _get_players_sync(db, page, page_size, search):
    query = db.query(Player)
    if search:
        query = query.filter(or_(
            Player.name.contains(search), Player.name_zh.contains(search),
            Player.nationality.contains(search),
        ))
    total = query.count()
    players = (query.options(
        selectinload(Player.statistics).selectinload(PlayerStats.team),
        selectinload(Player.statistics).selectinload(PlayerStats.league),
    ).order_by(Player.id).offset((page - 1) * page_size).limit(page_size).all())
    for p in players:
        zh_swap(p)
        for s in p.statistics:
            if s.team: zh_swap(s.team)
            if s.league: zh_swap(s.league)
    return {"data": players, "total": total, "page": page, "page_size": page_size}


@router.get("/players/{player_id}", response_model=PlayerDetailSchema)
async def get_player(player_id: int, db: Session = Depends(get_db)):
    return await asyncio.to_thread(_get_player_sync, db, player_id)


def _get_player_sync(db, player_id):
    player = (db.query(Player).options(
        selectinload(Player.statistics).selectinload(PlayerStats.team),
        selectinload(Player.statistics).selectinload(PlayerStats.league),
    ).filter(Player.id == player_id).first())
    if player is None:
        raise HTTPException(status_code=404, detail="球员不存在")
    zh_swap(player)
    for s in player.statistics:
        if s.team: zh_swap(s.team)
        if s.league: zh_swap(s.league)
    return player
