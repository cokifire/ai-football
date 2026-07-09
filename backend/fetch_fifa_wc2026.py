"""
从 FIFA 官方 API 拉取 2026 世界杯比赛数据和积分榜
数据源: https://api.fifa.com/api/v3/calendar/matches
用法: python fetch_fifa_wc2026.py
"""

import sys
import io
import os
import json
import time
from datetime import datetime
from collections import defaultdict

# 强制 stdout UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 确保 backend 目录在 sys.path
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

import requests
from dotenv import load_dotenv

load_dotenv()

from app.db.session import SessionLocal
from app.models.fixture import Fixture
from app.models.standing import Standing

# ─── FIFA API 配置 ───
FIFA_API_BASE = "https://api.fifa.com/api/v3"
COMPETITION_ID = 17       # FIFA World Cup
SEASON_ID = 285023        # 2026 赛季
LEAGUE_ID = 1             # 数据库中世界杯的 league_id
SEASON_YEAR = 2026

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://www.fifa.com",
    "Referer": "https://www.fifa.com/",
}

# ─── 2026 世界杯球队英→中映射表 ───
# FIFA API 返回英文名 → 中文名
TEAM_NAME_ZH: dict[str, str] = {
    "Algeria": "阿尔及利亚",
    "Argentina": "阿根廷",
    "Australia": "澳大利亚",
    "Austria": "奥地利",
    "Belgium": "比利时",
    "Bosnia and Herzegovina": "波黑",
    "Brazil": "巴西",
    "Cabo Verde": "佛得角",
    "Canada": "加拿大",
    "Colombia": "哥伦比亚",
    "Congo DR": "刚果民主共和国",
    "Côte d'Ivoire": "科特迪瓦",
    "Croatia": "克罗地亚",
    "Curaçao": "库拉索",
    "Czechia": "捷克",
    "Ecuador": "厄瓜多尔",
    "Egypt": "埃及",
    "England": "英格兰",
    "France": "法国",
    "Germany": "德国",
    "Ghana": "加纳",
    "Haiti": "海地",
    "IR Iran": "伊朗",
    "Iran": "伊朗",
    "Iraq": "伊拉克",
    "Japan": "日本",
    "Jordan": "约旦",
    "Korea Republic": "韩国",
    "South Korea": "韩国",
    "Mexico": "墨西哥",
    "Morocco": "摩洛哥",
    "Netherlands": "荷兰",
    "New Zealand": "新西兰",
    "Norway": "挪威",
    "Panama": "巴拿马",
    "Paraguay": "巴拉圭",
    "Portugal": "葡萄牙",
    "Qatar": "卡塔尔",
    "Saudi Arabia": "沙特阿拉伯",
    "Scotland": "苏格兰",
    "Senegal": "塞内加尔",
    "South Africa": "南非",
    "Spain": "西班牙",
    "Sweden": "瑞典",
    "Switzerland": "瑞士",
    "Tunisia": "突尼斯",
    "Türkiye": "土耳其",
    "Turkey": "土耳其",
    "Uruguay": "乌拉圭",
    "USA": "美国",
    "United States": "美国",
    "Uzbekistan": "乌兹别克斯坦",
}


def _team_zh(english_name: str) -> str:
    """将英文队名转为中文，找不到则返回原文"""
    if not english_name:
        return english_name
    # 先精确匹配
    if english_name in TEAM_NAME_ZH:
        return TEAM_NAME_ZH[english_name]
    # 再尝试 strip / title 后匹配
    stripped = english_name.strip()
    if stripped in TEAM_NAME_ZH:
        return TEAM_NAME_ZH[stripped]
    return english_name


# ─── 工具函数 ───

def _desc(obj, default=""):
    """从 FIFA 多语言数组中提取 en-GB 的 Description"""
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item.get("Locale") == "en-GB":
                return item.get("Description", default)
    return default


def _int_or_none(val):
    """安全转整数"""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def fetch_matches() -> list:
    """从 FIFA API 获取所有 2026 世界杯比赛"""
    all_matches = []
    page = 1
    while True:
        params = {
            "language": "en",
            "count": 200,
            "page": page,
            "IdCompetition": COMPETITION_ID,
            "IdSeason": SEASON_ID,
        }
        try:
            resp = requests.get(
                f"{FIFA_API_BASE}/calendar/matches",
                headers=HEADERS,
                params=params,
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ❌ 请求失败 (page={page}): {e}")
            break

        results = data.get("Results", [])
        if not results:
            break

        all_matches.extend(results)
        print(f"  📄 page={page}: {len(results)} 场比赛 (累计 {len(all_matches)})")

        # 检查是否还有更多页
        total = data.get("TotalResults", 0)
        if len(all_matches) >= total:
            break
        page += 1
        time.sleep(0.3)

    return all_matches


def map_status(match: dict) -> str:
    """将 FIFA 比赛状态映射为短码"""
    home_score = match.get("HomeTeamScore")
    away_score = match.get("AwayTeamScore")

    # 有比分 → 已完赛
    if home_score is not None and away_score is not None:
        home_pen = match.get("HomeTeamPenaltyScore")
        away_pen = match.get("AwayTeamPenaltyScore")
        if home_pen is not None and away_pen is not None:
            return "PEN"  # 点球决胜
        return "FT"

    # 无比分 → 未开始或进行中
    match_status = match.get("MatchStatus")
    if match_status == 3:
        return "1H"  # 进行中
    return "NS"  # 未开始


def upsert_fixture(db, match: dict) -> Fixture | None:
    """将一场 FIFA 比赛写入 fixtures 表"""
    fid = _int_or_none(match.get("IdMatch"))
    if not fid:
        return None

    home = match.get("Home") or {}
    away = match.get("Away") or {}
    stadium = match.get("Stadium") or {}

    home_name_en = _desc(home.get("TeamName"), home.get("ShortClubName", ""))
    away_name_en = _desc(away.get("TeamName"), away.get("ShortClubName", ""))
    home_name = _team_zh(home_name_en)
    away_name = _team_zh(away_name_en)
    home_id = _int_or_none(home.get("IdTeam"))
    away_id = _int_or_none(away.get("IdTeam"))

    goals_home = _int_or_none(match.get("HomeTeamScore"))
    goals_away = _int_or_none(match.get("AwayTeamScore"))
    status_short = map_status(match)

    # 确定 winner
    home_winner = None
    away_winner = None
    if goals_home is not None and goals_away is not None:
        if goals_home > goals_away:
            home_winner = True
            away_winner = False
        elif goals_away > goals_home:
            home_winner = False
            away_winner = True
        else:
            # 平局，看点球
            home_pen = _int_or_none(match.get("HomeTeamPenaltyScore"))
            away_pen = _int_or_none(match.get("AwayTeamPenaltyScore"))
            if home_pen is not None and away_pen is not None:
                if home_pen > away_pen:
                    home_winner = True
                    away_winner = False
                else:
                    home_winner = False
                    away_winner = True

    # 轮次信息
    stage_name = _desc(match.get("StageName"), "")
    group_name = _desc(match.get("GroupName"), "")
    round_name = group_name if group_name else stage_name

    # 日期
    date_str = match.get("Date")
    fixture_date = None
    if date_str:
        try:
            # 处理 ISO 格式: "2026-06-11T19:00:00Z"
            date_str_clean = date_str.replace("Z", "+00:00")
            fixture_date = datetime.fromisoformat(date_str_clean)
        except (ValueError, TypeError):
            pass

    obj = db.query(Fixture).filter(Fixture.id == fid).first()
    is_new = obj is None
    if is_new:
        obj = Fixture(id=fid)
        db.add(obj)

    obj.date = fixture_date
    obj.league_id = LEAGUE_ID
    obj.league_name = "FIFA World Cup"
    obj.season = SEASON_YEAR
    obj.round = round_name
    obj.home_id = home_id
    obj.home_name = home_name
    obj.home_logo = _desc(home.get("TeamName")) or home_name
    obj.home_winner = home_winner
    obj.away_id = away_id
    obj.away_name = away_name
    obj.away_logo = _desc(away.get("TeamName")) or away_name
    obj.away_winner = away_winner
    obj.goals_home = goals_home
    obj.goals_away = goals_away
    obj.status_short = status_short
    obj.status_long = "Match Finished" if status_short in ("FT", "PEN") else "Not Started"
    obj.venue_id = _int_or_none(stadium.get("IdStadium"))
    obj.venue_name = _desc(stadium.get("Name"), "")
    obj.venue_city = _desc(stadium.get("CityName"), "")

    # 分类标签 (VARCHAR(20) 限制)
    category_raw = group_name if group_name else stage_name
    # 缩写过长的 stage 名称
    CATEGORY_ABBREV = {
        "Round of 32": "Round of 32",
        "Round of 16": "Round of 16",
        "Quarter-final": "Quarter-final",
        "Semi-final": "Semi-final",
        "Play-off for third place": "3rd Place",
        "Final": "Final",
    }
    obj.category = (CATEGORY_ABBREV.get(category_raw) or category_raw)[:20]

    # 标记子数据未同步
    if is_new:
        obj.sub_data_synced = False

    return obj


# ─── 积分榜推导 ───

def compute_standings(matches: list) -> list:
    """根据小组赛比赛结果推导积分榜"""
    # 只处理 First Stage (小组赛) 的比赛
    group_matches = defaultdict(list)
    for m in matches:
        stage = _desc(m.get("StageName"), "")
        group = _desc(m.get("GroupName"), "")
        if stage == "First Stage" and group:
            group_matches[group].append(m)

    standings = []
    for group_name in sorted(group_matches.keys()):
        group_standings = _compute_group_standings(group_name, group_matches[group_name])
        standings.extend(group_standings)

    return standings


def _compute_group_standings(group_name: str, matches: list) -> list:
    """计算单个小组的积分榜"""
    # 球队数据: team_id -> {name, played, win, draw, lose, gf, ga}
    teams = {}

    def get_or_create_team(team_data: dict) -> dict:
        tid = _int_or_none(team_data.get("IdTeam"))
        tname_en = _desc(team_data.get("TeamName"), team_data.get("ShortClubName", ""))
        tname = _team_zh(tname_en)
        if tid not in teams:
            teams[tid] = {
                "team_id": tid,
                "team_name": tname,
                "team_logo": team_data.get("PictureUrl", ""),
                "played": 0, "win": 0, "draw": 0, "lose": 0,
                "goals_for": 0, "goals_against": 0,
            }
        return teams[tid]

    for m in matches:
        home = m.get("Home") or {}
        away = m.get("Away") or {}
        h_score = _int_or_none(m.get("HomeTeamScore"))
        a_score = _int_or_none(m.get("AwayTeamScore"))

        if h_score is None or a_score is None:
            continue  # 跳过未进行的比赛

        ht = get_or_create_team(home)
        at = get_or_create_team(away)

        ht["played"] += 1
        at["played"] += 1
        ht["goals_for"] += h_score
        ht["goals_against"] += a_score
        at["goals_for"] += a_score
        at["goals_against"] += h_score

        if h_score > a_score:
            ht["win"] += 1
            at["lose"] += 1
        elif a_score > h_score:
            at["win"] += 1
            ht["lose"] += 1
        else:
            ht["draw"] += 1
            at["draw"] += 1

    # 排序: 积分 → 净胜球 → 进球数
    sorted_teams = sorted(teams.values(), key=lambda t: (
        -(t["win"] * 3 + t["draw"]),
        -(t["goals_for"] - t["goals_against"]),
        -t["goals_for"],
    ))

    result = []
    for rank, t in enumerate(sorted_teams, 1):
        result.append({
            "league_id": LEAGUE_ID,
            "season": SEASON_YEAR,
            "group_name": group_name,
            "rank": rank,
            "team_id": t["team_id"],
            "team_name": t["team_name"],
            "team_logo": t["team_logo"],
            "points": t["win"] * 3 + t["draw"],
            "goals_diff": t["goals_for"] - t["goals_against"],
            "form": "",  # FIFA API 不提供近期状态
            "status": "same",
            "description": "",
            "all_played": t["played"],
            "all_win": t["win"],
            "all_draw": t["draw"],
            "all_lose": t["lose"],
            "all_goals_for": t["goals_for"],
            "all_goals_against": t["goals_against"],
        })

    return result


def save_standings(db, standings_data: list) -> int:
    """将积分榜数据 upsert 到数据库"""
    count = 0
    for s in standings_data:
        existing = (
            db.query(Standing)
            .filter(
                Standing.league_id == s["league_id"],
                Standing.season == s["season"],
                Standing.group_name == s["group_name"],
                Standing.team_id == s["team_id"],
            )
            .first()
        )

        if existing:
            # 更新
            for key in ["rank", "team_name", "team_logo", "points", "goals_diff",
                        "form", "status", "description",
                        "all_played", "all_win", "all_draw", "all_lose",
                        "all_goals_for", "all_goals_against"]:
                setattr(existing, key, s.get(key))
        else:
            db.add(Standing(**s))

        count += 1

    db.commit()
    return count


# ─── 主流程 ───

def main():
    print("=" * 70)
    print("FIFA 2026 世界杯数据拉取脚本")
    print(f"数据源: {FIFA_API_BASE}/calendar/matches")
    print(f"Competition={COMPETITION_ID}, Season={SEASON_ID}")
    print("=" * 70)

    # 1. 拉取比赛
    print("\n⏳ 拉取比赛中...")
    matches = fetch_matches()
    if not matches:
        print("  ❌ 未获取到比赛数据！")
        return

    print(f"\n✅ 共获取 {len(matches)} 场比赛")

    # 统计
    stages = defaultdict(int)
    groups = set()
    statuses = defaultdict(int)
    for m in matches:
        st = _desc(m.get("StageName"), "?")
        grp = _desc(m.get("GroupName"), "")
        stages[st] += 1
        if grp:
            groups.add(grp)
        statuses[map_status(m)] += 1

    print(f"  阶段分布: {dict(stages)}")
    print(f"  小组数: {len(groups)} → {sorted(groups)}")
    print(f"  状态分布: {dict(statuses)}")

    # 2. 清理旧数据
    db = SessionLocal()
    try:
        print("\n⏳ 清理旧的 2026 WC 数据...")
        from sqlalchemy import text
        # 删除旧的 fixtures (league_id=1, season=2026)
        del_f = db.execute(text(
            "DELETE FROM fixtures WHERE league_id=:lid AND season=:s"
        ), {"lid": LEAGUE_ID, "s": SEASON_YEAR})
        # 删除旧的 standings (league_id=1, season=2026)
        del_s = db.execute(text(
            "DELETE FROM standings WHERE league_id=:lid AND season=:s"
        ), {"lid": LEAGUE_ID, "s": SEASON_YEAR})
        db.commit()
        print(f"  已清除 fixtures {del_f.rowcount} 条, standings {del_s.rowcount} 条")

        print("\n⏳ 写入 fixtures 表...")
        fixture_count = 0
        for m in matches:
            obj = upsert_fixture(db, m)
            if obj:
                fixture_count += 1
        db.commit()
        print(f"✅ fixtures 写入/更新: {fixture_count} 条")

        # 3. 同步 teams 表 (把 FIFA 球队写入 teams 表并设置 name_zh)
        print("\n⏳ 同步 teams 表...")
        from app.models.team import Team
        team_ids_added = 0
        team_zh_updated = 0
        # 收集所有出现过的球队
        seen_teams: dict[int, tuple[str, str]] = {}  # id -> (en_name, zh_name)
        for m in matches:
            for side in ("Home", "Away"):
                t = (m.get(side) or {})
                tid = _int_or_none(t.get("IdTeam"))
                tname_en = _desc(t.get("TeamName"), t.get("ShortClubName", ""))
                if tid and tname_en:
                    seen_teams[tid] = (tname_en, _team_zh(tname_en))

        for tid, (en_name, zh_name) in seen_teams.items():
            team = db.query(Team).filter(Team.id == tid).first()
            if team is None:
                db.add(Team(
                    id=tid,
                    name=en_name,
                    name_zh=zh_name,
                    national=True,
                ))
                team_ids_added += 1
            elif not team.name_zh or team.name_zh != zh_name:
                team.name_zh = zh_name
                if not team.name:
                    team.name = en_name
                team.national = True
                team_zh_updated += 1
        db.commit()
        print(f"✅ teams 新增 {team_ids_added} 支, 更新中文名 {team_zh_updated} 支")

        # 4. 推导积分榜
        print("\n⏳ 推导小组积分榜...")
        standings_data = compute_standings(matches)
        if standings_data:
            sc = save_standings(db, standings_data)
            print(f"✅ standings 写入/更新: {sc} 条")
        else:
            print("  ⚠️ 无小组赛数据，跳过积分榜")

        # 5. 汇总
        print("\n" + "=" * 70)
        print("导入完成！")

        # 验证
        from sqlalchemy import text
        f_cnt = db.execute(text(
            "SELECT COUNT(*) FROM fixtures WHERE league_id=:lid AND season=:s"
        ), {"lid": LEAGUE_ID, "s": SEASON_YEAR}).scalar()
        s_cnt = db.execute(text(
            "SELECT COUNT(*) FROM standings WHERE league_id=:lid AND season=:s"
        ), {"lid": LEAGUE_ID, "s": SEASON_YEAR}).scalar()
        print(f"  2026 WC fixtures: {f_cnt} 行")
        print(f"  2026 WC standings: {s_cnt} 行")

        if s_cnt > 0:
            print("\n📋 积分榜预览:")
            rows = db.execute(text(
                "SELECT group_name, team_name, points, goals_diff, all_played,"
                " all_win, all_draw, all_lose, all_goals_for, all_goals_against "
                "FROM standings WHERE league_id=:lid AND season=:s "
                "ORDER BY group_name, `rank`"
            ), {"lid": LEAGUE_ID, "s": SEASON_YEAR}).fetchall()
            for r in rows:
                print(f"  {r[0]:10s} | {r[1]:25s} | Pts:{r[2]:2d} GD:{r[3]:3d} "
                      f"P:{r[4]} W:{r[5]} D:{r[6]} L:{r[7]} GF:{r[8]} GA:{r[9]}")

        print("=" * 70)

    except Exception as e:
        db.rollback()
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


if __name__ == "__main__":
    main()
