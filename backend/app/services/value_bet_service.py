"""价值投注筛选服务。

核心逻辑：比较「模型隐含概率」与「市场赔率隐含概率」，找出有正 edge 的下注机会。
  edge = model_p - 1/odds      （未去庄家抽水的市场隐含概率）
  kelly = (model_p * odds - 1) / (odds - 1)   单注建议比例（下限 0）

覆盖市场：胜负平(1X2)、大小球(Over/Under)、让球盘(Asian Handicap)。
"""
import json

from sqlalchemy import text

from app.services.calibration_service import (
    CALIBRATION_PATH, apply_calibration, calibrated_sides,
)
from app.services.market_odds import consensus as shared_consensus, parse_pct as shared_parse_pct


def _parse_pct(v):
    return shared_parse_pct(v)


def _consensus(snaps):
    return shared_consensus(snaps)


def _unavailable_comparison(key, side, reason):
    return {
        "status": "UNAVAILABLE",
        "reason": reason,
        "key": key,
        "selected_side": side,
        "llm_side": side,
        "sides": {},
        "suggested_side": None,
        "market_consensus_side": None,
    }


def calibrated_comparison(p, cons, margin=0.03, path=CALIBRATION_PATH):
    """生成 ou / ah 的双侧校准对照，供 LLM 原方向与校准建议方向并排展示。

    只读取已有预测与赔率共识，不写库、不覆盖 llm_ou_type / llm_handicap_team。
    判据是两侧 edge，缺赔率/缺 pct/缺校准参数一律标 UNAVAILABLE。
    """
    out = {}

    ou_type = (p.get("llm_ou_type") or "").strip()
    ou_line = p.get("llm_ou_line")
    if ou_type and ou_line is not None:
        try:
            ln = float(ou_line)
        except (TypeError, ValueError):
            ln = None
        mkt = (cons.get("ou") or {}).get(ln) if ln is not None else None
        raw = _parse_pct(p.get("llm_ou_pct"))
        side = "over" if "大" in ou_type else "under"
        if mkt and raw is not None:
            r = calibrated_sides("ou", raw, side, mkt["over"], mkt["under"], ln,
                                 path=path, margin=margin)
        else:
            r = _unavailable_comparison("ou", side, "缺少该盘口线的两侧赔率或所选方向概率")
        r["llm_side"] = side
        out["ou"] = r

    hc = p.get("llm_handicap_num")
    team = (p.get("llm_handicap_team") or "").strip()
    if hc is not None and team:
        try:
            hcv = float(hc)
        except (TypeError, ValueError):
            hcv = None
        mkt = (cons.get("ah") or {}).get(hcv) if hcv is not None else None
        raw = _parse_pct(p.get("llm_handicap_pct"))
        side = "away" if team == "客队" else "home"
        if mkt and raw is not None:
            r = calibrated_sides("ah", raw, side, mkt["home_side"], mkt["away_side"], hcv,
                                 path=path, margin=margin)
        else:
            r = _unavailable_comparison("ah", side, "缺少该盘口线的两侧赔率或所选方向概率")
        r["llm_side"] = side
        out["ah"] = r

    return out


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

        def _add_sel(side, label, raw, source, cal_key):
            nonlocal best
            odd = mkt.get(side)
            if odd is None or odd <= 1 or raw is None or raw <= 0:
                return
            mp = apply_calibration(cal_key, raw)
            implied = 1 / odd
            edge = mp - implied
            sel = {"side": side, "label": label, "source": source,
                   "model_p": round(mp, 4), "raw_model_p": round(raw, 4),
                   "market_implied": round(implied, 4),
                   "edge": round(edge, 4), "kelly": round(_kelly(mp, odd), 4), "odds": odd}
            sels.append(sel)
            if edge > margin and (best is None or edge > best["edge"]):
                best = sel

        for side, label in (("home", "主胜"), ("draw", "平局"), ("away", "客胜")):
            _add_sel(side, label, probs[side], "model", "1x2_" + side)

        # LLM 自己的胜负判断作为独立信源纳入同一 1X2 市场：不覆盖宿主结论，
        # 只同台比较，edge 最高者胜出（recommendation_source 标明来源）。
        llm_side = {"主胜": "home", "平局": "draw", "客胜": "away"}.get((p.get("llm_win") or "").strip())
        if llm_side:
            _add_sel(llm_side, _side_label(llm_side), _parse_pct(p.get("llm_win_pct")), "llm", "llm_win")

        markets["1x2"] = {
            "available": True,
            "recommendation": best["label"] if best else "无价值（观望）",
            "recommendation_source": best["source"] if best else None,
            "best_edge": round(best["edge"], 4) if best else None,
            "selections": sels,
        }
    else:
        markets["1x2"] = {"available": False}

    # 双侧校准对照：LLM 原方向与校准建议方向并排返回，不改任何落库字段。
    comparison = calibrated_comparison(p, cons, margin=margin)

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
            cmp_ou = comparison.get("ou")
            if cmp_ou and cmp_ou["status"] == "AVAILABLE":
                other = "under" if side == "over" else "over"
                s_other = cmp_ou["sides"][other]
                o_odd = mkt_ou[other]
                markets["ou"]["selections"].append({
                    "side": other, "label": ("小球" if other == "under" else "大球"),
                    "model_p": round(s_other["p"], 4),
                    "raw_model_p": None,  # 另一侧无 LLM 声明概率，不造代理指标
                    "market_implied": round(s_other["implied"], 4),
                    "edge": round(s_other["edge"], 4),
                    "kelly": round(_kelly(s_other["p"], o_odd), 4), "odds": o_odd,
                })
            markets["ou"]["llm_side"] = side
            markets["ou"]["calibrated_suggestion"] = (cmp_ou or {}).get("suggested_side")
            markets["ou"]["calibration"] = cmp_ou
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
            cmp_ah = comparison.get("ah")
            if cmp_ah and cmp_ah["status"] == "AVAILABLE":
                other = "away" if cmp_ah["llm_side"] == "home" else "home"
                s_other = cmp_ah["sides"][other]
                other_label = "客队" if other == "away" else "主队"
                o_odd = mkt_ah[f"{other}_side"]
                markets["ah"]["selections"].append({
                    "side": other_label,
                    "label": f"{other_label}赢盘",
                    "model_p": round(s_other["p"], 4),
                    "raw_model_p": None,
                    "market_implied": round(s_other["implied"], 4),
                    "edge": round(s_other["edge"], 4),
                    "kelly": round(_kelly(s_other["p"], o_odd), 4), "odds": o_odd,
                    "handicap_num": hc, "handicap_team": team,
                })
            markets["ah"]["llm_side"] = (cmp_ah or {}).get("llm_side")
            markets["ah"]["calibrated_suggestion"] = (cmp_ah or {}).get("suggested_side")
            markets["ah"]["calibration"] = cmp_ah
        else:
            markets["ah"] = {"available": False}
    else:
        markets["ah"] = {"available": False}

    return {"fixture_id": fixture_id, "margin": margin, "markets": markets}
