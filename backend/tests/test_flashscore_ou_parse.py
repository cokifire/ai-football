"""回归测试：Flashscore 改版后大小球改从 ope2 GraphQL 响应取数。

重点保护两个坑：
  1. 盘口线不能用赔率的 _to_float 解析——它会把 0.5 / 1.5 这类合法盘口
     当成 <1.0 的无效值丢掉；
  2. 停售(active=False)与占位值("-")要跳过，不能混进均值。
"""
from tools.fetch_flashscore_odds import _item_odd, _parse_ou_payloads


def _opp(line, over, under, over_active=True, under_active=True):
    return {
        "over": {"value": over, "active": over_active},
        "under": {"value": under, "active": under_active},
        "handicap": {"value": line, "type": "UNKNOWN"},
    }


def test_parses_all_lines_including_small_handicaps():
    payload = {
        "type": "OVER_UNDER",
        "bookmakerId": 997,
        "opportunities": [
            _opp("0.5", "1.09", "6.50"),
            _opp("2.5", "1.32", "3.20"),
            _opp("3.5", "1.81", "1.93"),
        ],
    }
    rows = _parse_ou_payloads([payload])
    assert [r["line"] for r in rows] == [0.5, 2.5, 3.5]
    assert rows[1] == {
        "bookmaker": "997", "line": 2.5, "over": 1.32, "under": 3.2,
    }


def test_ignores_other_betting_types():
    payload = {
        "type": "HOME_DRAW_AWAY", "bookmakerId": 417,
        "opportunities": [_opp("2.5", "1.32", "3.20")],
    }
    assert _parse_ou_payloads([payload]) == []


def test_skips_inactive_and_placeholder_odds():
    payload = {
        "type": "OVER_UNDER", "bookmakerId": 417,
        "opportunities": [
            _opp("2.5", "-", "3.20"),                       # 占位值
            _opp("2.75", "1.38", "2.90", over_active=False),  # 停售
            _opp("3.0", "1.46", "2.60"),
        ],
    }
    rows = _parse_ou_payloads([payload])
    assert [r["line"] for r in rows] == [3.0]


def test_skips_missing_handicap_and_keeps_quarter_line():
    payload = {
        "type": "OVER_UNDER", "bookmakerId": 417,
        "opportunities": [
            {"over": {"value": "1.32", "active": True},
             "under": {"value": "3.20", "active": True}},   # 无 handicap
            _opp("2,2.5", "1.32", "3.20"),                  # 四分盘取均值
        ],
    }
    rows = _parse_ou_payloads([payload])
    assert [r["line"] for r in rows] == [2.25]


def test_item_odd_rejects_out_of_range_and_inactive():
    assert _item_odd({"value": "1.90", "active": True}) == 1.9
    assert _item_odd({"value": "0.5", "active": True}) is None
    assert _item_odd({"value": "1.90", "active": False}) is None
    assert _item_odd(None) is None
