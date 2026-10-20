"""
由已有的 xG 推导并回填 xGA (Expected goals against)。

口径
----
本队 xGA = 同一场比赛对手的 expected_goals。这是定义上的恒等关系
(对手创造的 xG 就是本队承受的 xGA), 不是估算或代理指标, 因此不需要爬虫、
不消耗 API-Football 额度, 纯库内推导。

只覆盖「双方 expected_goals 都存在」的比赛; 对手 xG 缺失时宁可跳过,
不做任何推测性填充 (与 project 约定一致: 缺数据不造近似值)。

落库
----
fixture_statistics, stat_type = 'expected_goals_against'。
值原样复制对手的 stat_value 字符串, 保证 A.xga 与 B.xg 逐字节一致,
便于事后用恒等式自检。

约束
----
- 表上没有 (fixture_id, team_id, stat_type) 唯一约束, 去重靠应用层查找;
  写前先查已存在的 xGA 行, 默认跳过, 仅 --force 时覆盖。
- team_id 为空、同一场比赛出现 3+ 个 team_id、或对手 xG 为空值的行,
  一律跳过并计数。

用法
----
    python tools/backfill_xga.py --dry-run          # 只扫描不写
    python tools/backfill_xga.py                    # 实际写入 (跳过已存在)
    python tools/backfill_xga.py --force            # 已存在也按当前 xG 覆盖
    python tools/backfill_xga.py --limit 500        # 限制本次写入条数
"""
import sys
import argparse
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# app/core/config.py 的 env_file = ".env" 是相对路径, 按 CWD 解析; 从仓库根直接
# `python tools/backfill_xga.py` 会找不到 backend/.env, 悄悄退回 localhost:3306
# 默认值并报 Access denied。这里显式按脚本位置加载, 使脚本从任意目录运行都拿到
# 同一套配置 (os.environ 优先级高于 env_file)。
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")

if hasattr(sys.stdout, "reconfigure"):
    # line_buffering: 被 `2>&1` 管道重定向时 stdout 会退化成块缓冲(4~8KB),
    # 回填几万条时看起来像"卡住没反应"; 逐行冲刷才能看到实时进度。
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

from sqlalchemy import text  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.fixture import FixtureStatistic  # noqa: E402

XG = "expected_goals"
XGA = "expected_goals_against"
COMMIT_BATCH = 200
EMPTY_VALS = ("", "None", "none", "null")


def _clean(v):
    """stat_value 是字符串; 空值/占位值视为缺失。"""
    if v is None:
        return None
    s = str(v).strip()
    return None if s in EMPTY_VALS else s


def load_xg(db):
    """读取全部 expected_goals 行。

    返回 ({(fixture_id, team_id): (team_name, stat_value)}, 冲突键列表, 原始行数)。
    同一 (fixture_id, team_id) 出现多行时保留第一条, 值不一致的记入冲突。
    """
    rows = db.execute(text(
        "SELECT fixture_id, team_id, team_name, stat_value "
        "FROM fixture_statistics WHERE stat_type = :t"
    ), {"t": XG}).fetchall()
    out, conflicts = {}, []
    for fid, tid, tname, val in rows:
        if tid is None:
            continue
        key = (fid, tid)
        if key in out:
            if _clean(out[key][1]) != _clean(val):
                conflicts.append((fid, tid, out[key][1], val))
            continue
        out[key] = (tname, val)
    return out, conflicts, len(rows)


def load_xga(db):
    """读取全部已存在的 xGA 行 -> {(fixture_id, team_id): (id, stat_value)}。"""
    rows = db.execute(text(
        "SELECT id, fixture_id, team_id, stat_value "
        "FROM fixture_statistics WHERE stat_type = :t"
    ), {"t": XGA}).fetchall()
    out, dups = {}, 0
    for rid, fid, tid, val in rows:
        if tid is None:
            continue
        key = (fid, tid)
        if key in out:
            dups += 1
            continue
        out[key] = (rid, val)
    return out, dups


def group_by_fixture(xg_map):
    by_fx = {}
    for (fid, tid), val in xg_map.items():
        by_fx.setdefault(fid, {})[tid] = val
    return by_fx


def plan(by_fx, existing, force):
    """按恒等式推导待写列表, 返回 (新增, 更新, 各类跳过计数)。"""
    to_insert, to_update = [], []
    skip_one_side, skip_anomaly, skip_no_val = [], [], []
    skip_exists, already_same = 0, 0

    for fid, teams in by_fx.items():
        if len(teams) < 2:
            skip_one_side.append((fid, list(teams)))
            continue
        if len(teams) > 2:
            skip_anomaly.append((fid, len(teams)))
            continue

        (tid_a, (name_a, val_a)), (tid_b, (name_b, val_b)) = list(teams.items())
        # 本队 xGA = 对手 xG
        for tid, tname, opp_val in ((tid_a, name_a, val_b), (tid_b, name_b, val_a)):
            val = _clean(opp_val)
            if val is None:
                skip_no_val.append((fid, tid))
                continue
            cur = existing.get((fid, tid))
            if cur is None:
                to_insert.append((fid, tid, tname, val))
            elif _clean(cur[1]) == val:
                already_same += 1
            elif force:
                to_update.append((cur[0], fid, tid, tname, val))
            else:
                skip_exists += 1

    return {
        "insert": to_insert,
        "update": to_update,
        "one_side": skip_one_side,
        "anomaly": skip_anomaly,
        "no_val": skip_no_val,
        "exists": skip_exists,
        "same": already_same,
    }


def write(db, to_insert, to_update, progress_every=2000):
    written = 0
    for fid, tid, tname, val in to_insert:
        db.add(FixtureStatistic(
            fixture_id=fid, team_id=tid, team_name=tname,
            stat_type=XGA, stat_value=val,
        ))
        written += 1
        if written % COMMIT_BATCH == 0:
            db.commit()
            if written % progress_every == 0:
                print(f"  新增中 {written}/{len(to_insert)}")
    db.commit()

    stmt = text("UPDATE fixture_statistics SET stat_value = :v, team_name = :n WHERE id = :i")
    for rid, fid, tid, tname, val in to_update:
        db.execute(stmt, {"v": val, "n": tname, "i": rid})
        written += 1
        if written % COMMIT_BATCH == 0:
            db.commit()
            if written % progress_every == 0:
                print(f"  更新中 {written}/{len(to_insert) + len(to_update)}")
    db.commit()
    return written


def verify(db):
    """写入后自检: 同一场比赛内 A.xga 应等于 B.xg (且反之亦然)。"""
    xg_map, _, _ = load_xg(db)
    rows = db.execute(text(
        "SELECT fixture_id, team_id, stat_value "
        "FROM fixture_statistics WHERE stat_type = :t"
    ), {"t": XGA}).fetchall()
    xga_map = {(fid, tid): val for fid, tid, val in rows if tid is not None}

    checked, bad = 0, 0
    for fid, teams in group_by_fixture(xg_map).items():
        if len(teams) != 2:
            continue
        ids = list(teams)
        for idx, tid in enumerate(ids):
            opp = ids[1 - idx]
            got, want = _clean(xga_map.get((fid, tid))), _clean(teams[opp][1])
            if got is None or want is None:
                continue
            checked += 1
            if got != want:
                bad += 1
                if bad <= 10:
                    print(f"  不符 #{fid} team={tid}: xga={got} 但对手 xg={want}")
    print(f"\n自检: 核对 {checked} 条, 不符 {bad} 条; xGA 行总数 {len(xga_map)}")


def main():
    ap = argparse.ArgumentParser(description="由对手 xG 推导回填 xGA (expected_goals_against)")
    ap.add_argument("--dry-run", action="store_true", help="只扫描并打印将写入的内容, 不落库")
    ap.add_argument("--force", action="store_true", help="已存在的 xGA 行也按当前 xG 覆盖")
    ap.add_argument("--limit", type=int, default=0, help="本次最多写入(新增+更新)条数, 0=不限")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        xg_map, conflicts, xg_rows = load_xg(db)
        existing, xga_dups = load_xga(db)
        by_fx = group_by_fixture(xg_map)
        res = plan(by_fx, existing, args.force)

        if args.limit > 0:
            room = args.limit
            res["insert"] = res["insert"][:room]
            room -= len(res["insert"])
            res["update"] = res["update"][:room]

        print("\n===== xGA 回填扫描 (xGA = 同场对手的 expected_goals) =====")
        print(f"expected_goals 行数       : {xg_rows}")
        print(f"涉及比赛数                : {len(by_fx)}")
        print(f"已有 xGA 行数             : {len(existing)}")
        print(f"  ├─ 值已一致 (跳过)      : {res['same']}")
        print(f"  └─ 值不同 (--force 才改): {res['exists'] if not args.force else 0}")
        print(f"仅一队有 xG (无法推导)    : {len(res['one_side'])}")
        print(f"异常: 一场 3+ 个 team_id  : {len(res['anomaly'])}")
        print(f"对手 xG 为空值            : {len(res['no_val'])}")
        print(f"待新增                    : {len(res['insert'])}")
        print(f"待更新                    : {len(res['update'])}")
        if conflicts:
            print(f"[warn] expected_goals 存在 {len(conflicts)} 个重复键且值不一致, 已取第一条")
            for fid, tid, a, b in conflicts[:10]:
                print(f"  #{fid} team={tid}: {a} vs {b}")
        if xga_dups:
            print(f"[warn] xGA 存在 {xga_dups} 条重复行, 更新时只改第一条")

        if res["one_side"]:
            print("\n-- 仅一队有 xG, 跳过 (共 %d 场) --" % len(res["one_side"]))
            for fid, tids in res["one_side"][:20]:
                print(f"  #{fid} 只有 team_id={tids}")
        if res["anomaly"]:
            print("\n-- 一场比赛出现 3+ 个 team_id, 跳过 --")
            for fid, n in res["anomaly"][:20]:
                print(f"  #{fid} 共 {n} 个 team_id")

        if args.dry_run:
            print("\n[dry-run] 前 30 条将写入:")
            for fid, tid, tname, val in res["insert"][:30]:
                print(f"  + #{fid} team={tid} ({tname}) xga={val}")
            for rid, fid, tid, tname, val in res["update"][:30]:
                print(f"  ~ #{fid} team={tid} ({tname}) xga -> {val} (id={rid})")
            if len(res["insert"]) + len(res["update"]) > 30:
                print(f"  ... 共 {len(res['insert']) + len(res['update'])} 条")
            return

        total = len(res["insert"]) + len(res["update"])
        if total == 0:
            print("\n无需写入, 结束。")
            verify(db)
            return

        print(f"\n===== 开始写入 {total} 条 (新增 {len(res['insert'])} / 更新 {len(res['update'])}) =====")
        written = write(db, res["insert"], res["update"])
        print(f"写入完成: {written} 条")
        verify(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
