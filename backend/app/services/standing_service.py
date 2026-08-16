from sqlalchemy.orm import Session
from loguru import logger
import httpx
import random
import sys
import time
from pathlib import Path

from app.core.config import settings
from app.models.league import League, Season
from app.models.standing import Standing

# 允许从 tools/ 导入 Flashscore 抓取脚本, 作为 API-Football 的备用数据源
_TOOLS_DIR = str(Path(__file__).resolve().parent.parent.parent / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

# Flashscore 连续爬取之间的随机等待区间(秒), 降低被反爬封锁的概率
FLASHSCORE_SCRAPE_INTERVAL = (5, 12)


def _sync_from_flashscore(db: Session, league_id: int) -> bool:
    """用 Flashscore 抓取并写入该联赛积分榜。返回是否成功触发抓取。"""
    try:
        from fetch_flashscore_standings import run as flashscore_run
    except Exception as e:
        logger.error(f"无法导入 Flashscore 抓取脚本, 跳过联赛 {league_id}: {e}")
        return False
    try:
        flashscore_run(league_id, db=db)
        return True
    except Exception as e:
        logger.error(f"Flashscore 同步联赛 {league_id} 失败: {e}")
        return False


def _sync_from_api_football(db: Session, season: Season) -> bool:
    """使用 API-Football 同步单个积分榜。

    这是保留的备用实现。目前积分榜同步不会调用此函数；如果未来需要
    临时切回 API-Football，可在 sync_standings() 中显式接入。
    """
    try:
        response = httpx.get(
            f"{settings.api_football_base_url}/standings",
            headers={"x-apisports-key": settings.api_football_key},
            params={"league": season.league_id, "season": season.year},
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            logger.warning(
                f"联赛 {season.league_id} 赛季 {season.year} 积分榜被 API-Football 拒绝: "
                f"{data['errors']}"
            )
            return False
    except Exception as e:
        logger.error(f"API-Football 拉取联赛 {season.league_id} 积分榜失败: {e}")
        return False

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
                    db.add(_make_standing(
                        lid, stat_season, row, team, all_stats, home_stats, away_stats
                    ))

    db.commit()
    return True


def sync_standings(db: Session) -> None:
    """遍历已启用联赛的当前赛季，只从 Flashscore 拉取积分榜。

    API-Football 相关代码保留在 _sync_from_api_football() 中作为未来备用，
    但当前同步路径不会访问 API-Football，也不依赖其 key 或 base URL。
    """
    seasons = (
        db.query(Season)
        .join(League)
        .filter(Season.is_current == True, League.enabled == True)
        .all()
    )
    if not seasons:
        logger.warning("没有找到当前赛季，跳过积分榜同步")
        return

    for index, season in enumerate(seasons):
        logger.info(
            f"从 Flashscore 拉取联赛 {season.league_id} 赛季 {season.year} 的积分榜..."
        )
        if not _sync_from_flashscore(db, season.league_id):
            logger.warning(
                f"联赛 {season.league_id} 积分榜未同步：未配置 Flashscore URL 或抓取失败"
            )
        # 多次 Flashscore 爬取之间设置随机等待, 避免被反爬封锁
        if index < len(seasons) - 1:
            wait = random.uniform(*FLASHSCORE_SCRAPE_INTERVAL)
            logger.info(f"Flashscore 爬取间隔等待 {wait:.1f}s")
            time.sleep(wait)

    logger.info("Flashscore 积分榜同步完成")


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
