"""
一次性数据修复脚本：
World Cup 2026 (league_id=1, season=2026) 的 fixtures 中存在 10 场使用旧
api-football team_id（如 15=Switzerland、8=Colombia）的历史数据，而这些队的
积分榜记录在 standings 表中是以 FIFA team_id（43971/43926 等）存储的，
导致 predict.py 提取不到积分榜特征、XGBoost 输出失真。

本脚本为这些旧 team_id 在 standings 表补上 league_id=1, season=2026 的记录
（从对应 FIFA 队复制），使特征提取完整。幂等：已存在则跳过。
"""
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from app.db.session import SessionLocal
from sqlalchemy import text

LEAGUE_ID = 1
SEASON = 2026

db = SessionLocal()
try:
    # 1. 收集 WC2026 fixtures 中使用的所有"旧" team_id（对应中文 standings 缺失的）
    rows = db.execute(text("""
        SELECT DISTINCT home_id AS tid FROM fixtures
        WHERE league_id=:lid AND season=:s AND home_id IS NOT NULL
        UNION
        SELECT DISTINCT away_id AS tid FROM fixtures
        WHERE league_id=:lid AND season=:s AND away_id IS NOT NULL
    """), {"lid": LEAGUE_ID, "s": SEASON}).fetchall()
    fixture_tids = {r[0] for r in rows}

    # 2. 有效(有WC2026 standings)的 FIFA team_id -> standings 数据
    fifa_rows = db.execute(text("""
        SELECT team_id, team_name, team_logo, `rank`, points, goals_diff,
               form, status, description,
               all_played, all_win, all_draw, all_lose, all_goals_for, all_goals_against,
               home_played, home_win, home_draw, home_lose, home_goals_for, home_goals_against,
               away_played, away_win, away_draw, away_lose, away_goals_for, away_goals_against
        FROM standings WHERE league_id=:lid AND season=:s
    """), {"lid": LEAGUE_ID, "s": SEASON}).fetchall()

    # 用 English name 建映射: teams.name -> fifa standings 行
    name_to_fifa = {}
    for fr in fifa_rows:
        t = db.execute(text("SELECT name, name_zh FROM teams WHERE id=:i"),
                       {"i": fr[0]}).fetchone()
        if t:
            name_to_fifa[t[0]] = fr  # 以英文名为 key

    cols = ["team_id","team_name","team_logo","rank","points","goals_diff","form","status",
            "description","all_played","all_win","all_draw","all_lose","all_goals_for",
            "all_goals_against","home_played","home_win","home_draw","home_lose",
            "home_goals_for","home_goals_against","away_played","away_win","away_draw",
            "away_lose","away_goals_for","away_goals_against"]

    inserted = 0
    skipped = 0
    for tid in sorted(fixture_tids):
        # 已存在则跳过
        exists = db.execute(text(
            "SELECT 1 FROM standings WHERE team_id=:t AND league_id=:lid AND season=:s LIMIT 1"),
            {"t": tid, "lid": LEAGUE_ID, "s": SEASON}).fetchone()
        if exists:
            skipped += 1
            continue
        t = db.execute(text("SELECT id, name, name_zh FROM teams WHERE id=:i"),
                       {"i": tid}).fetchone()
        if not t:
            print(f"  team_id={tid} 在 teams 表不存在，跳过")
            continue
        fifa = name_to_fifa.get(t[1])
        if not fifa:
            print(f"  team_id={tid} ({t[1]}) 未找到对应 FIFA standings，跳过")
            continue
        vals = dict(zip(cols, fifa))
        vals["team_id"] = tid
        vals["team_name"] = t[2] or t[1]  # 用中文名保持一致性
        quoted_cols = ', '.join('`' + c + '`' for c in cols)
        db.execute(text(f"""
            INSERT INTO standings (league_id, season, group_name, {quoted_cols})
            VALUES (:lid, :s, 'Group Stage', {', '.join(':'+c for c in cols)})
        """), {"lid": LEAGUE_ID, "s": SEASON, **vals})
        inserted += 1
        print(f"  插入 standings: team_id={tid} ({vals['team_name']}) from FIFA {fifa[0]}")

    db.commit()
    print(f"\n完成: 插入 {inserted} 条, 已存在跳过 {skipped} 条")
finally:
    db.close()
