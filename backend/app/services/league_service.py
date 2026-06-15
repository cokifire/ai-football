from datetime import date
from sqlalchemy.orm import Session
from loguru import logger
import httpx

from app.core.config import settings
from app.core.whitelist import WHITELIST_LEAGUE_IDS
from app.models.league import League, Season


def parse_coverage(raw: dict) -> dict:
    """将 API 返回的 coverage 对象展平存储"""
    fixtures = raw.get("fixtures", {})
    return {
        "events": fixtures.get("events", False),
        "lineups": fixtures.get("lineups", False),
        "statistics_fixtures": fixtures.get("statistics_fixtures", False),
        "statistics_players": fixtures.get("statistics_players", False),
        "standings": raw.get("standings", False),
        "players": raw.get("players", False),
        "top_scorers": raw.get("top_scorers", False),
        "top_assists": raw.get("top_assists", False),
        "top_cards": raw.get("top_cards", False),
        "injuries": raw.get("injuries", False),
        "predictions": raw.get("predictions", False),
        "odds": raw.get("odds", False),
    }


def sync_leagues(db: Session) -> None:
    """从 API-Football 拉取联赛数据并写入数据库"""
    if not settings.api_football_key:
        logger.warning("API_FOOTBALL_KEY 未配置，跳过联赛同步")
        return

    logger.info("开始同步联赛数据...")

    try:
        response = httpx.get(
            f"{settings.api_football_base_url}/leagues",
            headers={"x-apisports-key": settings.api_football_key},
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"拉取联赛数据失败: {e}")
        return

    league_count = 0
    season_count = 0

    for item in data.get("response", []):
        league_raw = item.get("league", {})
        country_raw = item.get("country", {})

        # 联赛 upsert
        league = db.query(League).get(league_raw["id"])
        if league is None:
            league = League(id=league_raw["id"])
            db.add(league)
            league_count += 1

        league.name = league_raw.get("name", "")
        league.type = league_raw.get("type", "")
        league.logo = league_raw.get("logo", "")
        league.country_name = country_raw.get("name", "")
        league.country_code = country_raw.get("code", "")
        league.country_flag = country_raw.get("flag", "")
        # 白名单自动启用，已启用的保持不变
        if league_raw["id"] in WHITELIST_LEAGUE_IDS:
            league.enabled = True

        # 赛季 upsert
        for season_raw in item.get("seasons", []):
            season = (
                db.query(Season)
                .filter(
                    Season.league_id == league_raw["id"],
                    Season.year == season_raw["year"],
                )
                .first()
            )
            if season is None:
                season = Season(
                    league_id=league_raw["id"],
                    year=season_raw["year"],
                )
                db.add(season)
                season_count += 1

            season.start_date = _parse_date(season_raw.get("start"))
            season.end_date = _parse_date(season_raw.get("end"))
            season.is_current = season_raw.get("current", False)
            season.coverage = parse_coverage(season_raw.get("coverage", {}))

    db.commit()
    logger.info(f"联赛同步完成: 联赛 {league_count} 新增, 赛季 {season_count} 新增")


def _parse_date(value: str | None) -> date | None:
    """解析日期字符串，失败返回 None"""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None
