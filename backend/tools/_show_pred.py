"""从 predictions 表读取并干净打印某 fixture 的预测结果(UTF-8)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import SessionLocal
from sqlalchemy import text

if __name__ == "__main__":
    fid = int(sys.argv[1]) if len(sys.argv) > 1 else 1494202
    db = SessionLocal()
    row = db.execute(text("""
        SELECT home_name, away_name, league_name, match_date, model_group,
               win_home, win_draw, win_away, over25_prob, top3_scores,
               lambda_home, lambda_away, handicap,
               llm_win, llm_win_pct, llm_score,
               llm_handicap_num, llm_handicap_team, llm_handicap_pct,
               llm_ou_line, llm_ou_type, llm_ou_pct,
               llm_brief, llm_core_data, llm_deep_report,
               updated_at
        FROM predictions WHERE fixture_id=:fid
    """), {"fid": fid}).fetchone()
    db.close()
    if not row:
        print("未找到预测记录")
        sys.exit(1)
    d = dict(row._mapping)
    print(f"比赛: {d['home_name']} vs {d['away_name']}  ({d['league_name']})")
    print(f"时间: {d['match_date']}   模型组: {d['model_group']}   更新: {d['updated_at']}")
    print("\n【XGBoost 机器概率】")
    print(f"  主胜 {d['win_home']:.1%} | 平 {d['win_draw']:.1%} | 客胜 {d['win_away']:.1%}")
    print(f"  大小球(2.5)大球 {d['over25_prob']:.1%}   预期进球 λ={d['lambda_home']:.2f}/{d['lambda_away']:.2f}   让球参考 {d['handicap']}")
    try:
        import json
        top3 = json.loads(d['top3_scores'])
        print("  Top3 比分: " + "  ".join(f"{t['score']}({t['prob']:.0%})" for t in top3))
    except Exception:
        pass
    print("\n【DeepSeek 最终研判】")
    print(f"  胜负: {d['llm_win']}   信心: {d['llm_win_pct']}")
    print(f"  比分: {d['llm_score']}")
    print(f"  让球: {d['llm_handicap_team']} {d['llm_handicap_num']}  赢盘概率 {d['llm_handicap_pct']}")
    print(f"  大小球: {d['llm_ou_line']} {d['llm_ou_type']}  概率 {d['llm_ou_pct']}")
    print(f"  一句话: {d['llm_brief']}")
    print(f"  核心数据: {d['llm_core_data']}")
    print(f"  深度报告: {d['llm_deep_report']}")
