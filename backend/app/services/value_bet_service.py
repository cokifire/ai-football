"""价值投注筛选服务。

核心逻辑：比较「模型隐含概率」与「市场赔率隐含概率」，找出有正 edge 的下注机会。
  edge = model_p - 1/odds      （未去庄家抽水的市场隐含概率）
  kelly = (model_p * odds - 1) / (odds - 1)   单注建议比例（下限 0）

覆盖市场：胜负平(1X2)、大小球(Over/Under)、让球盘(Asian Handicap)。
"""
import json
from statistics import median

from sqlalchemy import text

from app.services.calibration_service import apply_calibration


def _parse_pct(v):
    """兼容 '53%' / 0.53 / None，统一返回 0-1 概率。"""
    if v is None:
        return None
    s = str(v).strip().replace("%", "").replace("％", "")
    try:
        f = float(s)
    except (ValueError, TypeError):
        return None
    return f / 100.0 if f > 1 else f


def _consensus(snaps):
    """合并一个 fixture 的全部赔率快照，返回各市场共识（原始 decimal 赔率中位数）。

    1X2: {home, draw, away}
    ou:  {line: {over, under}}
    ah:  {mline: {home_side, away_side}}  mline 为主队视角让球数（负=主让）
    """
    h_raw, d_raw, a_raw = [], [], []
    ou, ah = {}, {}

    for snap in snaps:
        data = snap.get("odds_data") if isinstance(snap, dict) else snap
        if isinstance(data, dict):
            data = data.get("odds_data", [])
        for bm in data:
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
                    mline = -ln  # 统一到主队视角让球数（负=主让）
                    g = ah.setdefault(mline, {"home_side": [], "away_side": []})
                    if ln >= 0:  # API "Home X"：主队侧=home_raw
                        hs, as_ = e.get("ah_home_raw"), e.get("ah_away_raw")
                    else:       # API "Away X"：主队侧=away_raw
                        hs, as_ = e.get("ah_away_raw"), e.get("ah_home_raw")
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


def _kelly(model_p, odds):
    if odds <= 1:
        return 0.0
    f = (model_p * odds - 1) / (odds - 1)
    return max(0.0, f)


def _side_label(side):
    return {"home": "主胜", "draw": "平局", "away": "客胜"}.get(side, side)


def compute_value_bets(db, fixture_id, refresh=False, margin=0.03):
    """返回某场比赛的价值投注建议（dict）。"""
    pred = db.execute(text("""
        SELECT win_home, win_draw, win_away, over25_prob,
               llm_win, llm_win_pct, llm_ou_type, llm_ou_line, llm_ou_pct,
               llm_handicap_num, llm_handicap_team, llm_handicap_pct,
               llm_score, top3_scores
        FROM predictions WHERE fixture_id=:fid
    """), {"fid": fixture_id}).fetchone()
    if not pred:
        return {"error": "该场暂无预测数据，请先调用 /predict/{fixture_id} 生成预测"}

    p = dict(pred._mapping)

    if refresh:
        from prediction.predict import _fetch_odds, _save_odds
        row = db.execute(text("SELECT date FROM fixtures WHERE id=:fid"),
                         {"fid": fixture_id}).fetchone()
        match_date = row[0] if row else None
        res = _fetch_odds(fixture_id, match_date)
        if isinstance(res, dict) and res.get("__api_error__") is not None:
            return {"error": "赔率接口受限，无法实时抓取", "detail": res["__api_error__"]}
        if res and isinstance(res, dict) and res.get("odds_data"):
            _save_odds(db, fixture_id, res)

    snaps_raw = db.execute(text(
        "SELECT odds_data FROM odds WHERE fixture_id=:fid ORDER BY created_at DESC, id DESC"
    ), {"fid": fixture_id}).fetchall()
    if not snaps_raw:
        return {"error": "该场暂无赔率数据，请先调用 POST /odds/{fixture_id} 抓取（或用 refresh=true）"}

    snaps = []
    for (od,) in snaps_raw:
        if isinstance(od, (str, bytes, bytearray)):
            try:
                od = json.loads(od)
            except Exception:
                continue
        snaps.append(od)
    cons = _consensus(snaps)

    markets = {}

    # ── 1X2 胜负平 ──
    mkt = cons.get("1x2")
    if mkt:
        probs = {"home": float(p.get("win_home") or 0),
                 "draw": float(p.get("win_draw") or 0),
                 "away": float(p.get("win_away") or 0)}
        sels, best = [], None
        for side, odd in mkt.items():
            raw = probs[side]
            if raw <= 0 or odd <= 1:
                continue
            mp = apply_calibration("1x2_" + side, raw)
            implied = 1 / odd
            edge = mp - implied
            sel = {"side": side, "label": _side_label(side),
                   "model_p": round(mp, 4), "raw_model_p": round(raw, 4),
                   "market_implied": round(implied, 4),
                   "edge": round(edge, 4), "kelly": round(_kelly(mp, odd), 4), "odds": odd}
            sels.append(sel)
            if edge > margin and (best is None or edge > best["edge"]):
                best = sel
        markets["1x2"] = {
            "available": True,
            "recommendation": best["label"] if best else "无价值（观望）",
            "best_edge": round(best["edge"], 4) if best else None,
            "selections": sels,
        }
    else:
        markets["1x2"] = {"available": False}

    # ── 大小球 ──
    ou_type = (p.get("llm_ou_type") or "").strip()
    line = p.get("llm_ou_line")
    if ou_type and line is not None and cons.get("ou"):
        line = float(line)
        mkt_ou = cons["ou"].get(line)
        mp = _parse_pct(p.get("llm_ou_pct"))
        mp_key = "ou"
        if mp is None and line == 2.5 and p.get("over25_prob") is not None:
            base = float(p["over25_prob"])
            mp = base if "大" in ou_type else 1 - base
            mp_key = "ou25"
        if mkt_ou and mp is not None:
            raw = mp
            mp = apply_calibration(mp_key, raw)
            side = "over" if "大" in ou_type else "under"
            odd = mkt_ou[side]
            implied = 1 / odd
            edge = mp - implied
            sel = {"side": side, "label": ("大球" if side == "over" else "小球"),
                   "model_p": round(mp, 4), "raw_model_p": round(raw, 4),
                   "market_implied": round(implied, 4),
                   "edge": round(edge, 4), "kelly": round(_kelly(mp, odd), 4), "odds": odd}
            markets["ou"] = {
                "available": True, "line": line,
                "recommendation": (sel["label"] + f" (盘口{line})") if edge > margin else "无价值（观望）",
                "best_edge": round(edge, 4), "selections": [sel],
            }
        else:
            markets["ou"] = {"available": False}
    else:
        markets["ou"] = {"available": False}

    # ── 让球盘 ──
    hc = p.get("llm_handicap_num")
    team = (p.get("llm_handicap_team") or "").strip()
    if hc is not None and team and cons.get("ah"):
        hc = float(hc)
        mkt_ah = cons["ah"].get(hc)
        mp = _parse_pct(p.get("llm_handicap_pct"))
        if mkt_ah and mp is not None:
            raw = mp
            mp = apply_calibration("ah", raw)
            if team == "客队":
                odd, side_label = mkt_ah["away_side"], "客队"
            else:
                odd, side_label = mkt_ah["home_side"], "主队"
            implied = 1 / odd
            edge = mp - implied
            sel = {"side": side_label,
                   "label": f"{side_label}赢盘",
                   "model_p": round(mp, 4), "raw_model_p": round(raw, 4),
                   "market_implied": round(implied, 4),
                   "edge": round(edge, 4), "kelly": round(_kelly(mp, odd), 4), "odds": odd,
                   "handicap_num": hc, "handicap_team": team}
            markets["ah"] = {
                "available": True,
                "recommendation": (f"{side_label}赢盘 (盘口{hc})") if edge > margin else "无价值（观望）",
                "best_edge": round(edge, 4), "selections": [sel],
            }
        else:
            markets["ah"] = {"available": False}
    else:
        markets["ah"] = {"available": False}

    return {"fixture_id": fixture_id, "margin": margin, "markets": markets}
