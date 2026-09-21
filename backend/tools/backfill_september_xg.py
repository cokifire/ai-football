"""
回填指定月份比赛的 Expected goals (xG) / Goals prevented 数据。

数据来源: Flashscore 单场 stats 页 (复用 tools/fetch_flashscore_match_xg.scrape_match_xg,
需 Playwright + 可访问 Flashscore)。结果落库到 fixture_statistics
(stat_type = 'expected_goals' / 'goals_prevented')。

约束
----
- 仅当比赛双方球队都在 flashscore_team_map.json 中找到 hash 时才可抓取 (否则跳过)。
- 仅对已结束 (status_short IN FT/AET/PEN) 的比赛抓取; 未开赛/进行中的比赛无法取得 xG。
- fixtures.date 列为北京时间 (naive), 直接按自然月筛选即 "用户视角的某月"。

用法
----
    python tools/backfill_september_xg.py --dry-run
    python tools/backfill_september_xg.py            # 实际抓取 (默认 3s 间隔)
    python tools/backfill_september_xg.py --year 2026 --month 8 --limit 5 --sleep 5
"""
import sys
import json
import time
import argparse
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from sqlalchemy import text, bindparam  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.fixture import FixtureStatistic  # noqa: E402

MAP_FILE = BACKEND / "tools" / "flashscore_team_map.json"
FINISHED_STATUSES = ("FT", "AET", "PEN")
EST_SEC_PER_MATCH = 12


def load_maps(db):
    override = json.loads(MAP_FILE.read_text(encoding="utf-8")) if MAP_FILE.exists() else {}
    id_to_hash = {tid: h for h, tid in override.items()}
    rows = db.execute(text("SELECT id, name FROM teams")).fetchall()
    id_to_name = {r[0]: r[1] for r in rows}
    hash_to_name = {h: id_to_name[tid] for tid, h in id_to_hash.items() if tid in id_to_name}
    return id_to_hash, id_to_name, hash_to_name


def find_month_fixtures(db, year, month):
    if month == 12:
        ey, em = year + 1, 1
    else:
        ey, em = year, month + 1
    start = f"{year}-{month:02d}-01 00:00:00"
    end = f"{ey}-{em:02d}-01 00:00:00"
    rows = db.execute(text(
        "SELECT id, home_id, away_id, home_name, away_name, date, status_short, league_name "
        "FROM fixtures WHERE date >= :s AND date < :e ORDER BY date"
    ), {"s": start, "e": end}).fetchall()
    return rows


def existing_xg_teams(db, fixture_ids):
    if not fixture_ids:
        return set()
    stmt = text(
        "SELECT fixture_id, team_id FROM fixture_statistics "
        "WHERE stat_type = 'expected_goals' AND fixture_id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    rows = db.execute(stmt, {"ids": fixture_ids}).fetchall()
    return {(r[0], r[1]) for r in rows}


def upsert_xg(db, fixture_id, home_id, home_name, away_id, away_name, stats):
    written = 0

    def _upsert(team_id, team_name, vals):
        nonlocal written
        for stat_type, value in (
            ("expected_goals", vals.get("xg")),
            ("goals_prevented", vals.get("goals_prevented")),
        ):
            if value is None:
                continue
            exist = (
                db.query(FixtureStatistic)
                .filter_by(fixture_id=fixture_id, team_id=team_id, stat_type=stat_type)
                .first()
            )
            if exist:
                exist.stat_value = str(value)
                exist.team_name = team_name
            else:
                db.add(FixtureStatistic(
                    fixture_id=fixture_id, team_id=team_id,
                    team_name=team_name, stat_type=stat_type, stat_value=str(value),
                ))
            written += 1

    _upsert(home_id, home_name, stats["home"])
    _upsert(away_id, away_name, stats["away"])
    db.commit()
    return written


def main():
    ap = argparse.ArgumentParser(description="回填指定月份比赛的 xG / Goals prevented")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--month", type=int, default=9)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=3.0)
    args = ap.parse_args()

    if not MAP_FILE.exists():
        print("[fatal] 未找到 flashscore_team_map.json, 无法抓取")
        sys.exit(1)

    db = SessionLocal()
    try:
        id_to_hash, id_to_name, hash_to_name = load_maps(db)
        fixtures = find_month_fixtures(db, args.year, args.month)
        fixture_ids = [f[0] for f in fixtures]
        present = existing_xg_teams(db, fixture_ids)

        need, skip_status, skip_map, already = [], [], [], []
        for f in fixtures:
            fid, hid, aid, hn, an, fdate, status, league = f
            has_home = (fid, hid) in present if hid is not None else True
            has_away = (fid, aid) in present if aid is not None else True
            if has_home and has_away:
                already.append(f)
                continue
            if status not in FINISHED_STATUSES:
                skip_status.append(f)
                continue
            if hid not in id_to_hash or aid not in id_to_hash:
                skip_map.append(f)
                continue
            need.append(f)

        print(f"\n===== {args.year}-{args.month:02d} 比赛 xG 补缺扫描 =====")
        print(f"该月比赛总数        : {len(fixtures)}")
        print(f"已有 xG (跳过)      : {len(already)}")
        print(f"未结束 (不抓)       : {len(skip_status)}")
        print(f"球队不在 flashscore : {len(skip_map)}")
        print(f"可补缺 (双方都在map): {len(need)}")

        if skip_map:
            print("\n-- 因球队不在 flashscore_team_map.json 跳过 --")
            for f in skip_map[:30]:
                fid, hid, aid, hn, an, *_ = f
                miss = []
                if hid not in id_to_hash:
                    miss.append(f"{hn}(id={hid})")
                if aid not in id_to_hash:
                    miss.append(f"{an}(id={aid})")
                print(f"  #{fid} {hn} vs {an} -> {'; '.join(miss)}")

        if args.dry_run:
            print("\n[dry-run] 以下场次将补缺:")
            for f in need[:50]:
                fid, hid, aid, hn, an, fdate, status, league = f
                print(f"  #{fid} {fdate} [{status}] {hn} vs {an} ({league})")
            if len(need) > 50:
                print(f"  ... 共 {len(need)} 场")
            return

        if not need:
            print("\n无需补缺, 结束。")
            return

        from tools.fetch_flashscore_match_xg import scrape_match_xg

        targets = need if args.limit <= 0 else need[:args.limit]
        eta = len(targets) * EST_SEC_PER_MATCH
        print(f"\n===== 开始补缺 {len(targets)} 场 (预计 {eta//60} 分 {eta%60} 秒) =====")

        ok, fail = 0, 0
        for i, f in enumerate(targets, 1):
            fid, hid, aid, hn, an, fdate, status, league = f
            home_hash = id_to_hash[hid]
            away_hash = id_to_hash[aid]
            h2n = {home_hash: id_to_name.get(hid, hn), away_hash: id_to_name.get(aid, an)}
            print(f"\n[{i}/{len(targets)}] #{fid} {hn} vs {an} ({fdate})")
            try:
                stats = scrape_match_xg(home_hash, away_hash, h2n, match_date=fdate)
                written = upsert_xg(db, fid, hid, hn, aid, an, stats)
                if written:
                    ok += 1
                    print(f"  OK 写入 {written} 条: "
                          f"home xG={stats['home']['xg']}/GP={stats['home']['goals_prevented']}, "
                          f"away xG={stats['away']['xg']}/GP={stats['away']['goals_prevented']}")
                else:
                    fail += 1
                    print("  FAIL Flashscore 未返回 xG/GP 数据 (0 条写入)")
            except Exception as e:
                fail += 1
                print(f"  FAIL 抓取失败: {e}")
            if i < len(targets):
                time.sleep(args.sleep)

        print(f"\n===== 补缺完成: 成功 {ok} / 失败(无数据或异常) {fail} / 共 {len(targets)} =====")
    finally:
        db.close()


if __name__ == "__main__":
    main()
