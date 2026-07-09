"""
批量拉取历届世界杯 (league_id=1) 积分榜数据
用法: python fetch_wc_standings.py
"""

import sys
import io
import os
import time

# 强制 stdout UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 确保 backend 目录在 sys.path
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

import httpx
from dotenv import load_dotenv

load_dotenv()

from app.db.session import SessionLocal
from app.models.standing import Standing
from app.core.config import settings
from sqlalchemy import text

API_KEY = settings.api_football_key
BASE_URL = settings.api_football_base_url
LEAGUE_ID = 1  # 世界杯

print("=" * 70)
print(f"历届世界杯积分榜拉取脚本")
print(f"API: {BASE_URL}")
print(f"联赛: World Cup (league_id={LEAGUE_ID})")
print("=" * 70)


def fetch_standings(season: int) -> list | None:
    """调用 API 拉取指定赛季的积分榜原始数据"""
    url = f"{BASE_URL}/standings"
    headers = {"x-apisports-key": API_KEY}
    params = {"league": LEAGUE_ID, "season": season}

    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=30.0)
        remaining = resp.headers.get("x-ratelimit-requests-remaining", "N/A")
        limit = resp.headers.get("x-ratelimit-requests-limit", "N/A")

        if resp.status_code != 200:
            print(f"  ❌ HTTP {resp.status_code} (配额 {remaining}/{limit})")
            print(f"     响应: {resp.text[:300]}")
            return None

        data = resp.json()
        errors = data.get("errors", [])
        if errors:
            print(f"  ⚠️  API 错误: {errors}")
            return None

        response_list = data.get("response", [])
        results = data.get("results", 0)
        print(f"  ✅ results={results}, 配额 {remaining}/{limit}")
        return response_list

    except Exception as e:
        print(f"  ❌ 异常: {type(e).__name__}: {e}")
        return None


def make_standing(lid, stat_season, row, team, all_stats, home_stats, away_stats):
    """参照 standing_service._make_standing()"""
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


def update_standing(standing, row, team, all_stats, home_stats, away_stats):
    """参照 standing_service._update_standing()"""
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


def save_standings(db, season: int, api_response: list) -> int:
    """将 API 返回的积分榜数据 upsert 到数据库，返回插入/更新行数"""
    upserted = 0
    for entry in api_response:
        league_info = entry.get("league", {})
        lid = league_info.get("id") or LEAGUE_ID
        stat_season = league_info.get("season") or season

        for group in league_info.get("standings", []):
            for row in group:
                team = row.get("team") or {}
                all_stats = row.get("all") or {}
                home_stats = row.get("home") or {}
                away_stats = row.get("away") or {}

                team_id = team.get("id")
                if not team_id:
                    continue

                existing = (
                    db.query(Standing)
                    .filter(
                        Standing.league_id == lid,
                        Standing.season == stat_season,
                        Standing.group_name == row.get("group"),
                        Standing.team_id == team_id,
                    )
                    .first()
                )

                if existing:
                    update_standing(existing, row, team, all_stats, home_stats, away_stats)
                else:
                    db.add(make_standing(lid, stat_season, row, team, all_stats, home_stats, away_stats))

                upserted += 1
    db.commit()
    return upserted


def main():
    db = SessionLocal()
    try:
        # 1. 查询世界杯所有赛季
        seasons = db.execute(
            text("SELECT year FROM seasons WHERE league_id = :lid ORDER BY year DESC"),
            {"lid": LEAGUE_ID},
        ).fetchall()

        if not seasons:
            print("\n❌ 数据库中没有世界杯赛季记录，请先运行 sync_leagues !")
            return

        season_years = [s[0] for s in seasons]
        print(f"\n数据库中共有 {len(season_years)} 个世界杯赛季:")
        print(", ".join(str(y) for y in season_years))
        print()

        # 2. 逐个赛季拉取
        total_inserted = 0
        success_count = 0
        fail_count = 0

        for i, year in enumerate(season_years, 1):
            print(f"[{i}/{len(season_years)}] 拉取 {year} 年世界杯积分榜...")

            api_data = fetch_standings(year)
            if api_data is None:
                fail_count += 1
                continue

            upserted = save_standings(db, year, api_data)
            total_inserted += upserted
            success_count += 1

            print(f"  📊 写入 {upserted} 条积分榜记录")
            time.sleep(1.5)  # 限速，避免触发 API 限制

        # 3. 汇总
        print("\n" + "=" * 70)
        print("拉取完成!")
        print(f"  成功: {success_count} 个赛季")
        print(f"  失败: {fail_count} 个赛季")
        print(f"  写入总计: {total_inserted} 条积分榜记录")
        print("=" * 70)

        # 4. 验证
        print("\n📋 当前数据库中世界杯积分榜汇总:")
        rows = db.execute(
            text(
                "SELECT season, COUNT(*) AS cnt FROM standings WHERE league_id = :lid GROUP BY season ORDER BY season DESC"
            ),
            {"lid": LEAGUE_ID},
        ).fetchall()
        for r in rows:
            print(f"  {r[0]}: {r[1]} 行")

    finally:
        db.close()


if __name__ == "__main__":
    main()
