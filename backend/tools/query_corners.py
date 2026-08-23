"""
通用角球查询工具
支持功能：
1. 查询某球队近N场角球统计
2. 查询两队历史交锋角球数据
3. 分析对阵双方角球投注价值

用法：
# 查询赫根近10场角球
python tools/query_corners.py --team "BK Hacken" --limit 10

# 查询赫根 vs 哈姆斯塔德历史交锋角球
python tools/query_corners.py --h2h "BK Hacken" "Halmstad"

# 分析布拉迪斯拉发 vs 采列角球价值
python tools/query_corners.py --analyze "Slovan Bratislava" "Celje"
"""
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import SessionLocal
from sqlalchemy import text
from rich.console import Console
from rich.table import Table

console = Console()
db = SessionLocal()

def get_team_corner_stats(team_name: str, limit: int = 15) -> list:
    rows = db.execute(text("""
    SELECT 
        f.date,
        f.home_name,
        f.away_name,
        CAST(MAX(CASE WHEN fs.team_name = f.home_name AND fs.stat_type = 'Corner Kicks' THEN fs.stat_value END) AS SIGNED) AS home_corners,
        CAST(MAX(CASE WHEN fs.team_name = f.away_name AND fs.stat_type = 'Corner Kicks' THEN fs.stat_value END) AS SIGNED) AS away_corners
    FROM fixtures f
    JOIN fixture_statistics fs ON f.id = fs.fixture_id
    WHERE (f.home_name = :team OR f.away_name = :team)
      AND f.status_short = 'FT'
      AND fs.stat_type = 'Corner Kicks'
    GROUP BY f.id, f.date, f.home_name, f.away_name
    HAVING home_corners IS NOT NULL AND away_corners IS NOT NULL
    ORDER BY f.date DESC
    LIMIT :limit
    """), {'team': team_name, 'limit': limit}).fetchall()
    data = []
    for r in rows:
        d = dict(r._mapping)
        d['team_corners'] = d['home_corners'] if d['home_name'] == team_name else d['away_corners']
        d['opponent_corners'] = d['away_corners'] if d['home_name'] == team_name else d['home_corners']
        d['total'] = d['home_corners'] + d['away_corners']
        data.append(d)
    return data

def get_h2h_corners(team1: str, team2: str) -> list:
    rows = db.execute(text("""
    SELECT 
        f.date,
        f.home_name,
        f.away_name,
        CAST(MAX(CASE WHEN fs.team_name = f.home_name AND fs.stat_type = 'Corner Kicks' THEN fs.stat_value END) AS SIGNED) AS home_corners,
        CAST(MAX(CASE WHEN fs.team_name = f.away_name AND fs.stat_type = 'Corner Kicks' THEN fs.stat_value END) AS SIGNED) AS away_corners
    FROM fixtures f
    JOIN fixture_statistics fs ON f.id = fs.fixture_id
    WHERE ((f.home_name = :t1 AND f.away_name = :t2) OR (f.home_name = :t2 AND f.away_name = :t1))
      AND f.status_short = 'FT'
      AND fs.stat_type = 'Corner Kicks'
    GROUP BY f.id, f.date, f.home_name, f.away_name
    ORDER BY f.date DESC
    """), {'t1': team1, 't2': team2}).fetchall()
    data = [dict(r._mapping) for r in rows]
    for d in data:
        d['total'] = d['home_corners'] + d['away_corners']
    return data

def render_team_stats(team_name: str, data: list):
    if not data:
        console.print(f"[red]未找到{team_name}的角球数据[/red]")
        return
    table = Table(title=f"{team_name} 近{len(data)}场角球明细")
    table.add_column("日期", style="cyan")
    table.add_column("对阵")
    table.add_column("主队角球", justify="right")
    table.add_column("客队角球", justify="right")
    table.add_column("总角球", justify="right", style="magenta")
    for item in data:
        table.add_row(
            item['date'].strftime('%Y-%m-%d'),
            f"{item['home_name']} vs {item['away_name']}",
            str(item['home_corners']),
            str(item['away_corners']),
            str(item['total'])
        )
    avg_team = sum([x['team_corners'] for x in data])/len(data)
    avg_opp = sum([x['opponent_corners'] for x in data])/len(data)
    avg_total = sum([x['total'] for x in data])/len(data)
    console.print(table)
    console.print(f"场均统计: 本队获得{avg_team:.1f}个 | 对手获得{avg_opp:.1f}个 | 总角球{avg_total:.1f}个")
    console.print(f"大9场数: {len([x for x in data if x['total'] >9])}场 ({len([x for x in data if x['total']>9])/len(data)*100:.0f}%)")

def render_h2h_stats(team1: str, team2: str, data: list):
    if not data:
        console.print(f"[red]未找到{team1} vs {team2}的交锋数据[/red]")
        return
    table = Table(title=f"{team1} vs {team2} 历史交锋角球")
    table.add_column("日期", style="cyan")
    table.add_column("对阵")
    table.add_column("主队角球", justify="right")
    table.add_column("客队角球", justify="right")
    table.add_column("总角球", justify="right", style="magenta")
    for item in data:
        table.add_row(
            item['date'].strftime('%Y-%m-%d'),
            f"{item['home_name']} vs {item['away_name']}",
            str(item['home_corners']),
            str(item['away_corners']),
            str(item['total'])
        )
    avg_total = sum([x['total'] for x in data])/len(data)
    console.print(table)
    console.print(f"共{len(data)}场交锋，场均总角球{avg_total:.1f}个")

def analyze_corner_value(home_team: str, away_team: str):
    home_stats = get_team_corner_stats(home_team, 15)
    away_stats = get_team_corner_stats(away_team, 15)
    h2h_stats = get_h2h_corners(home_team, away_team)
    
    if not home_stats or not away_stats:
        console.print("[red]数据不足，无法分析[/red]")
        return
    
    home_avg_total = sum([x['total'] for x in home_stats])/len(home_stats)
    away_avg_total = sum([x['total'] for x in away_stats])/len(away_stats)
    expected_total = (home_avg_total + away_avg_total)/2
    
    home_avg_get = sum([x['team_corners'] for x in home_stats])/len(home_stats)
    away_avg_get = sum([x['team_corners'] for x in away_stats])/len(away_stats)
    home_avg_concede = sum([x['opponent_corners'] for x in home_stats])/len(home_stats)
    away_avg_concede = sum([x['opponent_corners'] for x in away_stats])/len(away_stats)
    
    expected_home = (home_avg_get + away_avg_concede)/2
    expected_away = (away_avg_get + home_avg_concede)/2
    
    if h2h_stats:
        h2h_avg_total = sum([x['total'] for x in h2h_stats])/len(h2h_stats)
        expected_total = (expected_total + h2h_avg_total)/2
    
    console.rule(f"[bold cyan]{home_team} vs {away_team} 角球价值分析[/bold cyan]")
    console.print(f"预期主队角球: {expected_home:.1f} | 预期客队角球: {expected_away:.1f} | 预期总角球: {expected_total:.1f}")
    
    diff = expected_home - expected_away
    if diff > 2:
        console.print(f"✅ [green]主队角球让分-1.5/2有价值[/green]：主队场均比客队多获得{diff:.1f}个角球")
    elif diff < -2:
        console.print(f"✅ [green]客队角球让分-1.5/2有价值[/green]：客队场均比主队多获得{-diff:.1f}个角球")
    else:
        console.print("⚠️ [yellow]角球让分价值低[/yellow]：两队角球能力接近，波动大")
    
    if expected_total > 10:
        console.print(f"✅ [green]总角球大9.5有价值[/green]：预期总角球{expected_total:.1f}，两队大角概率均超过60%")
    elif expected_total < 8:
        console.print(f"✅ [green]总角球小9.5有价值[/green]：预期总角球{expected_total:.1f}，两队小角概率高")
    else:
        console.print("⚠️ [yellow]总角球价值低[/yellow]：预期值在9左右，盘口利润空间小")

def main():
    parser = argparse.ArgumentParser(description="角球查询工具")
    parser.add_argument('--team', type=str, help='查询单队角球数据，输入球队名')
    parser.add_argument('--limit', type=int, default=10, help='单队查询返回场数，默认10')
    parser.add_argument('--h2h', type=str, nargs=2, help='查询两队交锋角球，输入两个球队名')
    parser.add_argument('--analyze', type=str, nargs=2, help='分析对阵双方角球投注价值，输入主队+客队名')
    args = parser.parse_args()

    if args.team:
        data = get_team_corner_stats(args.team, args.limit)
        render_team_stats(args.team, data)
    elif args.h2h:
        data = get_h2h_corners(args.h2h[0], args.h2h[1])
        render_h2h_stats(args.h2h[0], args.h2h[1], data)
    elif args.analyze:
        analyze_corner_value(args.analyze[0], args.analyze[1])
    else:
        parser.print_help()
    
    db.close()

if __name__ == "__main__":
    main()
