"""方向对称校验的失败测试（TDD RED）。

覆盖：Platt 校准双侧换算、整数盘走水、quarter line 无整场走水、
缺赔率/缺校准参数标 UNAVAILABLE、两侧 edge 判据（不用 50% 阈值）、
两侧 edge 接近时回落到市场共识侧（让方），以及共用赔率工具。
"""
import json
import math

from app.services.calibration_service import (
    calibrated_sides, collect_samples, direction_calibration_hint,
)
from app.services.market_odds import consensus, parse_pct
from app.services.value_bet_service import calibrated_comparison


def _write_calibration(tmp_path, params):
    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps({"params": params, "diagnostics": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path)


def test_parse_pct_accepts_percent_string_and_fraction():
    assert parse_pct("54%") == 0.54
    assert parse_pct(0.54) == 0.54
    assert parse_pct("65") == 0.65
    assert parse_pct(None) is None
    assert parse_pct("abc") is None


def test_consensus_merges_ou_and_ah_lines():
    snaps = [[{"bookmaker": "b1", "entries": [
        {"ou_line": 2.5, "ou_over_raw": 1.90, "ou_under_raw": 1.95,
         "ah_line": 0.5, "ah_home_raw": 1.88, "ah_away_raw": 1.98},
    ]}]]
    c = consensus(snaps)
    assert c["ou"][2.5] == {"over": 1.90, "under": 1.95}
    assert c["ah"][0.5]["home_side"] == 1.88
    assert c["ah"][0.5]["away_side"] == 1.98


def test_calibrated_sides_returns_two_sided_edges_without_changing_selection(tmp_path):
    path = _write_calibration(tmp_path, {"ou": {"a": 1.0, "b": 0.0}})
    r = calibrated_sides("ou", 0.60, "under", 2.0, 1.80, 2.5, path=path)
    assert r["status"] == "AVAILABLE"
    assert r["selected_side"] == "under"
    assert set(r["sides"]) == {"over", "under"}
    assert math.isclose(r["calibrated_p"], 0.60, abs_tol=1e-6)
    assert math.isclose(r["sides"]["under"]["p"], 0.60)
    assert math.isclose(r["sides"]["over"]["p"], 0.40)
    assert math.isclose(r["sides"]["under"]["edge"], 0.60 - 1 / 1.80)


def test_platt_slope_shrinks_overconfident_selected_side(tmp_path):
    path = _write_calibration(
        tmp_path, {"ou": {"a": -0.4408550093056103, "b": -0.12820291895626645}}
    )
    r = calibrated_sides("ou", 0.65, "under", 1.95, 1.95, 2.5, path=path)
    assert math.isclose(r["calibrated_p"], 0.401, abs_tol=1e-3)
    assert r["calibrated_p"] < r["raw_p"]


def test_integer_line_push_keeps_two_sided_sum_below_one(tmp_path):
    path = _write_calibration(tmp_path, {"ou": {"a": 1.0, "b": 0.0}})
    r = calibrated_sides("ou", 0.50, "over", 2.10, 2.10, 3.0, path=path)
    assert r["push_p"] > 0
    assert sum(s["p"] for s in r["sides"].values()) < 1.0


def test_quarter_line_has_no_full_push(tmp_path):
    path = _write_calibration(tmp_path, {"ou": {"a": 1.0, "b": 0.0}})
    r = calibrated_sides("ou", 0.50, "over", 1.90, 1.90, 2.75, path=path)
    assert r["push_p"] == 0.0
    assert math.isclose(sum(s["p"] for s in r["sides"].values()), 1.0)


def test_missing_odds_is_unavailable_not_fabricated(tmp_path):
    path = _write_calibration(tmp_path, {"ou": {"a": 1.0, "b": 0.0}})
    r = calibrated_sides("ou", 0.55, "under", None, 1.90, 2.5, path=path)
    assert r["status"] == "UNAVAILABLE"
    assert r["suggested_side"] is None
    assert r["reason"]


def test_unfitted_calibration_is_unavailable(tmp_path):
    path = _write_calibration(tmp_path, {})
    r = calibrated_sides("ou", 0.55, "under", 1.90, 1.90, 2.5, path=path)
    assert r["status"] == "UNAVAILABLE"
    assert r["suggested_side"] is None


def test_edge_not_fifty_percent_threshold_decides_side(tmp_path):
    path = _write_calibration(tmp_path, {"ou": {"a": 1.0, "b": 0.0}})
    r = calibrated_sides("ou", 0.45, "under", 1.20, 4.00, 2.5, path=path)
    assert r["sides"]["under"]["p"] < 0.5
    assert r["suggested_side"] == "under"


def test_near_equal_edges_fall_back_to_market_consensus_giving_side(tmp_path):
    path = _write_calibration(tmp_path, {"ah": {"a": 1.0, "b": 0.0}})
    # 选中受让方（弱队），但两侧 edge 差小于 margin 时应回落到让方 home
    r = calibrated_sides("ah", 0.50, "away", 1.90, 2.00, -1.75, path=path)
    assert r["selected_side"] == "away"
    assert r["market_consensus_side"] == "home"
    assert r["suggested_side"] == "home"


def test_direction_hint_uses_two_sided_edge_and_no_fifty_percent_threshold(tmp_path):
    path = _write_calibration(tmp_path, {"ou": {"a": 1.0, "b": 0.0}})
    cons = {"ou": {2.5: {"over": 1.90, "under": 1.95}}, "ah": {}}
    hint = direction_calibration_hint(cons, path=path)
    assert "edge" in hint
    assert "两侧" in hint
    assert "2.5" in hint
    assert "过五成" not in hint
    assert "50%" not in hint


def test_direction_hint_reports_calibrated_hit_rate_from_real_params(tmp_path):
    path = _write_calibration(
        tmp_path, {"ou": {"a": -0.4408550093056103, "b": -0.12820291895626645}}
    )
    hint = direction_calibration_hint({}, path=path)
    assert "45.0%" in hint


def test_direction_hint_marks_unavailable_without_params(tmp_path):
    hint = direction_calibration_hint({}, path=str(tmp_path / "missing.json"))
    assert "UNAVAILABLE" in hint


def test_calibrated_comparison_keeps_llm_side_and_suggestion_separate(tmp_path):
    path = _write_calibration(tmp_path, {
        "ou": {"a": 1.0, "b": 0.0}, "ah": {"a": 1.0, "b": 0.0},
    })
    p = {
        "llm_ou_type": "小", "llm_ou_line": "2.5", "llm_ou_pct": "60%",
        "llm_handicap_num": "-1.75", "llm_handicap_team": "客队",
        "llm_handicap_pct": "50%",
    }
    cons = {
        "ou": {2.5: {"over": 2.0, "under": 1.80}},
        "ah": {-1.75: {"home_side": 1.90, "away_side": 2.00}},
    }
    out = calibrated_comparison(p, cons, path=path)
    assert out["ou"]["status"] == "AVAILABLE"
    assert out["ou"]["llm_side"] == "under"
    assert set(out["ou"]["sides"]) == {"over", "under"}
    assert out["ah"]["llm_side"] == "away"
    assert out["ah"]["selected_side"] == "away"
    assert out["ah"]["suggested_side"] == "home"  # 边缘接近时回落到让方


def test_calibrated_comparison_marks_missing_odds_unavailable(tmp_path):
    path = _write_calibration(tmp_path, {"ou": {"a": 1.0, "b": 0.0}})
    p = {"llm_ou_type": "小", "llm_ou_line": "2.5", "llm_ou_pct": "60%"}
    out = calibrated_comparison(p, {"ou": {}, "ah": {}}, path=path)
    assert out["ou"]["status"] == "UNAVAILABLE"
    assert out["ou"]["reason"]


def test_collect_samples_tolerates_dash_placeholder_ou_line():
    class _Row:
        def __init__(self, d):
            self._mapping = d

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _DB:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *args, **kwargs):
            return _Result(self._rows)

    # skill 契约：无赔率时 ou_line 写 "-"。历史行里确实存在该值。
    row = _Row({
        "win_home": 0.5, "win_draw": 0.25, "win_away": 0.25,
        "llm_ou_type": "小", "llm_ou_line": "-", "llm_ou_pct": "54%",
        "over25_prob": 0.40,
        "llm_handicap_num": "-0.5", "llm_handicap_team": "客队",
        "llm_handicap_pct": "55%",
        "llm_win": "主胜", "llm_win_pct": "55%",
        "actual_home_goals": 1, "actual_away_goals": 0,
    })
    samples = collect_samples(_DB([row]))
    assert samples["ou"]["raw"] == []   # 占位行不进大小球样本
    assert samples["ou25"]["raw"] == []  # 也不能在 2.5 fallback 处崩溃
    assert len(samples["ah"]["raw"]) == 1  # 其它市场照常采集
