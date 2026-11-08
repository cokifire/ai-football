"""赔率共识与百分比解析的共用工具。

predict_fixture（预测时注入校准方向参考）与 compute_value_bets（事后价值投注）
必须用同一套取线/去水口径，否则两处对「所选方向」的赔率理解会漂移。
"""
from statistics import median


def parse_pct(v):
    """兼容 '53%' / 0.53 / None，统一返回 0-1 概率。"""
    if v is None:
        return None
    s = str(v).strip().replace("%", "").replace("％", "")
    try:
        f = float(s)
    except (ValueError, TypeError):
        return None
    return f / 100.0 if f > 1 else f


def consensus(snaps):
    """合并一个 fixture 的全部赔率快照，返回各市场共识（原始 decimal 赔率中位数）。

    1X2: {home, draw, away}
    ou:  {line: {over, under}}
    ah:  {line: {home_side, away_side}}  line 与 llm_handicap_num 同向（正=客让/负=主让）
    """
    h_raw, d_raw, a_raw = [], [], []
    ou, ah = {}, {}

    for snap in snaps:
        data = snap.get("odds_data") if isinstance(snap, dict) else snap
        if isinstance(data, dict):
            data = data.get("odds_data", [])
        for bm in data or []:
            for e in bm.get("entries", []):
                if e.get("home_raw"):
                    h_raw.append(float(e["home_raw"]))
                if e.get("draw_raw"):
                    d_raw.append(float(e["draw_raw"]))
                if e.get("away_raw"):
                    a_raw.append(float(e["away_raw"]))

                if e.get("ou_line") is not None and e.get("ou_over_raw") and e.get("ou_under_raw"):
                    ln = float(e["ou_line"])
                    g = ou.setdefault(ln, {"over": [], "under": []})
                    g["over"].append(float(e["ou_over_raw"]))
                    g["under"].append(float(e["ou_under_raw"]))

                if e.get("ah_line") is not None and (e.get("ah_home_raw") or e.get("ah_away_raw")):
                    ln = float(e["ah_line"])
                    # ah_line 与 llm_handicap_num 同口径（正=客让、负=主让），直接当 key；
                    # 取反会与 compute_value_bets 的 cons["ah"].get(hc) 符号对不上，
                    # 导致让球盘 value-bet 市场永远 available:False。
                    g = ah.setdefault(ln, {"home_side": [], "away_side": []})
                    hs, as_ = e.get("ah_home_raw"), e.get("ah_away_raw")
                    if hs:
                        g["home_side"].append(float(hs))
                    if as_:
                        g["away_side"].append(float(as_))

    def med(v):
        return median(v) if v else None

    return {
        "1x2": ({o: med(v) for o, v in (("home", h_raw), ("draw", d_raw), ("away", a_raw))}
                if (h_raw and d_raw and a_raw) else None),
        "ou": {ln: {"over": med(v["over"]), "under": med(v["under"])}
               for ln, v in ou.items() if v["over"] and v["under"]},
        "ah": {ln: {"home_side": med(v["home_side"]), "away_side": med(v["away_side"])}
               for ln, v in ah.items() if v["home_side"] and v["away_side"]},
    }
