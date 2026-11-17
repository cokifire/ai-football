"""盘口深浅命中率监控（只读，不写库）。

按 |让球数| 分层统计盘口命中率，用于定位「主让深盘」这类系统性偏弱场景。

口径与 app/api/predictions.py::_derive_result_flags 完全一致：
  adjusted_home = fulltime_home + handicap_num（num 负=主让、正=客让）
  走水（adjusted_home = fulltime_away）剔除，不计入命中率分母
  命中 = 选「主队」且 adjusted_home > away；选「客队」且 adjusted_home < away

用法：
  python tools/handicap_depth_accuracy.py
  python tools/handicap_depth_accuracy.py --model codex:default --date-from 2026-11-01
  python tools/handicap_depth_accuracy.py --by-side
"""
import argparse
import sys
from pathlib import Path

# Windows 下 Python 的 stdout 默认 GBK，而 PowerShell Core 按 UTF-8 解码子进程输出，
# 强制 UTF-8 避免 rich 表格里的中文（如「平手盘」）乱码。
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import SessionLocal
from sqlalchemy import text
from rich.console import Console
from rich.table import Table

console = Console()

DEPTH_EXPR = """
  CASE
    WHEN ABS(hc) = 0     THEN '0 平手盘(0)'
    WHEN ABS(hc) <= 0.25 THEN '1 浅盘|L|<=0.25'
    WHEN ABS(hc) <= 0.5  THEN '2 半一内|L|<=0.5'
    WHEN ABS(hc) <= 0.75 THEN '3 让半一|L|<=0.75'
    WHEN ABS(hc) <= 1.0  THEN '4 让一球|L|<=1.0'
    WHEN ABS(hc) <= 1.5  THEN '5 让球半|L|<=1.5'
    ELSE                      '6 深盘|L|>1.5'
  END
"""

SIDE_EXPR = "CASE WHEN hc < 0 THEN '主让(负)' WHEN hc > 0 THEN '客让(正)' ELSE '平手(0)' END"


def main():
    parser = argparse.ArgumentParser(description="盘口深浅命中率监控（只读）")
    parser.add_argument("--model", type=str, default=None, help="按 llm_model 过滤，如 codex:default")
    parser.add_argument("--date-from", type=str, default=None, help="预测生成时间下限，含，如 2026-11-01")
    parser.add_argument("--date-to", type=str, default=None, help="预测生成时间上限，含")
    parser.add_argument("--by-side", action="store_true", help="按「让球方向 × 深浅」二维拆分")
    args = parser.parse_args()

    conditions = [
        "f.fulltime_home IS NOT NULL",
        "f.fulltime_away IS NOT NULL",
        "p.llm_handicap_num REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'",
        "p.llm_handicap_team IN ('主队', '客队')",
    ]
    params = {}
    if args.model:
        conditions.append("p.llm_model = :model")
        params["model"] = args.model.strip()
    if args.date_from:
        conditions.append("p.created_at >= :df")
        params["df"] = args.date_from.strip()
    if args.date_to:
        conditions.append("p.created_at < DATE_ADD(:dt, INTERVAL 1 DAY)")
        params["dt"] = args.date_to.strip()

    where = "WHERE " + " AND ".join(conditions)

    if args.by_side:
        select_cols = f"{SIDE_EXPR} AS side, {DEPTH_EXPR} AS depth"
        group_cols = "side, depth"
    else:
        select_cols = f"{DEPTH_EXPR} AS depth"
        group_cols = "depth"

    sql = f"""
    SELECT {select_cols},
           COUNT(*) AS total,
           SUM(push_flag) AS push,
           SUM(hit_flag)  AS hit,
           ROUND(100 * SUM(hit_flag) / NULLIF(COUNT(*) - SUM(push_flag), 0), 1) AS acc_pct
    FROM (
      SELECT
        CAST(p.llm_handicap_num AS DECIMAL(10,2)) AS hc,
        (f.fulltime_home + CAST(p.llm_handicap_num AS DECIMAL(10,2)) = f.fulltime_away) AS push_flag,
        ((f.fulltime_home + CAST(p.llm_handicap_num AS DECIMAL(10,2)) > f.fulltime_away AND p.llm_handicap_team = '主队')
         OR
         (f.fulltime_home + CAST(p.llm_handicap_num AS DECIMAL(10,2)) < f.fulltime_away AND p.llm_handicap_team = '客队')) AS hit_flag
      FROM predictions p
      JOIN fixtures f ON f.id = p.fixture_id
      {where}
    ) t
    GROUP BY {group_cols}
    ORDER BY {group_cols}
    """

    db = SessionLocal()
    try:
        rows = db.execute(text(sql), params).fetchall()
    finally:
        db.close()

    title = "盘口深浅命中率"
    if args.model:
        title += f"（{args.model}）"
    table = Table(title=title)
    if args.by_side:
        table.add_column("方向", style="cyan")
    table.add_column("盘口深度", style="cyan")
    table.add_column("样本", justify="right")
    table.add_column("走水", justify="right")
    table.add_column("命中", justify="right")
    table.add_column("命中率", justify="right", style="magenta")

    for r in rows:
        d = dict(r._mapping)
        vals = [d["side"], d["depth"]] if args.by_side else [d["depth"]]
        vals += [str(d["total"]), str(d["push"]), str(d["hit"]),
                 f"{float(d['acc_pct'] or 0):.1f}%"]
        table.add_row(*vals)
    console.print(table)

    total = sum(int(r._mapping["total"]) for r in rows)
    push = sum(int(r._mapping["push"]) for r in rows)
    hit = sum(int(r._mapping["hit"]) for r in rows)
    valid = total - push
    if valid:
        console.print(f"合计：{total} 场（走水 {push}），命中 {hit}，命中率 {hit / valid * 100:.1f}%")
    else:
        console.print(f"合计：{total} 场，无有效样本")


if __name__ == "__main__":
    main()
