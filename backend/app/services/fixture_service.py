from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from sqlalchemy.orm import Session
from sqlalchemy import text
from loguru import logger
import httpx
import time

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.league import League, Season
from app.models.fixture import Fixture, FixtureEvent, FixtureLineup, FixtureStatistic, FixturePlayerStat

LIVE_SYNC_INTERVAL = 120  # 秒

# ──────────── daily sync ────────────

def _upsert_fixture(db: Session, row: dict) -> Fixture | None:
    """插入或更新 fixture 主表，返回 Fixture 对象"""
    fixture_raw = row.get("fixture", {})
    league_raw = row.get("league", {})
    teams_raw = row.get("teams", {})
    home_raw = teams_raw.get("home") or {}
    away_raw = teams_raw.get("away") or {}
    goals_raw = row.get("goals", {})
    score_raw = row.get("score", {})
    venue_raw = fixture_raw.get("venue") or {}
    status_raw = fixture_raw.get("status") or {}
    periods_raw = fixture_raw.get("periods") or {}
    halftime_raw = score_raw.get("halftime") or {}
    fulltime_raw = score_raw.get("fulltime") or {}
    extratime_raw = score_raw.get("extratime") or {}
    penalty_raw = score_raw.get("penalty") or {}

    fid = fixture_raw.get("id")
    if not fid:
        return None

    obj = db.query(Fixture).filter(Fixture.id == fid).first()
    if obj is None:
        obj = Fixture(id=fid)
        db.add(obj)

    obj.date = fixture_raw.get("date")
    obj.timestamp = fixture_raw.get("timestamp")
    obj.timezone = fixture_raw.get("timezone")
    obj.referee = fixture_raw.get("referee")
    obj.first_period = periods_raw.get("first")
    obj.second_period = periods_raw.get("second")
    obj.venue_id = venue_raw.get("id")
    obj.venue_name = venue_raw.get("name")
    obj.venue_city = venue_raw.get("city")
    obj.status_short = status_raw.get("short")
    obj.status_long = status_raw.get("long")
    obj.status_elapsed = status_raw.get("elapsed")
    obj.status_extra = status_raw.get("extra")
    obj.league_id = league_raw.get("id")
    obj.league_name = league_raw.get("name")
    obj.season = league_raw.get("season")
    obj.round = league_raw.get("round")
    obj.home_id = home_raw.get("id")
    obj.home_name = home_raw.get("name")
    obj.home_logo = home_raw.get("logo")
    obj.home_winner = home_raw.get("winner")
    obj.away_id = away_raw.get("id")
    obj.away_name = away_raw.get("name")
    obj.away_logo = away_raw.get("logo")
    obj.away_winner = away_raw.get("winner")
    obj.goals_home = goals_raw.get("home")
    obj.goals_away = goals_raw.get("away")
    obj.halftime_home = halftime_raw.get("home")
    obj.halftime_away = halftime_raw.get("away")
    obj.fulltime_home = fulltime_raw.get("home")
    obj.fulltime_away = fulltime_raw.get("away")
    obj.extratime_home = extratime_raw.get("home")
    obj.extratime_away = extratime_raw.get("away")
    obj.penalty_home = penalty_raw.get("home")
    obj.penalty_away = penalty_raw.get("away")

    return obj


def sync_fixtures(db: Session) -> None:
    """每日同步: 遍历已启用联赛当前赛季，拉取近 7 天 + 未来 7 天赛程"""
    if not settings.api_football_key:
        logger.warning("API_FOOTBALL_KEY 未配置，跳过赛程同步")
        return

    from datetime import timedelta
    today = datetime.now().date()
    from_date = (today - timedelta(days=1)).isoformat()
    to_date = today.isoformat()

    seasons = (
        db.query(Season)
        .join(League)
        .filter(
            Season.is_current == True,
            League.enabled == True,
        )
        .all()
    )
    if not seasons:
        logger.warning("没有找到当前赛季，跳过赛程同步")
        return

    total_synced = 0
    processed = 0
    for season in seasons:
        msg = f"联赛 {season.league_id} 赛季 {season.year}"
        logger.info(f"拉取{msg}昨日+今日赛程 ({from_date} ~ {to_date})...")

        try:
            response = httpx.get(
                f"{settings.api_football_base_url}/fixtures",
                headers={"x-apisports-key": settings.api_football_key},
                params={
                    "league": str(season.league_id),
                    "season": str(season.year),
                    "from": from_date,
                    "to": to_date,
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"拉取{msg}赛程失败: {e}")
            print(f"  {msg} FAILED: {e}")
            continue

        items = data.get("response", [])
        if not items:
            continue

        for row in items:
            _upsert_fixture(db, row)

        db.commit()
        processed += 1
        total_synced += len(items)
        print(f"  [{processed}/{len(seasons)}] {msg}: {len(items)} 场")

    logger.info(f"赛程每日同步完成，共 {total_synced} 场")
    print(f"  赛程每日同步完成: {total_synced} 场")

    # 赛后子数据同步
    sync_completed_sub_data(db)


# ──────────── sub-data sync ────────────

BATCH_SIZE = 4
MAX_WORKERS = 4
RETRY_MAX = 3


def _do_fetch(db: Session, fixture_id: int) -> None:
    """4 个 API 并行请求，已存在则跳过，纯 INSERT 无 DELETE"""
    from app.models.fixture import FixtureEvent, FixtureLineup, FixtureStatistic, FixturePlayerStat

    results = {}
    check_db = SessionLocal()
    try:
        need_events = not check_db.query(FixtureEvent).filter(FixtureEvent.fixture_id == fixture_id).first()
        need_lineups = not check_db.query(FixtureLineup).filter(FixtureLineup.fixture_id == fixture_id).first()
        need_stats = not check_db.query(FixtureStatistic).filter(FixtureStatistic.fixture_id == fixture_id).first()
        need_players = not check_db.query(FixturePlayerStat).filter(FixturePlayerStat.fixture_id == fixture_id).first()
    finally:
        check_db.close()

    if not any([need_events, need_lineups, need_stats, need_players]):
        db.query(Fixture).filter(Fixture.id == fixture_id).update(
            {"sub_data_synced": True}, synchronize_session=False
        )
        db.commit()
        return

    def _fetch_one(fn, key):
        s = SessionLocal()
        try:
            fn(s, fixture_id)
            s.commit()
            results[key] = True
        except Exception as e:
            logger.warning(f"子数据 {key} fixture={fixture_id}: {e}")
            s.rollback()
            results[key] = False
        finally:
            s.close()

    tasks = []
    if need_events: tasks.append((_fetch_events, "events"))
    if need_lineups: tasks.append((_fetch_lineups, "lineups"))
    if need_stats: tasks.append((_fetch_statistics, "statistics"))
    if need_players: tasks.append((_fetch_player_stats, "players"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_fetch_one, fn, key) for fn, key in tasks]
        for f in futures:
            f.result()

    fail = [k for k in ["events", "lineups", "statistics", "players"]
            if k in results and not results[k]]
    if not fail:
        db.query(Fixture).filter(Fixture.id == fixture_id).update(
            {"sub_data_synced": True}, synchronize_session=False
        )
        db.commit()
    else:
        raise Exception(f"子数据部分失败 fixture={fixture_id}: {fail}")


def _sync_one_fixture_sub_data(fixture_id: int) -> bool:
    """单个 fixture 子数据拉取，失败重试"""
    for attempt in range(RETRY_MAX):
        db = SessionLocal()
        try:
            _do_fetch(db, fixture_id)
            return True
        except Exception as e:
            db.rollback()
            if attempt < RETRY_MAX - 1:
                time.sleep(2 * (attempt + 1))  # 退避: 2s, 4s, 6s
                logger.debug(f"子数据重试 fixture={fixture_id} #{attempt+1}")
            else:
                logger.warning(f"子数据失败 fixture={fixture_id}: {e}")
                return False
        finally:
            db.close()
    return False


def sync_completed_sub_data(db: Session) -> None:
    """为已完赛但子数据未同步的比赛并发拉取"""
    finished_statuses = {"FT", "AET", "PEN", "AWD", "WO"}
    fixtures = (
        db.query(Fixture)
        .filter(
            Fixture.sub_data_synced == False,
            Fixture.status_short.in_(finished_statuses),
        )
        .limit(200)
        .all()
    )
    if not fixtures:
        return

    logger.info(f"拉取 {len(fixtures)} 场完赛的子数据 (并发={MAX_WORKERS})...")
    ids = [f.id for f in fixtures]
    total_ok = 0
    total_fail = 0

    # 分批处理
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i : i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_sync_one_fixture_sub_data, fid): fid
                for fid in batch
            }
            ok = 0
            for f in as_completed(futures):
                fid = futures[f]
                try:
                    if f.result():
                        ok += 1
                    else:
                        total_fail += 1
                except Exception as e:
                    total_fail += 1
                    logger.warning(f"fixture={fid} 线程异常: {e}")

            total_ok += ok
            # 每 10 个或最后一批输出
            batch_no = (i // BATCH_SIZE) + 1
            if batch_no == 1 or batch_no % 10 == 0 or batch_no * BATCH_SIZE >= len(ids):
                print(f"    [{datetime.now().strftime('%H:%M:%S')}] {total_ok}/{len(ids)}", flush=True)

        time.sleep(0.3)  # 批次间隔

    db.expire_all()
    logger.info(f"子数据同步完成 ({total_ok}/{len(ids)}, 失败 {total_fail})")


def _fetch_events(db: Session, fixture_id: int) -> None:
    try:
        r = httpx.get(
            f"{settings.api_football_base_url}/fixtures/events",
            headers={"x-apisports-key": settings.api_football_key},
            params={"fixture": fixture_id},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"拉取事件失败 fixture={fixture_id}: {e}")
        return

    for evt in data.get("response", []):
        time_raw = evt.get("time") or {}
        team_raw = evt.get("team") or {}
        player_raw = evt.get("player") or {}
        assist_raw = evt.get("assist") or {}
        db.add(
            FixtureEvent(
                fixture_id=fixture_id,
                elapsed=time_raw.get("elapsed"),
                extra=time_raw.get("extra"),
                type=evt.get("type"),
                detail=evt.get("detail"),
                comments=evt.get("comments"),
                team_id=team_raw.get("id"),
                team_name=team_raw.get("name"),
                player_id=player_raw.get("id"),
                player_name=player_raw.get("name"),
                assist_id=assist_raw.get("id"),
                assist_name=assist_raw.get("name"),
            )
        )


def _fetch_lineups(db: Session, fixture_id: int) -> None:
    try:
        r = httpx.get(
            f"{settings.api_football_base_url}/fixtures/lineups",
            headers={"x-apisports-key": settings.api_football_key},
            params={"fixture": fixture_id},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"拉取阵容失败 fixture={fixture_id}: {e}")
        return

    for team_entry in data.get("response", []):
        team_raw = team_entry.get("team") or {}
        formation = team_entry.get("formation", "")
        tid = team_raw.get("id")
        tname = team_raw.get("name", "")

        for xi in team_entry.get("startXI", []):
            player_raw = xi.get("player") or {}
            db.add(
                FixtureLineup(
                    fixture_id=fixture_id,
                    team_id=tid,
                    team_name=tname,
                    formation=formation,
                    player_id=player_raw.get("id"),
                    player_name=player_raw.get("name"),
                    player_number=player_raw.get("number"),
                    player_position=player_raw.get("pos"),
                    player_grid=player_raw.get("grid"),
                    is_substitute=False,
                )
            )
        for sub in team_entry.get("substitutes", []):
            player_raw = sub.get("player") or {}
            db.add(
                FixtureLineup(
                    fixture_id=fixture_id,
                    team_id=tid,
                    team_name=tname,
                    formation=formation,
                    player_id=player_raw.get("id"),
                    player_name=player_raw.get("name"),
                    player_number=player_raw.get("number"),
                    player_position=player_raw.get("pos"),
                    player_grid=player_raw.get("grid"),
                    is_substitute=True,
                )
            )


def _fetch_statistics(db: Session, fixture_id: int) -> None:
    try:
        r = httpx.get(
            f"{settings.api_football_base_url}/fixtures/statistics",
            headers={"x-apisports-key": settings.api_football_key},
            params={"fixture": fixture_id},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"拉取统计失败 fixture={fixture_id}: {e}")
        return

    for team_entry in data.get("response", []):
        team_raw = team_entry.get("team") or {}
        tid = team_raw.get("id")
        tname = team_raw.get("name", "")
        for stat in team_entry.get("statistics", []):
            db.add(
                FixtureStatistic(
                    fixture_id=fixture_id,
                    team_id=tid,
                    team_name=tname,
                    stat_type=stat.get("type"),
                    stat_value=str(stat.get("value")) if stat.get("value") is not None else None,
                )
            )


def _fetch_player_stats(db: Session, fixture_id: int) -> None:
    try:
        r = httpx.get(
            f"{settings.api_football_base_url}/fixtures/players",
            headers={"x-apisports-key": settings.api_football_key},
            params={"fixture": fixture_id},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"拉取球员统计失败 fixture={fixture_id}: {e}")
        return

    for team_entry in data.get("response", []):
        team_raw = team_entry.get("team") or {}
        tid = team_raw.get("id")
        tname = team_raw.get("name", "")
        for p_entry in team_entry.get("players", []):
            player_raw = p_entry.get("player") or {}
            stats_list = p_entry.get("statistics") or []
            stats_data = stats_list[0] if stats_list else {}

            db.add(
                FixturePlayerStat(
                    fixture_id=fixture_id,
                    team_id=tid,
                    team_name=tname,
                    player_id=player_raw.get("id"),
                    player_name=player_raw.get("name"),
                    player_photo=player_raw.get("photo"),
                    games=stats_data.get("games"),
                    offsides=stats_data.get("offsides"),
                    shots=stats_data.get("shots"),
                    goals=stats_data.get("goals"),
                    passes=stats_data.get("passes"),
                    tackles=stats_data.get("tackles"),
                    duels=stats_data.get("duels"),
                    dribbles=stats_data.get("dribbles"),
                    fouls=stats_data.get("fouls"),
                    cards=stats_data.get("cards"),
                    penalty=stats_data.get("penalty"),
                )
            )


# ──────────── live sync ────────────

def sync_live_fixtures(db: Session) -> None:
    """实时同步: 按今天+昨天日期拉取比赛状态和比分"""
    if not settings.api_football_key:
        return

    from datetime import datetime, timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    all_items = []
    for date_str in [today, yesterday]:
        try:
            response = httpx.get(
                f"{settings.api_football_base_url}/fixtures",
                headers={"x-apisports-key": settings.api_football_key},
                params={"date": date_str},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("response", [])
            if items:
                all_items.extend(items)
        except Exception as e:
            logger.warning(f"拉取赛程失败 date={date_str}: {e}")

    if not all_items:
        return

    updated = 0
    just_finished = []  # 刚结束的比赛

    for row in all_items:
        fixture_raw = row.get("fixture", {})
        fid = fixture_raw.get("id")
        if not fid:
            continue
        league_raw = row.get("league", {})
        lid = league_raw.get("id")
        if lid and not db.query(League).filter(League.id == lid, League.enabled == True).first():
            continue

        status_raw = fixture_raw.get("status") or {}
        goals_raw = row.get("goals", {})
        score_raw = row.get("score", {})
        halftime_raw = score_raw.get("halftime") or {}

        obj = db.query(Fixture).filter(Fixture.id == fid).first()
        if obj is None:
            _upsert_fixture(db, row)
            updated += 1
        else:
            changed = False
            old_short = obj.status_short
            new_short = status_raw.get("short")
            if new_short and obj.status_short != new_short:
                obj.status_short = new_short
                obj.status_long = status_raw.get("long")
                obj.status_elapsed = status_raw.get("elapsed")
                obj.status_extra = status_raw.get("extra")
                changed = True

            gh = goals_raw.get("home")
            ga = goals_raw.get("away")
            if gh is not None and ga is not None:
                if obj.goals_home != gh or obj.goals_away != ga:
                    obj.goals_home = gh
                    obj.goals_away = ga
                    changed = True

            hh = halftime_raw.get("home")
            ha = halftime_raw.get("away")
            if hh is not None and ha is not None:
                if obj.halftime_home != hh or obj.halftime_away != ha:
                    obj.halftime_home = hh
                    obj.halftime_away = ha
                    changed = True

            if changed:
                updated += 1

            # 检测比赛刚刚结束
            finished = {"FT", "AET", "PEN", "AWD", "WO"}
            if new_short in finished and old_short not in finished:
                just_finished.append(fid)

    if updated:
        db.commit()
        logger.debug(f"实时赛程更新: {updated} 场")

    # 刚结束的比赛异步拉取子数据
    if just_finished:
        logger.info(f"检测到 {len(just_finished)} 场刚结束, 拉取子数据: {just_finished}")
        for fid in just_finished:
            try:
                _sync_one_fixture_sub_data(fid)
            except Exception as e:
                logger.warning(f"子数据同步失败 fixture={fid}: {e}")
