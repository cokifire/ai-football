import asyncio
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, selectinload

from app.db.session import get_db
from app.models.fixture import Fixture, FixtureEvent, FixtureLineup, FixtureStatistic, FixturePlayerStat
from app.schemas.fixture import FixtureSchema, FixtureDetailSchema
from app.schemas.league import PaginatedResponse
from app.core.zh import zh_swap, fixtures_apply_denorm_zh
from app.core.config import settings
from loguru import logger

router = APIRouter()

# DB 存的是北京时间，10:00 为次日分界
# 用户选北京日期 date，查询范围：date 10:00 ~ date+1 09:59
def _date_to_utc_range(date_str: str) -> tuple[str, str]:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start = d + timedelta(hours=10, minutes=10)
    end   = d + timedelta(hours=34, minutes=10)
    return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")


@router.get("/fixtures", response_model=PaginatedResponse[FixtureSchema])
async def list_fixtures(
    league_id: int | None = Query(None), season: int | None = Query(None),
    team_id: int | None = Query(None), date: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return await asyncio.to_thread(_list_sync, db, league_id, season, team_id, date, status, page, page_size)


def _list_sync(db, league_id, season, team_id, date, status, page, page_size):
    query = db.query(Fixture)
    if league_id is not None: query = query.filter(Fixture.league_id == league_id)
    if season is not None: query = query.filter(Fixture.season == season)
    if team_id is not None: query = query.filter((Fixture.home_id == team_id) | (Fixture.away_id == team_id))
    if date is not None:
        utc_start, utc_end = _date_to_utc_range(date)
        query = query.filter(Fixture.date >= utc_start)
        query = query.filter(Fixture.date < utc_end)
    if status is not None: query = query.filter(Fixture.status_short == status)
    total = query.count()
    fixtures = (query.order_by(Fixture.date.desc(), Fixture.id.desc())
                .offset((page - 1) * page_size).limit(page_size).all())
    fixtures_apply_denorm_zh(db, fixtures)
    return {"data": fixtures, "total": total, "page": page, "page_size": page_size}


@router.get("/fixtures/{fixture_id}", response_model=FixtureDetailSchema)
async def get_fixture(fixture_id: int, db: Session = Depends(get_db)):
    return await asyncio.to_thread(_get_sync, db, fixture_id)


def _get_sync(db, fixture_id):
    fixture = (db.query(Fixture).options(
        selectinload(Fixture.events), selectinload(Fixture.lineups),
        selectinload(Fixture.statistics), selectinload(Fixture.player_stats),
    ).filter(Fixture.id == fixture_id).first())
    if fixture is None:
        raise HTTPException(status_code=404, detail="比赛不存在")
    fixtures_apply_denorm_zh(db, [fixture])
    for e in fixture.events: zh_swap(e)
    for s in fixture.statistics: zh_swap(s)
    return fixture


@router.post("/fixtures/{fixture_id}/refresh", response_model=FixtureDetailSchema)
async def refresh_fixture_endpoint(fixture_id: int, db: Session = Depends(get_db)):
    """手动刷新: 重新从 API-Football 拉取并更新该场比赛主表与子数据，返回最新详情。"""
    if not settings.api_football_key:
        raise HTTPException(status_code=503, detail="未配置 API_FOOTBALL_KEY，无法刷新")
    from app.services.fixture_service import refresh_fixture
    try:
        ok = await asyncio.to_thread(refresh_fixture, db, fixture_id)
    except Exception as e:
        logger.error(f"刷新比赛失败 fixture={fixture_id}: {e}")
        raise HTTPException(status_code=502, detail=f"刷新失败: {e}")
    if not ok:
        raise HTTPException(status_code=502, detail="刷新失败：API-Football 未返回该比赛数据")
    fixture = await asyncio.to_thread(_get_sync, db, fixture_id)
    return fixture


@router.patch("/fixtures/{fixture_id}/category")
async def set_fixture_category(fixture_id: int, category: str | None = None,
                                db: Session = Depends(get_db)):
    """设置或清除比赛分类标签（category=jingzu 或 category=null 清除）"""
    def _run():
        f = db.query(Fixture).filter(Fixture.id == fixture_id).first()
        if not f:
            raise HTTPException(status_code=404, detail="比赛不存在")
        f.category = category
        db.commit()
        return {"fixture_id": fixture_id, "category": category}
    return await asyncio.to_thread(_run)



async def get_fixture_form(fixture_id: int, db: Session = Depends(get_db)):
    return await asyncio.to_thread(_form_sync, db, fixture_id)


def _form_sync(db, fixture_id):
    f = db.query(Fixture).filter(Fixture.id == fixture_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="比赛不存在")
    hid, aid, limit = f.home_id, f.away_id, 10
    home_recent = (db.query(Fixture).filter((Fixture.home_id == hid) | (Fixture.away_id == hid))
                   .order_by(Fixture.date.desc()).limit(limit).all())
    away_recent = (db.query(Fixture).filter((Fixture.home_id == aid) | (Fixture.away_id == aid))
                   .order_by(Fixture.date.desc()).limit(limit).all())
    h2h = (db.query(Fixture).filter(
        ((Fixture.home_id == hid) & (Fixture.away_id == aid)) | ((Fixture.home_id == aid) & (Fixture.away_id == hid))
    ).order_by(Fixture.date.desc()).limit(limit).all())
    fixtures_apply_denorm_zh(db, home_recent)
    fixtures_apply_denorm_zh(db, away_recent)
    fixtures_apply_denorm_zh(db, h2h)
    return {
        "home_recent": [FixtureSchema.model_validate(m).model_dump() for m in home_recent],
        "away_recent": [FixtureSchema.model_validate(m).model_dump() for m in away_recent],
        "h2h": [FixtureSchema.model_validate(m).model_dump() for m in h2h],
    }
