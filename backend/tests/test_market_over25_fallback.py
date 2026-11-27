"""_market_over25_prob 兜底口径测试。

背景：odds 无 2.5 线时，旧实现把「最均衡线 L 的去水 P(>L)」直接当 P(>2.5)，
高线时系统性低估 ~15-20pp；新实现经 convert_ou_prob 泊松反解后换算到 2.5。
"""
import pytest

from prediction.predict import _market_over25_prob


def test_with_25_line_uses_direct_devig():
    """有 2.5 线时保持原逻辑：直接去水，不受 share 影响。"""
    cons = {"ou": {2.5: {"over": 1.95, "under": 1.85}}}
    expect = (1 / 1.95) / (1 / 1.95 + 1 / 1.85)
    assert _market_over25_prob(cons, share=0.55) == pytest.approx(expect, abs=1e-9)
    assert _market_over25_prob(cons, share=0.80) == pytest.approx(expect, abs=1e-9)


def test_fallback_high_line_converts_upward():
    """最均衡线挂在 3.25（对称赔率）时，不能把 0.5 当 P(大2.5)；
    3.25 均衡 ⇒ total≈3.3 ⇒ P(大2.5)≈0.70。"""
    cons = {"ou": {3.25: {"over": 1.90, "under": 1.90}}}
    p = _market_over25_prob(cons, share=0.55)
    assert 0.65 < p < 0.78


def test_fallback_low_line_converts_downward():
    """最均衡线挂在 2.0（对称赔率）时，P(大2.5) 应低于 0.5（方向也正确）。"""
    cons = {"ou": {2.0: {"over": 1.90, "under": 1.90}}}
    p = _market_over25_prob(cons, share=0.55)
    assert 0.20 < p < 0.50


def test_fallback_picks_most_balanced_line():
    """无 2.5 线时选两侧隐含概率最接近 50/50 的线，而非随便一条。"""
    cons = {"ou": {
        3.0: {"over": 3.20, "under": 1.35},   # 明显不均衡，不应被选中
        3.25: {"over": 1.90, "under": 1.90},  # 均衡线
    }}
    only = {"ou": {3.25: {"over": 1.90, "under": 1.90}}}
    assert _market_over25_prob(cons, share=0.55) == pytest.approx(
        _market_over25_prob(only, share=0.55), abs=1e-9
    )


def test_no_ou_returns_none():
    assert _market_over25_prob({}, share=0.55) is None
    assert _market_over25_prob(None, share=0.55) is None
