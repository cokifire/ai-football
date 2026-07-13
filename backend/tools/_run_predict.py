"""运行完整预测管线并友好打印结果."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prediction.predict import predict_fixture

if __name__ == "__main__":
    fid = int(sys.argv[1]) if len(sys.argv) > 1 else 1494202
    res = predict_fixture(fid)
    if res is None:
        print("预测未生成(可能是特征不足 / LLM 未返回 / 网络异常)")
        sys.exit(1)

    xgb = {k: res[k] for k in ("win_home", "win_draw", "win_away", "over25_prob", "lambda_home", "lambda_away", "handicap", "model_group")}
    print("== XGBoost 机器模型 ==")
    print(f"  主胜 {xgb['win_home']:.1%} | 平 {xgb['win_draw']:.1%} | 客胜 {xgb['win_away']:.1%}")
    print(f"  大小球(2.5)大球概率 {xgb['over25_prob']:.1%}")
    print(f"  预期进球 λ_home={xgb['lambda_home']:.2f} λ_away={xgb['lambda_away']:.2f}")
    print(f"  让球参考 {xgb['handicap']} | 模型组 {xgb['model_group']}")
    print("  Top3 比分:")
    for t in res["top3"]:
        print(f"    {t['score']:<6} {t['prob']:.1%}")

    llm = res.get("llm") or {}
    print("\n== DeepSeek LLM 最终研判 ==")
    for k in ("win", "win_pct", "score", "handicap_num", "handicap_team", "handicap_pct",
              "ou_line", "ou_type", "ou_pct", "brief_analysis", "core_data", "deep_report"):
        v = llm.get(k)
        if v is not None:
            print(f"  {k}: {v}")
    print("\n预测结果已写入 predictions 表。")
