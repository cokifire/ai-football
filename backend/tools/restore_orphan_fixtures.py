"""重建孤儿 predictions 对应的 fixtures 行。

原因：fetch_fifa_wc2026.py 曾用 `DELETE FROM fixtures WHERE league_id/seasaon`
整体清空后再从源重建，导致源不再返回的比赛(如已预测过的 1489369)被删掉，
而 predictions 表无外键保护，留下孤儿记录。本脚本用 predictions 中已冗余保存的
主队/客队/联赛名、比赛时间、实际比分重建 fixtures 行，恢复数据一致性。

用法：
    cd backend && python tools/restore_orphan_fixtures.py
加 --dry-run 只报告不写入。
"""
import argparse

from sqlalchemy import text

from app.db.session import SessionLocal
from app.models.fixture import Fixture
from app.models.league import League


def _resolve_league(db, league_name: str, match_date):
    if not league_name:
        return None, None
    # 按名称(忽略大小写)匹配联赛
    row = db.execute(text(
        "SELECT id FROM leagues WHERE LOWER(name)=LOWER(:n) OR LOWER(name_zh)=LOWER(:n) LIMIT 1"
    ), {"n": league_name}).fetchone()
    if row:
        return row[0], (match_date.year if match_date else None)
    # World Cup 兜底
    if "world cup" in league_name.lower():
        return 1, (match_date.year if match_date else 2026)
    return None, (match_date.year if match_date else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        orphans = db.execute(text(
            "SELECT fixture_id, home_name, away_name, home_logo, away_logo, "
            "league_name, match_date, actual_home_goals, actual_away_goals "
            "FROM predictions p "
            "WHERE NOT EXISTS (SELECT 1 FROM fixtures f WHERE f.id = p.fixture_id)"
        )).fetchall()
        print(f"发现孤儿 predictions 记录: {len(orphans)} 条")
        if not orphans:
            return

        created = 0
        for r in orphans:
            fid = r.fixture_id
            league_id, season = _resolve_league(db, r.league_name, r.match_date)
            status = "FT" if (r.actual_home_goals is not None and r.actual_away_goals is not None) else "NS"
            obj = Fixture(id=fid)
            obj.date = r.match_date
            obj.league_id = league_id
            obj.league_name = r.league_name
            obj.season = season
            obj.home_name = r.home_name
            obj.away_name = r.away_name
            obj.home_logo = r.home_logo
            obj.away_logo = r.away_logo
            obj.status_short = status
            obj.goals_home = r.actual_home_goals
            obj.goals_away = r.actual_away_goals
            obj.category = "finished"
            obj.sub_data_synced = False
            db.add(obj)
            created += 1
            print(f"  + fixture {fid} ({r.league_name}) {r.home_name} vs {r.away_name} -> "
                  f"league_id={league_id}, status={status}")

        if args.dry_run:
            print("[dry-run] 未写入，已回滚")
            db.rollback()
        else:
            db.commit()
            print(f"✅ 已重建 {created} 条 fixtures 记录")
    finally:
        db.close()


if __name__ == "__main__":
    main()
