from sqlalchemy.orm import Session
from loguru import logger
import httpx

from app.core.config import settings
from app.models.league import League, Season
from app.models.team import Team, Venue


def sync_teams(db: Session) -> None:
    """遍历已启用联赛的当前赛季，拉取球队和场馆数据"""
    if not settings.api_football_key:
        logger.warning("API_FOOTBALL_KEY 未配置，跳过球队同步")
        return

    # 找所有已启用联赛的当前赛季
    seasons = (
        db.query(Season)
        .join(League)
        .filter(Season.is_current == True, League.enabled == True)
        .all()
    )
    if not seasons:
        logger.warning("没有找到当前赛季，跳过球队同步")
        return

    for season in seasons:
        logger.info(f"拉取联赛 {season.league_id} 赛季 {season.year} 的球队...")

        try:
            response = httpx.get(
                f"{settings.api_football_base_url}/teams",
                headers={"x-apisports-key": settings.api_football_key},
                params={"league": season.league_id, "season": season.year},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"拉取联赛 {season.league_id} 球队失败: {e}")
            continue

        seen_venues = set()
        seen_teams = set()

        for item in data.get("response", []):
            team_raw = item.get("team", {})
            venue_raw = item.get("venue", {})

            # 场馆去重 merge（同联赛内多队共用球场）
            vid = venue_raw.get("id")
            if vid and vid not in seen_venues:
                seen_venues.add(vid)
                db.merge(
                    Venue(
                        id=vid,
                        name=venue_raw.get("name", ""),
                        address=venue_raw.get("address", ""),
                        city=venue_raw.get("city", ""),
                        country=venue_raw.get("country", ""),
                        capacity=venue_raw.get("capacity"),
                        surface=venue_raw.get("surface", ""),
                        image=venue_raw.get("image", ""),
                    )
                )

            # 球队去重 merge
            tid = team_raw["id"]
            if tid not in seen_teams:
                seen_teams.add(tid)
                db.merge(
                    Team(
                        id=tid,
                        name=team_raw.get("name", ""),
                        code=team_raw.get("code", ""),
                        country=team_raw.get("country", ""),
                        founded=team_raw.get("founded"),
                        national=team_raw.get("national", False),
                        logo=team_raw.get("logo", ""),
                        venue_id=vid,
                    )
                )

        db.commit()

    logger.info("球队同步完成")
