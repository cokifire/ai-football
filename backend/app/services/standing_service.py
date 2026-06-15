from sqlalchemy.orm import Session
from loguru import logger
import httpx

from app.core.config import settings
from app.models.league import League, Season
from app.models.standing import Standing


def sync_standings(db: Session) -> None:
    """遍历已启用联赛的当前赛季，拉取积分榜数据"""
    if not settings.api_football_key:
        logger.warning("API_FOOTBALL_KEY 未配置，跳过积分榜同步")
        return

    seasons = (
        db.query(Season)
        .join(League)
        .filter(Season.is_current == True, League.enabled == True)
        .all()
    )
    if not seasons:
        logger.warning("没有找到当前赛季，跳过积分榜同步")
        return

    for season in seasons:
        logger.info(f"拉取联赛 {season.league_id} 赛季 {season.year} 的积分榜...")

        try:
            response = httpx.get(
                f"{settings.api_football_base_url}/standings",
                headers={"x-apisports-key": settings.api_football_key},
                params={"league": season.league_id, "season": season.year},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"拉取联赛 {season.league_id} 积分榜失败: {e}")
            continue

        for entry in data.get("response", []):
            league_info = entry.get("league", {})
            lid = league_info.get("id") or season.league_id
            stat_season = league_info.get("season") or season.year

            # standings 是二维数组: [[group1...], [group2...]]
            for group in league_info.get("standings", []):
                for row in group:
                    team = row.get("team") or {}
                    all_stats = row.get("all") or {}
                    home_stats = row.get("home") or {}
                    away_stats = row.get("away") or {}

                    team_id = team.get("id")
                    if not team_id:
                        continue

                    # 按 (league_id, season, group_name, team_id) 查找
                    standing = (
                        db.query(Standing)
                        .filter(
                            Standing.league_id == lid,
                            Standing.season == stat_season,
                            Standing.group_name == row.get("group"),
                            Standing.team_id == team_id,
                        )
                        .first()
                    )

                    if standing:
                        _update_standing(standing, row, team, all_stats, home_stats, away_stats)
                    else:
                        db.add(
                            _make_standing(
                                lid, stat_season, row, team, all_stats, home_stats, away_stats
                            )
                        )

        db.commit()

    logger.info("积分榜同步完成")


def _make_standing(lid, stat_season, row, team, all_stats, home_stats, away_stats):
    return Standing(
        league_id=lid,
        season=stat_season,
        group_name=row.get("group"),
        rank=row.get("rank"),
        team_id=team.get("id"),
        team_name=team.get("name", ""),
        team_logo=team.get("logo", ""),
        points=row.get("points"),
        goals_diff=row.get("goalsDiff"),
        form=row.get("form"),
        status=row.get("status"),
        description=row.get("description"),
        all_played=all_stats.get("played"),
        all_win=all_stats.get("win"),
        all_draw=all_stats.get("draw"),
        all_lose=all_stats.get("lose"),
        all_goals_for=(all_stats.get("goals") or {}).get("for"),
        all_goals_against=(all_stats.get("goals") or {}).get("against"),
        home_played=home_stats.get("played"),
        home_win=home_stats.get("win"),
        home_draw=home_stats.get("draw"),
        home_lose=home_stats.get("lose"),
        home_goals_for=(home_stats.get("goals") or {}).get("for"),
        home_goals_against=(home_stats.get("goals") or {}).get("against"),
        away_played=away_stats.get("played"),
        away_win=away_stats.get("win"),
        away_draw=away_stats.get("draw"),
        away_lose=away_stats.get("lose"),
        away_goals_for=(away_stats.get("goals") or {}).get("for"),
        away_goals_against=(away_stats.get("goals") or {}).get("against"),
    )


def _update_standing(standing, row, team, all_stats, home_stats, away_stats):
    standing.rank = row.get("rank")
    standing.team_name = team.get("name", "")
    standing.team_logo = team.get("logo", "")
    standing.points = row.get("points")
    standing.goals_diff = row.get("goalsDiff")
    standing.form = row.get("form")
    standing.status = row.get("status")
    standing.description = row.get("description")
    standing.all_played = all_stats.get("played")
    standing.all_win = all_stats.get("win")
    standing.all_draw = all_stats.get("draw")
    standing.all_lose = all_stats.get("lose")
    standing.all_goals_for = (all_stats.get("goals") or {}).get("for")
    standing.all_goals_against = (all_stats.get("goals") or {}).get("against")
    standing.home_played = home_stats.get("played")
    standing.home_win = home_stats.get("win")
    standing.home_draw = home_stats.get("draw")
    standing.home_lose = home_stats.get("lose")
    standing.home_goals_for = (home_stats.get("goals") or {}).get("for")
    standing.home_goals_against = (home_stats.get("goals") or {}).get("against")
    standing.away_played = away_stats.get("played")
    standing.away_win = away_stats.get("win")
    standing.away_draw = away_stats.get("draw")
    standing.away_lose = away_stats.get("lose")
    standing.away_goals_for = (away_stats.get("goals") or {}).get("for")
    standing.away_goals_against = (away_stats.get("goals") or {}).get("against")
