"""回归测试：让球方向一致性校验 + value_bet AH 盘口 key 符号。

覆盖：
  1. prediction.predict._check_handicap_consistency —— 让 0.5 球盘下 win 与
     handicap 方向矛盾（如 win=客胜60% 却客让0.5 下注主队）应判为不合规；
     1.0 球以上盘口让球方可"赢球但输盘"，不做硬校验（交给 LLM 自洽分布）；
  2. value_bet_service._consensus —— AH 盘口 key 与 llm_handicap_num 同口径
     （正=客让/负=主让），不得取反，主/客两侧价格不交换。
"""

from prediction.predict import _check_handicap_consistency
from app.services.value_bet_service import _consensus


def test_handicap_consistency_rejects_contradiction():
    # 用户报告的矛盾案例：win=客胜60%，客让0.5，却下注主队59%
    assert _check_handicap_consistency({
        "win": "客胜", "win_pct": "60%",
        "handicap_num": "+0.5", "handicap_team": "主队", "handicap_pct": "59%",
    }) is False
    # 主让0.5 的对称矛盾：客队赢盘应为 1-P(主胜)=30%，却报 76%
    assert _check_handicap_consistency({
        "win": "主胜", "win_pct": "70%",
        "handicap_num": "-0.5", "handicap_team": "客队", "handicap_pct": "76%",
    }) is False


def test_handicap_consistency_allows_large_handicap():
    """1.0 球以上盘口不做 1-win_pct 硬比。

    让球方"赢球但输盘"时 handicap_pct 本就大于 1-win_pct，属 LLM 单一
    自洽分布的合法结论。回归 2026-11-13 fixture=1528970：客让1.5、
    win=客胜却下注主队受让，三次都被判矛盾导致整场预测未落库。
    """
    # 日志第 1 次输出
    assert _check_handicap_consistency({
        "win": "客胜", "win_pct": "44%",
        "handicap_num": "1.5", "handicap_team": "主队", "handicap_pct": "80%",
    }) is True
    # 日志第 3 次：+1.5 写法
    assert _check_handicap_consistency({
        "win": "客胜", "win_pct": "43%",
        "handicap_num": "+1.5", "handicap_team": "主队", "handicap_pct": "80%",
    }) is True
    # 主让1.5 对称：原 rejects_contradiction 用例，现在应放行
    assert _check_handicap_consistency({
        "win": "主胜", "win_pct": "70%",
        "handicap_num": "-1.5", "handicap_team": "客队", "handicap_pct": "76%",
    }) is True


def test_handicap_consistency_accepts_consistent():
    # 方向一致：客让0.5 下注客队且 pct 等于 win_pct
    assert _check_handicap_consistency({
        "win": "客胜", "win_pct": "60%",
        "handicap_num": "0.5", "handicap_team": "客队", "handicap_pct": "60%",
    }) is True
    # 方向一致：主让0.5 下注主队（让球方）
    assert _check_handicap_consistency({
        "win": "主胜", "win_pct": "70%",
        "handicap_num": "-0.5", "handicap_team": "主队", "handicap_pct": "70%",
    }) is True


def test_handicap_consistency_skips_non_half_ball_and_draw():
    # 四分之一盘 / 整数盘 / win=平局 均不做硬校验（返回 True 放行）
    assert _check_handicap_consistency({
        "win": "客胜", "win_pct": "60%",
        "handicap_num": "-0.25", "handicap_team": "客队", "handicap_pct": "79%",
    }) is True
    assert _check_handicap_consistency({
        "win": "平局", "win_pct": "40%",
        "handicap_num": "0.5", "handicap_team": "主队", "handicap_pct": "50%",
    }) is True
    # 整数盘 1.0 同样不做硬校验
    assert _check_handicap_consistency({
        "win": "客胜", "win_pct": "44%",
        "handicap_num": "1.0", "handicap_team": "主队", "handicap_pct": "80%",
    }) is True


def test_consensus_ah_key_matches_handicap_num_sign():
    snaps = [{
        "odds_data": [
            {"bookmaker": "test", "entries": [
                # 客让0.5：Inter(客) 让 0.5，主侧=1.98 客侧=1.88
                {"ah_line": 0.5, "ah_home_raw": 1.98, "ah_away_raw": 1.88},
                # 主让1.75：Barca(主) 让 1.75，主侧=1.82 客侧=2.02
                {"ah_line": -1.75, "ah_home_raw": 1.82, "ah_away_raw": 2.02},
            ]},
        ],
    }]
    cons = _consensus(snaps)
    assert set(cons["ah"].keys()) == {0.5, -1.75}
    assert cons["ah"][0.5]["home_side"] == 1.98
    assert cons["ah"][0.5]["away_side"] == 1.88
    assert cons["ah"][-1.75]["home_side"] == 1.82
    assert cons["ah"][-1.75]["away_side"] == 2.02
