"""修复 team_elos：补种缺失球队、纠正错配占用值（2026-10-24 排查产出）。

背景：10 月预测中 16 场触发「team_elos 缺少球队 Elo」风险。根因是最初按
clubelo 名称匹配播种（2026-10-09 两批：23:10 主批次 + 23:35 补充批次）时：
  1. CSV 中 5 处错配——Brest→Dinamo Brest、AEK→AEK Larnaca、AZ→Jong AZ、
     Depor→Deportes Tolima、Sparta(鹿特丹)→Sparta Praha，真正的球队未入库，
     被错配的球队占着别人的 Elo 值；
  2. 2 支未匹配——Bodø/Glimt、Sabah FK 没进候选映射；
  3. 播种漏种——Telstar(427)、Groningen(202) 在 CSV 有 exact_norm 匹配却未入库；
  4. AEK Larnaca(614) 被 AEK 行错配，占着 AEK Athens 的 1704。

修正后的 elo_candidates_refined.csv 已把相关行改为 manual 正确映射，
本脚本按该 CSV 外科手术式修复，不触碰其余行——CSV 中还有 48 个重复
match_id（B 队/二队/同名球队，如 Liverpool 两支、Nacional 四支），全量
重刷有把正确值覆盖成错误值的风险，故不做全量重播种。

修复动作：
  INSERT（缺失）: 106 Stade Brestois 29 / 202 Groningen / 327 Bodo-Glimt /
                  427 Telstar / 575 AEK Athens FC / 13976 Sabah FA
  UPDATE（纠错）: 614 AEK Larnaca 1704.00 -> 1595（占着 AEK Athens 的值）

不触碰（有合法内部漂移或用户已修）：
  201 AZ Alkmaar 1672.10 —— 账本有 2 次内部更新（1686.00 基线漂移而来）
  426 Sparta Rotterdam 1506.80 —— 账本有 2 次内部更新（1498.00 基线漂移而来）
  544 Deportivo La Coruna 1686 —— 用户 2026-10-24 已手动修正为当前 clubelo 值
  （覆盖有账本记录的球队会破坏 apply_finished_fixture_elo 的记账模型，
    故 UPDATE 前强制检查 team_elo_updates，有记录则拒绝并告警。）

elo 取值：CSV 中的当前值（2026-10-24 自 clubelo.com 刷新）。
name 取值：以 teams.name 为准（TeamElo 模型要求与 teams.name 一致）。
created_at：INSERT 时取 NOW()——apply_finished_fixture_elo 按 created_at
决定是否跳过旧比赛，取 NOW() 可避免历史比赛被重复计分。

注意：生产库 team_elos.elo 实为 decimal(8,2)（与模型的 Integer 声明不符，
属 schema 漂移，另行处理）；本脚本写入整数值，MySQL 会自动存为 x.00。

用法（在 backend/ 目录下、项目 venv；需先建立 3307 SSH 隧道，见 backend/.env）：
    PYTHONPATH=backend python tools/repair_team_elos.py            # 预演，不写入
    PYTHONPATH=backend python tools/repair_team_elos.py --apply    # 实际写入
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402

CSV_PATH = Path(__file__).resolve().parent.parent / 'elo_candidates_refined.csv'

# 修复集：缺失补种 + 614 纠错（elo 以 CSV 当前值为准）
REPAIR_IDS = {106, 202, 327, 427, 575, 614, 13976}
# 观察集：只报告不改写（合法漂移 / 用户已修）
WATCH_IDS = {201, 426, 544}


def load_targets() -> dict[int, dict]:
    """从修正后的 CSV 读取目标行；elo 取 CSV 值，name 稍后以 teams 表为准。"""
    with open(CSV_PATH, encoding='utf-8-sig') as f:
        targets: dict[int, dict] = {}
        for r in csv.DictReader(f):
            mid = (r['match_id'] or '').strip()
            if not mid or int(mid) not in REPAIR_IDS:
                continue
            tid = int(mid)
            assert tid not in targets, f'CSV 中 id={tid} 出现多行，请先去重'
            targets[tid] = {'club': r['club'], 'elo': int(r['elo']),
                            'csv_name': r['match_name'], 'method': r['method']}
    missing = REPAIR_IDS - set(targets)
    assert not missing, f'CSV 中缺少修复目标行: {sorted(missing)}'
    return targets


def ledger_count(db, team_id: int) -> int:
    """该球队在 Elo 更新账本中的记录数（>0 说明有内部漂移，不可覆盖）。"""
    return db.execute(text(
        'SELECT COUNT(*) FROM team_elo_updates '
        'WHERE home_team_id=:i OR away_team_id=:i'), {'i': team_id}).scalar()


def main() -> None:
    ap = argparse.ArgumentParser(description='修复 team_elos（补种缺失 + 纠正错配值）')
    ap.add_argument('--apply', action='store_true', help='实际写入；不加则仅预演')
    args = ap.parse_args()

    targets = load_targets()
    db = SessionLocal()
    stats = {'insert': 0, 'update': 0, 'same': 0, 'skip': 0}
    try:
        print('=== 修复集 ===')
        for tid in sorted(targets):
            t = targets[tid]
            team = db.execute(text('SELECT name FROM teams WHERE id=:i'),
                              {'i': tid}).fetchone()
            if not team:
                print(f'[SKIP]   id={tid:<6} {t["csv_name"]} 不在 teams 表（外键会拒绝）')
                stats['skip'] += 1
                continue
            name = team[0]  # teams.name 为权威名称
            cur = db.execute(
                text('SELECT name, elo FROM team_elos WHERE id=:i'),
                {'i': tid}).fetchone()
            if cur is None:
                print(f'[INSERT] id={tid:<6} {name:<22} elo={t["elo"]:<5} '
                      f'(clubelo: {t["club"]}, {t["method"]})')
                if args.apply:
                    db.execute(text(
                        'INSERT INTO team_elos (id, name, elo, created_at, updated_at) '
                        'VALUES (:i, :n, :e, NOW(), NOW())'),
                        {'i': tid, 'n': name, 'e': t['elo']})
                stats['insert'] += 1
            elif cur.elo != t['elo'] or cur.name != name:
                n_ledger = ledger_count(db, tid)
                if n_ledger:
                    print(f'[REFUSE] id={tid:<6} {name:<22} elo {cur.elo} -> '
                          f'{t["elo"]} 被拒绝：账本有 {n_ledger} 条内部更新记录，'
                          f'覆盖会破坏 Elo 记账')
                    stats['skip'] += 1
                    continue
                note = '' if cur.name == name else f'  name {cur.name!r} -> {name!r}'
                print(f'[UPDATE] id={tid:<6} {name:<22} elo {cur.elo} -> {t["elo"]}{note}')
                if args.apply:
                    # 保留 created_at：Elo 账本按它决定是否跳过旧比赛
                    db.execute(text(
                        'UPDATE team_elos SET name=:n, elo=:e, updated_at=NOW() '
                        'WHERE id=:i'), {'i': tid, 'n': name, 'e': t['elo']})
                stats['update'] += 1
            else:
                print(f'[OK]     id={tid:<6} {name:<22} elo={t["elo"]} 无需改动')
                stats['same'] += 1

        print('\n=== 观察集（不改写）===')
        for tid in sorted(WATCH_IDS):
            r = db.execute(text('SELECT name, elo FROM team_elos WHERE id=:i'),
                           {'i': tid}).fetchone()
            if r is None:
                print(f'[WATCH]  id={tid:<6} 不在 team_elos（需关注）')
            else:
                n_ledger = ledger_count(db, tid)
                drift = f'漂移中（账本 {n_ledger} 条）' if n_ledger else '无账本记录'
                print(f'[WATCH]  id={tid:<6} {r.name:<22} elo={r.elo}  {drift}')

        if args.apply:
            db.commit()
            print(f'\n已提交: insert={stats["insert"]} update={stats["update"]} '
                  f'same={stats["same"]} skip={stats["skip"]}')
        else:
            print(f'\n预演完成（未写入）: insert={stats["insert"]} '
                  f'update={stats["update"]} same={stats["same"]} skip={stats["skip"]}')
            print('确认无误后加 --apply 执行')
    finally:
        db.close()


if __name__ == '__main__':
    main()
