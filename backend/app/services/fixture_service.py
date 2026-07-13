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

# TLS 握手/连接超时重试配置
API_CONNECT_TIMEOUT = 10.0   # 连接(含 TLS 握手)超时
API_READ_TIMEOUT = 30.0      # 读取响应超时
API_RETRY_MAX = 3            # 瞬时故障重试次数
API_RETRY_BASE_DELAY = 1.5   # 退避基数(秒)


def _api_get_http(endpoint: str, params: dict) -> httpx.Response:
    """带 TLS/连接超时重试的 API GET 请求 (返回 httpx.Response)

    仅对瞬时故障重试: TLS 握手超时、连接错误、网络中断、以及
    429/5xx 等服务端瞬时错误。HTTP 4xx 业务错误(如 401/403)不重试。
    """
    url = f"{settings.api_football_base_url}/{endpoint}"
    headers = {"x-apisports-key": settings.api_football_key}
    timeout = httpx.Timeout(
        connect=API_CONNECT_TIMEOUT,
        read=API_READ_TIMEOUT,
        write=API_CONNECT_TIMEOUT,
        pool=API_CONNECT_TIMEOUT,
    )
    last_err: Exception | None = None
    for attempt in range(API_RETRY_MAX):
        try:
            r = httpx.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = API_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"API {r.status_code} 瞬时错误 {endpoint}, {wait:.1f}s 后重试 #{attempt+1}"
                )
                time.sleep(wait)
                continue
            return r
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = e
            wait = API_RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(f"API 请求失败 {endpoint}: {e}, {wait:.1f}s 后重试 #{attempt+1}")
            time.sleep(wait)
    # 重试耗尽, 抛出最后一次错误(让调用方按现有逻辑处理)
    raise last_err or httpx.TransportError(f"API 请求失败 {endpoint}")

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


def _is_free_plan_error(data: dict) -> bool:
    """检测 API 返回的免费版限制错误"""
    errors = data.get("errors", {})
    if isinstance(errors, dict):
        plan_msg = errors.get("plan", "")
        if isinstance(plan_msg, str) and "free plans" in plan_msg.lower():
            return True
    return False


def _sync_one_league_by_season(db: Session, league_id: int, season_year: int) -> tuple[int, dict | None]:
    """按 league+season 拉取赛程（付费版），返回 (synced_count, plan_error_data)"""
    msg = f"联赛 {league_id} 赛季 {season_year}"
    page_num = 1
    synced = 0

    while True:
        try:
            params = {"league": str(league_id), "season": str(season_year), "page": page_num}
            response = _api_get_http("fixtures", params)
            remaining = response.headers.get("x-ratelimit-requests-remaining", "?")
            limit = response.headers.get("x-ratelimit-requests-limit", "?")
            logger.debug(f"API[{response.status_code}] fixtures league={league_id} season={season_year} page={page_num}  remaining={remaining}/{limit}")
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"拉取{msg}赛程失败 (page={page_num}): {e}")
            print(f"  {msg} page={page_num} FAILED: {e}")
            return (synced, None)

        errors = data.get("errors", [])
        if errors:
            err_msg = errors if isinstance(errors, str) else str(errors)
            logger.warning(f"API 返回错误 {msg}: {err_msg}")
            print(f"  {msg} API errors: {err_msg}")
            # 返回错误数据以便调用方判断是否是免费版限制
            if _is_free_plan_error(data):
                return (synced, data)
            return (synced, None)

        items = data.get("response", [])
        if not items:
            logger.info(f"{msg}: 无数据 (page={page_num})")
            break

        for row in items:
            _upsert_fixture(db, row)

        synced += len(items)
        db.commit()

        paging = data.get("paging", {})
        total_pages = paging.get("total") if isinstance(paging.get("total"), int) else 0
        current = paging.get("current", page_num)
        if current >= total_pages or len(items) == 0:
            break
        page_num += 1
        time.sleep(0.2)

    return (synced, None)


def _fetch_fixtures_by_params(params: dict) -> tuple[dict | None, str | None]:
    """通用 API 请求，返回 (data, error_msg)"""
    try:
        response = _api_get_http("fixtures", params)
        remaining = response.headers.get("x-ratelimit-requests-remaining", "?")
        logger.debug(f"API[{response.status_code}] fixtures params={params}  remaining={remaining}")
        response.raise_for_status()
        data = response.json()

        errors = data.get("errors", [])
        if errors:
            err_msg = errors if isinstance(errors, str) else "; ".join(
                f"{k}: {v}" for k, v in errors.items()
            ) if isinstance(errors, dict) else str(errors)
            return (None, err_msg)
        return (data, None)
    except Exception as e:
        return (None, str(e))


def _parse_free_plan_date_range(err_msg: str) -> tuple[str | None, str | None]:
    """从免费版错误信息中提取可用日期范围，如 'try from 2026-07-05 to 2026-07-07'"""
    import re
    m = re.search(r'try from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})', err_msg)
    if m:
        return (m.group(1), m.group(2))
    return (None, None)


def _sync_by_date_range(db: Session, enabled_league_ids: set[int],
                        days_back: int = 7, days_forward: int = 7) -> int:
    """免费版回退方案: 逐日查询赛程，自动检测免费版日期窗口并仅查询可用日"""
    from datetime import datetime, timedelta
    today = datetime.now()
    total_synced = 0

    # ── 步骤1: 先探测今天，确认日期窗口 ──
    allowed_dates: set[str] = set()
    today_str = today.strftime("%Y-%m-%d")
    data, err = _fetch_fixtures_by_params({"date": today_str})

    if data is None and err and "rateLimit" in err:
        time.sleep(10)
        data, err = _fetch_fixtures_by_params({"date": today_str})

    if data is not None:
        # 今天可用，尝试识别完整窗口
        allowed_dates.add(today_str)
        # 尝试昨天和明天来确定边界
        for day_offset in (-1, 1, -2, 2):
            test_date = (today + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            if test_date in allowed_dates:
                continue
            d, e = _fetch_fixtures_by_params({"date": test_date})
            if d is not None:
                allowed_dates.add(test_date)
            elif e:
                parsed_from, parsed_to = _parse_free_plan_date_range(e)
                if parsed_from and parsed_to:
                    # 用 API 建议的范围扩展
                    from_dt = datetime.strptime(parsed_from, "%Y-%m-%d")
                    to_dt = datetime.strptime(parsed_to, "%Y-%m-%d")
                    d2_dt = from_dt
                    while d2_dt <= to_dt:
                        allowed_dates.add(d2_dt.strftime("%Y-%m-%d"))
                        d2_dt += timedelta(days=1)
            time.sleep(0.15)
    elif err:
        # 今天的请求也出错，尝试解析错误中的建议范围
        parsed_from, parsed_to = _parse_free_plan_date_range(err)
        if parsed_from and parsed_to:
            from_dt = datetime.strptime(parsed_from, "%Y-%m-%d")
            to_dt = datetime.strptime(parsed_to, "%Y-%m-%d")
            d2_dt = from_dt
            while d2_dt <= to_dt:
                allowed_dates.add(d2_dt.strftime("%Y-%m-%d"))
                d2_dt += timedelta(days=1)
        else:
            logger.warning(f"无法确定可用日期范围: {err}")
            return 0

    if not allowed_dates:
        logger.warning("未找到任何可用日期，跳过赛程同步")
        return 0

    # ── 步骤2: 只请求可用日期 ──
    sorted_dates = sorted(allowed_dates)
    logger.info(f"免费版可用日期窗口: {sorted_dates[0]} ~ {sorted_dates[-1]} ({len(sorted_dates)} 天)")
    print(f"  免费版日期窗口: {sorted_dates[0]} ~ {sorted_dates[-1]} ({len(sorted_dates)} 天)")

    for i, date_str in enumerate(sorted_dates):
        # 今天已经在步骤1中获取了，跳过重复请求
        if date_str == today_str and data is not None:
            items = data.get("response", [])
        else:
            d, e = _fetch_fixtures_by_params({"date": date_str})
            if d is None:
                if e and "rateLimit" in e:
                    time.sleep(10)
                    d, e = _fetch_fixtures_by_params({"date": date_str})
                if d is None:
                    logger.warning(f"date={date_str} 跳过: {e}")
                    continue
            items = d.get("response", [])

        date_synced = 0
        for row in items:
            league_raw = row.get("league", {})
            if league_raw.get("id") in enabled_league_ids:
                _upsert_fixture(db, row)
                date_synced += 1
                total_synced += 1

        if date_synced > 0:
            db.commit()
            print(f"    {date_str}: {date_synced} 场 ✓")

        # 间隔 2.5 秒（30次/分钟限制）
        if i < len(sorted_dates) - 1:
            time.sleep(2.5)

    logger.info(f"逐日查询完成: 共 {total_synced} 场")
    return total_synced


def sync_fixtures(db: Session) -> None:
    """每日同步: 先尝试 league+season 分页，免费版则回退到日期范围拉取"""
    if not settings.api_football_key:
        logger.warning("API_FOOTBALL_KEY 未配置，跳过赛程同步")
        return

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

    enabled_league_ids = {s.league_id for s in seasons}
    total_synced = 0
    processed = 0

    for season in seasons:
        synced, plan_error = _sync_one_league_by_season(db, season.league_id, season.year)
        processed += 1

        if plan_error is not None:
            # 免费版限制，切换到日期回退方案
            logger.warning(
                f"检测到免费版 API 限制 (league={season.league_id} season={season.year})，"
                f"切换到按日期范围拉取 ({len(enabled_league_ids)} 个联赛)"
            )
            print(f"  ⚠️ 免费版限制，改用日期范围拉取...")
            # 回滚当前赛季未提交的数据
            db.rollback()
            # 按日期范围拉取所有启用联赛的赛程
            total_synced = _sync_by_date_range(db, enabled_league_ids)
            print(f"  日期范围拉取完成: {total_synced} 场")
            break
        else:
            total_synced += synced
            print(f"  [{processed}/{len(seasons)}] 联赛 {season.league_id} 赛季 {season.year}: {synced} 场")

    logger.info(f"赛程每日同步完成，共 {total_synced} 场")
    print(f"  赛程每日同步完成: {total_synced} 场")

    # 赛后子数据同步
    sync_completed_sub_data(db)


# ──────────── sub-data sync ────────────

BATCH_SIZE = 1
MAX_WORKERS = 1
RETRY_MAX = 3
API_RETRY_MAX = 3          # 单次 API 调用 429 重试次数
API_BASE_DELAY = 1.5       # API 基础延迟(秒)


def _api_get(endpoint: str, params: dict) -> dict:
    """带 429 / TLS 超时重试和退避的 API GET 请求 (返回解析后的 json dict)"""
    url = f"{settings.api_football_base_url}/{endpoint}"
    headers = {"x-apisports-key": settings.api_football_key}
    timeout = httpx.Timeout(
        connect=API_CONNECT_TIMEOUT,
        read=API_READ_TIMEOUT,
        write=API_CONNECT_TIMEOUT,
        pool=API_CONNECT_TIMEOUT,
    )
    r: httpx.Response | None = None
    for attempt in range(API_RETRY_MAX):
        try:
            r = httpx.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = API_BASE_DELAY * (2 ** attempt)
                logger.debug(f"{r.status_code} 瞬时错误, 等待 {wait:.1f}s 后重试 #{attempt+1}")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 502, 503, 504):
                wait = API_BASE_DELAY * (2 ** attempt)
                logger.debug(f"{e.response.status_code} 瞬时错误, 等待 {wait:.1f}s 后重试 #{attempt+1}")
                time.sleep(wait)
                continue
            raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            wait = API_BASE_DELAY * (2 ** attempt)
            logger.debug(f"TLS/连接错误 {endpoint}: {e}, 等待 {wait:.1f}s 后重试 #{attempt+1}")
            time.sleep(wait)
    # 所有重试均失败
    raise httpx.HTTPStatusError(
        f"请求失败 after {API_RETRY_MAX} retries for {endpoint}",
        request=r.request if r is not None else None,
        response=r if r is not None else None,
    )


def _do_fetch(db: Session, fixture_id: int) -> None:
    """串行请求 4 个 API，已存在则跳过，纯 INSERT 无 DELETE"""
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

    # 串行请求避免 429 限流
    for fn, key in tasks:
        _fetch_one(fn, key)
        time.sleep(0.5)  # 每个子请求间隔 0.5s

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
                time.sleep(3 * (2 ** attempt))  # 退避: 3s, 6s, 12s
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

        time.sleep(2.0)  # 批次间隔

    db.expire_all()
    logger.info(f"子数据同步完成 ({total_ok}/{len(ids)}, 失败 {total_fail})")


def refresh_fixture(db: Session, fixture_id: int) -> bool:
    """手动刷新单场比赛: 重新从 API-Football 拉取主表与子数据并覆盖更新。

    与每日同步不同，这里**强制覆盖**已有子数据（先删除再重新抓取），
    因此无论本地是否已有数据，都会拿到最新比分/状态/事件/阵容/统计。
    返回 True 表示主表刷新成功（子数据失败不影响主表）。
    """
    if not settings.api_football_key:
        logger.warning("API_FOOTBALL_KEY 未配置，跳过比赛刷新")
        return False

    # 1. 刷新主表（比分 / 状态 / 时间等）
    data, err = _fetch_fixtures_by_params({"id": fixture_id})
    if data is not None:
        items = data.get("response", [])
        if items:
            _upsert_fixture(db, items[0])
            db.commit()
        else:
            logger.warning(f"刷新主表: 未找到 fixture={fixture_id} 的 API 数据")
            return False
    else:
        logger.warning(f"刷新主表失败 fixture={fixture_id}: {err}")
        return False

    # 2. 清空旧子数据后重新抓取（覆盖式更新）
    for model in (FixtureEvent, FixtureLineup, FixtureStatistic, FixturePlayerStat):
        db.query(model).filter(model.fixture_id == fixture_id).delete(synchronize_session=False)
    db.commit()

    try:
        _do_fetch(db, fixture_id)
    except Exception as e:
        # 主表已更新；子数据刷新失败不影响主表展示
        logger.warning(f"刷新子数据失败 fixture={fixture_id}: {e}")

    return True


def _fetch_events(db: Session, fixture_id: int) -> None:
    try:
        data = _api_get("fixtures/events", {"fixture": fixture_id})
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
        data = _api_get("fixtures/lineups", {"fixture": fixture_id})
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
        data = _api_get("fixtures/statistics", {"fixture": fixture_id})
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
        data = _api_get("fixtures/players", {"fixture": fixture_id})
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
            response = _api_get_http("fixtures", {"date": date_str})
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
