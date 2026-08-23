"""
通用比赛查询工具
支持功能：
1. 根据fixture_id查询比赛基础信息
2. 查询比赛预测数据
3. 查询完赛技术统计（角球、射门、控球率等）

用法：
# 查询ID为1494234的比赛详情
python tools/query_fixture.py --id 1494234
"""
import os
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import SessionLocal
from sqlalchemy import text
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()
db = SessionLocal()

def get_fixture_info(fixture_id: int) -> dict:
    fixture = db.execute(text("""
    SELECT id, home_name, away_name, league_name, date, status_short, referee, venue_name
    FROM fixtures WHERE id = :fid
    """), {'fid': fixture_id}).fetchone()
    return dict(fixture._mapping) if fixture else None

def get_prediction_info(fixture_id: int) -> dict:
    pred = db.execute(text("""
    SELECT win_home, win_draw, win_away, over25_prob, llm_win, llm_win_pct, llm_brief,
           llm_handicap_team, llm_handicap_num, llm_handicap_pct,
           llm_ou_type, llm_ou_line, llm_ou_pct, llm_score
    FROM predictions WHERE fixture_id = :fid
    """), {'fid': fixture_id}).fetchone()
    return dict(pred._mapping) if pred else None

def get_match_stats(fixture_id: int) -> dict:
    stats = db.execute(text("""
    SELECT 
        fs.team_name,
        MAX(CASE WHEN fs.stat_type = 'Corner Kicks' THEN fs.stat_value END) AS corners,
        MAX(CASE WHEN fs.stat_type = 'Shots on Goal' THEN fs.stat_value END) AS shots_on_target,
        MAX(CASE WHEN fs.stat_type = 'Total Shots' THEN fs.stat_value END) AS total_shots,
        MAX(CASE WHEN fs.stat_type = 'Ball Possession' THEN fs.stat_value END) AS possession,
        MAX(CASE WHEN fs.stat_type = 'Fouls' THEN fs.stat_value END) AS fouls,
        MAX(CASE WHEN fs.stat_type = 'Yellow Cards' THEN fs.stat_value END) AS yellow_cards,
        MAX(CASE WHEN fs.stat_type = 'Red Cards' THEN fs.stat_value END) AS red_cards
    FROM fixture_statistics fs WHERE fs.fixture_id = :fid
    GROUP BY fs.team_name
    """), {'fid': fixture_id}).fetchall()
    return [dict(r._mapping) for r in stats] if stats else None

def _fmt_pct(value) -> str:
    if value is None:
        return "-"
    return f"{float(value)*100:.1f}%"

def main():
    parser = argparse.ArgumentParser(description="比赛查询工具")
    parser.add_argument('--id', type=int, required=True, help='比赛fixture_id')
    args = parser.parse_args()
    fixture_id = args.id

    fixture = get_fixture_info(fixture_id)
    if not fixture:
        console.print(f"[red]未找到fixture_id={fixture_id}的比赛[/red]")
        db.close()
        return

    # 基础信息
    console.print(Panel(
        f"联赛: {fixture['league_name']}\n"
        f"对阵: [bold cyan]{fixture['home_name']}[/bold cyan] vs [bold magenta]{fixture['away_name']}[/bold magenta]\n"
        f"时间: {fixture['date']}\n"
        f"状态: {fixture['status_short']}\n"
        f"裁判: {fixture['referee'] or '-'}\n"
        f"场地: {fixture['venue_name'] or '-'}",
        title=f"比赛基础信息 ID: {fixture_id}",
        border_style="cyan"
    ))

    # 预测数据
    pred = get_prediction_info(fixture_id)
    if pred:
        table = Table(title="赛前预测数据")
        table.add_column("项目", style="cyan")
        table.add_column("结果", style="white")
        table.add_row("胜平负概率", f"主胜 {_fmt_pct(pred['win_home'])} | 平 {_fmt_pct(pred['win_draw'])} | 客胜 {_fmt_pct(pred['win_away'])}")
        table.add_row("大2.5球概率", _fmt_pct(pred['over25_prob']))
        table.add_row("LLM最终预测", f"{pred['llm_win']} {pred['llm_win_pct'] or ''}")
        table.add_row("让球预测", f"{pred['llm_handicap_team'] or '-'} {pred['llm_handicap_num'] or '-'} {pred['llm_handicap_pct'] or ''}")
        table.add_row("大小球预测", f"{pred['llm_ou_type'] or '-'} {pred['llm_ou_line'] or '-'} {pred['llm_ou_pct'] or ''}")
        table.add_row("比分预测", pred['llm_score'] or '-')
        table.add_row("分析摘要", pred['llm_brief'] or '-')
        console.print(table)

    # 完赛统计
    if fixture['status_short'] == 'FT':
        stats = get_match_stats(fixture_id)
        if stats and len(stats) == 2:
            home_stat = next((s for s in stats if s['team_name'] == fixture['home_name']), {})
            away_stat = next((s for s in stats if s['team_name'] == fixture['away_name']), {})
            table = Table(title="完赛技术统计")
            table.add_column("项目", style="cyan")
            table.add_column(fixture['home_name'], justify="right", style="cyan")
            table.add_column(fixture['away_name'], justify="right", style="magenta")
            for item in [
                ("角球", 'corners'), ("射正", 'shots_on_target'), ("总射门", 'total_shots'),
                ("控球率", 'possession'), ("犯规", 'fouls'), ("黄牌", 'yellow_cards'), ("红牌", 'red_cards')
            ]:
                table.add_row(item[0], str(home_stat.get(item[1], '-')), str(away_stat.get(item[1], '-')))
            console.print(table)

    db.close()

if __name__ == "__main__":
    main()
