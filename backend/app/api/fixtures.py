import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import bindparam, text

from app.db.session import get_db
from app.models.fixture import Fixture, FixtureEvent, FixtureLineup, FixtureStatistic, FixturePlayerStat
from app.schemas.fixture import FixtureSchema, FixtureDetailSchema
from app.schemas.league import PaginatedResponse
from app.core.zh import zh_swap, fixtures_apply_denorm_zh
from app.core.config import settings
from app.core.security import AdminAuth
from loguru import logger

router = APIRouter()

# flashscore hash -> api-football team id
_TEAM_MAP_FILE = Path(__file__).resolve().parent.parent.parent / "tools" / "flashscore_team_map.json"

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
    team_id: int | None = Query(None), team_name: str | None = Query(None),
    date: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return await asyncio.to_thread(_list_sync, db, league_id, season, team_id, team_name, date, status, page, page_size)


def _list_sync(db, league_id, season, team_id, team_name, date, status, page, page_size):
    query = db.query(Fixture)
    if league_id is not None: query = query.filter(Fixture.league_id == league_id)
    if season is not None: query = query.filter(Fixture.season == season)
    if team_id is not None: query = query.filter((Fixture.home_id == team_id) | (Fixture.away_id == team_id))
    if team_name:
        like = f"%{team_name.strip()}%"
        query = query.filter((Fixture.home_name.ilike(like)) | (Fixture.away_name.ilike(like)))
    if date is not None:
        utc_start, utc_end = _date_to_utc_range(date)
        query = query.filter(Fixture.date >= utc_start)
        query = query.filter(Fixture.date < utc_end)
    if status is not None: query = query.filter(Fixture.status_short == status)
    total = query.count()
    fixtures = (query.order_by(Fixture.date.desc(), Fixture.id.desc())
                .offset((page - 1) * page_size).limit(page_size).all())
    if fixtures:
        fixture_ids = [fixture.id for fixture in fixtures]
        predicted_ids = set(db.execute(text(
            "SELECT fixture_id FROM predictions WHERE fixture_id IN :fixture_ids"
        ).bindparams(bindparam("fixture_ids", expanding=True)), {
            "fixture_ids": fixture_ids,
        }).scalars())
        for fixture in fixtures:
            # 该字段只用于接口响应，不写入 fixtures 表。
            fixture.predicted = fixture.id in predicted_ids
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
async def refresh_fixture_endpoint(fixture_id: int, _: AdminAuth, db: Session = Depends(get_db)):
    """手动刷新: 重新从 API-Football 拉取并更新该场比赛主表与子数据，返回最新详情。"""
    if not settings.api_football_key:
        raise HTTPException(status_code=503, detail="未配置 API_FOOTBALL_KEY，无法刷新")
    from app.services.fixture_service import ApiFootballQuotaExceeded, refresh_fixture
    try:
        ok = await asyncio.to_thread(refresh_fixture, db, fixture_id)
    except ApiFootballQuotaExceeded:
        raise HTTPException(status_code=429, detail="Football API 今日请求额度已用完，请稍后再试")
    except Exception as e:
        logger.error(f"刷新比赛失败 fixture={fixture_id}: {e}")
        raise HTTPException(status_code=502, detail=f"刷新失败: {e}")
    if not ok:
        raise HTTPException(status_code=502, detail="刷新失败：API-Football 未返回该比赛数据")
    fixture = await asyncio.to_thread(_get_sync, db, fixture_id)
    return fixture


@router.post("/fixtures/{fixture_id}/fetch-xg", response_model=FixtureDetailSchema)
async def fetch_xg_endpoint(fixture_id: int, _: AdminAuth, db: Session = Depends(get_db)):
    """从 Flashscore 抓取该场比赛的 Expected goals (xG) 与 Goals prevented, 写入 fixture_statistics。

    仅当双方球队都能在 flashscore_team_map.json 中找到对应 hash 时才可抓取。
    """
    def _run():
        f = db.query(Fixture).filter(Fixture.id == fixture_id).first()
        if not f:
            raise HTTPException(status_code=404, detail="比赛不存在")

        # 加载 hash -> api_football_id 映射, 反查 id -> hash
        if not _TEAM_MAP_FILE.exists():
            raise HTTPException(status_code=503, detail="未找到 flashscore_team_map.json")
        override = json.loads(_TEAM_MAP_FILE.read_text(encoding="utf-8"))
        id_to_hash = {tid: h for h, tid in override.items()}

        home_hash = id_to_hash.get(f.home_id)
        away_hash = id_to_hash.get(f.away_id)
        if not home_hash or not away_hash:
            miss = []
            if not home_hash:
                miss.append(f"{f.home_name}(id={f.home_id})")
            if not away_hash:
                miss.append(f"{f.away_name}(id={f.away_id})")
            raise HTTPException(
                status_code=422,
                detail=f"以下球队不在 flashscore_team_map.json 中, 无法抓取 xG: {'; '.join(miss)}",
            )

        # hash -> 队名 (用于生成 Flashscore URL slug)
        rows = db.execute(text("SELECT id, name FROM teams")).fetchall()
        id_to_name = {r[0]: r[1] for r in rows}
        hash_to_name = {}
        if f.home_id in id_to_name:
            hash_to_name[home_hash] = id_to_name[f.home_id]
        if f.away_id in id_to_name:
            hash_to_name[away_hash] = id_to_name[f.away_id]

        # 调用抓取脚本
        sys_path = str(_TEAM_MAP_FILE.resolve().parent.parent)  # backend/
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        try:
            from tools.fetch_flashscore_match_xg import scrape_match_xg
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"导入抓取脚本失败: {e}")

        try:
            stats = scrape_match_xg(home_hash, away_hash, hash_to_name, match_date=f.date)
        except Exception as e:
            logger.error(f"抓取 Flashscore xG 失败 fixture={fixture_id}: {e}")
            raise HTTPException(status_code=502, detail=f"抓取 Flashscore xG 失败: {e}")

        # 写库: 每队两条 (expected_goals, goals_prevented)
        def upsert(team_id, team_name, vals):
            for stat_type, value in (
                ("expected_goals", vals.get("xg")),
                ("goals_prevented", vals.get("goals_prevented")),
            ):
                if value is None:
                    continue
                exist = (
                    db.query(FixtureStatistic)
                    .filter_by(fixture_id=fixture_id, team_id=team_id, stat_type=stat_type)
                    .first()
                )
                if exist:
                    exist.stat_value = str(value)
                    exist.team_name = team_name
                else:
                    db.add(FixtureStatistic(
                        fixture_id=fixture_id, team_id=team_id,
                        team_name=team_name, stat_type=stat_type, stat_value=str(value),
                    ))
        upsert(f.home_id, f.home_name, stats["home"])
        upsert(f.away_id, f.away_name, stats["away"])
        db.commit()

        return _get_sync(db, fixture_id)

    return await asyncio.to_thread(_run)


@router.patch("/fixtures/{fixture_id}/category")
async def set_fixture_category(fixture_id: int, _: AdminAuth,
                                category: str | None = None,
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
