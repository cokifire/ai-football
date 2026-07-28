"""
历史回测脚本：用已回填的 predictions（含实际赛果 + 模型概率）与 odds 表（真实赔率）
回测多种下注策略的 ROI。

覆盖市场：
  - 1X2 胜负平（使用 xgb 概率 win_home/draw/away + 市场 1X2 赔率）
  - 大小球（使用 llm_ou_type / llm_ou_line / llm_ou_pct + 市场大小球赔率）
  - 让盘（使用 llm_handicap_num / team / llm_handicap_pct + 市场亚盘赔率）
  - 比分（无赔率数据，用历史命中率 + 假设赔率做期望/ROI 模拟）

策略：
  A. 直接跟单：每场下模型最自信的一方
  B. 置信度分位过滤：按模型置信度降序，只下头部 X% 的比赛（跟单）
  C. 价值投注：仅当下 model_p > market_implied + margin 时，按 Kelly 比例下注

运行：
  cd backend
  python tools/backtest_strategies.py
  python tools/backtest_strategies.py --top 0.5 --margin 0.03
"""
import argparse
import json
import os
import sys
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.db.session import SessionLocal
from sqlalchemy import text

from app.services.calibration_service import apply_calibration


# ── 数据加载 ────────────────────────────────────────────────────────────────
def load_predictions(db):
    rows = db.execute(text("""
        SELECT p.fixture_id,
               p.win_home, p.win_draw, p.win_away,
               p.over25_prob,
               p.llm_win, p.llm_win_pct,
               p.llm_ou_type, p.llm_ou_line, p.llm_ou_pct,
               p.llm_handicap_num, p.llm_handicap_team, p.llm_handicap_pct,
               p.llm_score, p.top3_scores, p.score_in_top3,
               p.actual_home_goals, p.actual_away_goals
        FROM predictions p
        WHERE p.actual_home_goals IS NOT NULL AND p.actual_away_goals IS NOT NULL
    """)).fetchall()
    return rows


def load_odds(db):
    res = db.execute(text("SELECT fixture_id, odds_data FROM odds")).fetchall()
    d = {}
    for fid, od in res:
        if isinstance(od, (str, bytes, bytearray)):
            try:
                od = json.loads(od)
            except Exception:
                continue
        d.setdefault(fid, []).append(od)
    return d


def consensus_from_snapshots(snaps):
    """合并一个 fixture 的全部赔率快照，返回各市场共识（取原始赔率中位数）。

    1X2: {home, draw, away} 原始 decimal 赔率
    ou:  {line: {over, under}}  原始赔率
    ah:  {mline: {home_side, away_side}}  原始赔率，mline 为主队视角让球数（负=主让）
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
                    # 统一到主队视角让球数（负=主让）：API "Home X" -> 主让X -> mline=-X
                    mline = -ln
                    g = ah.setdefault(mline, {"home_side": [], "away_side": []})
                    if ln >= 0:  # API Home X：主队侧=home_raw
                        hs, as_ = e.get("ah_home_raw"), e.get("ah_away_raw")
                    else:       # API Away X：主队侧=away_raw
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


# ── 赛果判定 ────────────────────────────────────────────────────────────────
def actual_outcome(gh, ga):
    if gh > ga:
        return "home"
    if gh == ga:
        return "draw"
    return "away"


def actual_over_under(gh, ga, line):
    total = gh + ga
    if total > line:
        return "over"
    if total < line:
        return "under"
    return None  # 走水


def actual_handicap(gh, ga, hc_val):
    adjusted = (gh or 0) + hc_val
    if adjusted > (ga or 0):
        return True
    if adjusted < (ga or 0):
        return False
    return None  # 走水


# ── 回测核心 ────────────────────────────────────────────────────────────────
def evaluate(bets):
    """bets: list of {stake, odds, win}。return={n,hits,hit_rate,stake,ret,profit,roi}"""
    n = len(bets)
    if n == 0:
        return {"n": 0, "hits": 0, "hit_rate": None, "stake": 0, "ret": 0,
                "profit": 0, "roi": None}
    stake = sum(b["stake"] for b in bets)
    ret = sum(b["stake"] * b["odds"] if b["win"] else 0 for b in bets)
    profit = ret - stake
    hits = sum(1 for b in bets if b["win"])
    return {
        "n": n, "hits": hits,
        "hit_rate": hits / n,
        "stake": stake, "ret": round(ret, 1),
        "profit": round(profit, 1),
        "roi": profit / stake if stake else 0,
    }


def kelly_fraction(model_p, odds):
    """Kelly 比例 = (p*o - 1)/(o - 1)，下限 0。"""
    if odds <= 1:
        return 0.0
    f = (model_p * odds - 1) / (odds - 1)
    return max(0.0, f)


def parse_pct(v):
    """兼容 '53%' / 0.53 / None 等多种存储形式，统一返回 0-1 概率。"""
    if v is None:
        return None
    s = str(v).strip().replace("%", "").replace("％", "")
    try:
        f = float(s)
    except (ValueError, TypeError):
        return None
    return f / 100.0 if f > 1 else f


# ── 各市场回测 ──────────────────────────────────────────────────────────────
def backtest_1x2(rows_by_fid, odds_map, top_frac, margin, bankroll, kelly_cap, cal=False):
    """返回 (跟单, 置信度分位, 价值投注) 三份 bets 列表。

    cal=True 时对模型概率应用 Platt scaling 校准（仅影响选边与 edge/Kelly）。
    """
    follow, conf, value = [], [], []
    scored = []  # (confidence, bet_for_follow)
    for fid, row in rows_by_fid.items():
        c = odds_map.get(fid)
        if not c or not c["1x2"]:
            continue
        wh = float(row["win_home"] or 0)
        wd = float(row["win_draw"] or 0)
        wa = float(row["win_away"] or 0)
        if wh + wd + wa <= 0:
            continue
        mkt = c["1x2"]
        gh, ga = int(row["actual_home_goals"]), int(row["actual_away_goals"])
        actual = actual_outcome(gh, ga)

        probs = {"home": wh, "draw": wd, "away": wa}
        if cal:
            probs = {s: apply_calibration("1x2_" + s, p) for s, p in probs.items()}
        best = max(probs, key=probs.get)
        conf_best = probs[best]
        odds_best = mkt[best]

        # 跟单
        scored.append((conf_best, {
            "stake": 1.0, "odds": odds_best, "win": actual == best}))

        # 价值投注：每场只下 edge 最大且 > margin 的一方
        best_edge, best_bet = -1, None
        for side in ("home", "draw", "away"):
            o = mkt[side]
            if o <= 1:
                continue
            mkt_implied = 1 / o
            edge = probs[side] - mkt_implied
            if edge > margin and edge > best_edge:
                f = min(kelly_cap, kelly_fraction(probs[side], o))
                if f > 0:
                    best_edge, best_bet = edge, {
                        "stake": f * bankroll, "odds": o, "win": actual == side}
        if best_bet:
            value.append(best_bet)

    # 跟单全集
    follow = [b for _, b in scored]
    # 置信度分位
    scored.sort(key=lambda x: x[0], reverse=True)
    k = int(len(scored) * top_frac)
    conf = [b for _, b in scored[:k]]
    return follow, conf, value


def backtest_ou(rows_by_fid, odds_map, top_frac, margin, bankroll, kelly_cap, cal=False):
    follow, conf, value = [], [], []
    scored = []
    for fid, row in rows_by_fid.items():
        c = odds_map.get(fid)
        if not c:
            continue
        ou_type = (row["llm_ou_type"] or "").strip()
        line = row["llm_ou_line"]
        if not ou_type or line is None:
            continue
        line = float(line)
        mkt = c["ou"].get(line)
        if not mkt:
            continue
        # 模型概率：优先 llm_ou_pct，否则 over25_prob 仅当 line==2.5
        model_p = None
        cal_key = "ou"
        if row["llm_ou_pct"] is not None:
            model_p = parse_pct(row["llm_ou_pct"])
        elif line == 2.5 and row["over25_prob"] is not None:
            model_p = float(row["over25_prob"]) if "大" in ou_type else 1 - float(row["over25_prob"])
            cal_key = "ou25"

        gh, ga = int(row["actual_home_goals"]), int(row["actual_away_goals"])
        actual = actual_over_under(gh, ga, line)
        if actual is None:
            continue  # 走水跳过
        side = "over" if "大" in ou_type else "under"
        o = mkt[side]
        win = actual == side

        if model_p is None:
            continue
        if cal:
            model_p = apply_calibration(cal_key, model_p)
        conf_score = model_p if side == "over" else model_p  # 置信度即该方向概率
        scored.append((conf_score, {"stake": 1.0, "odds": o, "win": win}))

        if model_p > margin:
            best_edge = model_p - 1 / o
            f = min(kelly_cap, kelly_fraction(model_p, o))
            if best_edge > margin and f > 0:
                value.append({"stake": f * bankroll, "odds": o, "win": win})

    follow = [b for _, b in scored]
    scored.sort(key=lambda x: x[0], reverse=True)
    k = int(len(scored) * top_frac)
    conf = [b for _, b in scored[:k]]
    return follow, conf, value


def backtest_ah(rows_by_fid, odds_map, top_frac, margin, bankroll, kelly_cap, cal=False):
    follow, conf, value = [], [], []
    scored = []
    for fid, row in rows_by_fid.items():
        c = odds_map.get(fid)
        if not c:
            continue
        hc_val = row["llm_handicap_num"]
        team = (row["llm_handicap_team"] or "").strip()
        if hc_val is None or not team:
            continue
        hc_val = float(hc_val)
        mkt = c["ah"].get(hc_val)
        if not mkt:
            continue
        model_p = parse_pct(row["llm_handicap_pct"])
        if model_p is None:
            continue
        if cal:
            model_p = apply_calibration("ah", model_p)

        gh, ga = int(row["actual_home_goals"]), int(row["actual_away_goals"])
        covers = actual_handicap(gh, ga, hc_val)
        if covers is None:
            continue  # 走水跳过

        # team 为预测方：主队下 home_side，客队下 away_side
        if team == "客队":
            win = not covers  # 客队视角与主队视角相反
            o = mkt["away_side"]
            conf_score = model_p
        else:
            win = covers
            o = mkt["home_side"]
            conf_score = model_p

        scored.append((conf_score, {"stake": 1.0, "odds": o, "win": win}))
        edge = model_p - 1 / o
        f = min(kelly_cap, kelly_fraction(model_p, o))
        if edge > margin and f > 0:
            value.append({"stake": f * bankroll, "odds": o, "win": win})

    follow = [b for _, b in scored]
    scored.sort(key=lambda x: x[0], reverse=True)
    k = int(len(scored) * top_frac)
    conf = [b for _, b in scored[:k]]
    return follow, conf, value


def backtest_score(rows):
    """比分无赔率数据：用历史命中率 + 假设赔率做期望/ROI 模拟。"""
    n = 0
    top1_hit = 0
    top3_hit = 0
    for row in rows:
        llm_score = (row["llm_score"] or "").strip()
        if not llm_score:
            continue
        gh, ga = int(row["actual_home_goals"]), int(row["actual_away_goals"])
        actual = f"{gh}-{ga}"
        n += 1
        scores = [s.strip().replace("：", "-").replace(":", "-")
                  for s in llm_score.split(",")]
        if scores and scores[0] == actual:
            top1_hit += 1
        if row["score_in_top3"]:
            top3_hit += 1
    if n == 0:
        return None
    top1_rate = top1_hit / n
    top3_rate = top3_hit / n

    sim = {}
    for o in (6, 8, 10):
        # 单注 Top1：成本 1，命中返 o
        roi1 = top1_rate * o - 1
        # 三注 Top3：成本 3，命中仅 1 注返 o（其余 2 注归零）
        roi3 = (top3_rate * o) - 3
        sim[o] = {
            "top1_rate": top1_rate, "top3_rate": top3_rate,
            "top1_roi": roi1, "top3_roi": roi3,
        }
    return {"n": n, "top1_hit": top1_hit, "top3_hit": top3_hit,
            "top1_rate": top1_rate, "top3_rate": top3_rate, "sim": sim}


# ── 报告 ────────────────────────────────────────────────────────────────────
def fmt_pct(x):
    return f"{x*100:.1f}%" if x is not None else "-"


def fmt_roi(x):
    return f"{x*100:+.1f}%" if x is not None else "-"


def print_market(name, raw, cal):
    """raw/cal 各为 (follow, conf, value) 三份 bets；分别打印原始与校准 ROI。"""
    print(f"\n── {name} ─────────────────────────────────────────────")
    print(f"  {'策略':<10} {'模式':<6} {'场数':>5} {'命中率':>8} {'投入':>8} {'返还':>9} {'盈亏':>9} {'ROI':>9}")
    for label, rb, cb in (("跟单", raw[0], cal[0]),
                          ("置信Top", raw[1], cal[1]),
                          ("价值投注", raw[2], cal[2])):
        for mode, bets in (("原始", rb), ("校准", cb)):
            r = evaluate(bets)
            if r["n"] == 0:
                print(f"  {label:<10} {mode:<6} {'-':>5} {'无数据':>8}")
                continue
            print(f"  {label:<10} {mode:<6} {r['n']:>5} {fmt_pct(r['hit_rate']):>8} "
                  f"{r['stake']:>8.1f} {r['ret']:>9.1f} {r['profit']:>+9.1f} {fmt_roi(r['roi']):>9}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=float, default=0.5, help="置信度分位过滤比例 (0-1)")
    ap.add_argument("--margin", type=float, default=0.03, help="价值投注 edge 阈值")
    ap.add_argument("--bankroll", type=float, default=100.0, help="Kelly 注码基准（单位）")
    ap.add_argument("--kelly-cap", type=float, default=0.25, help="单注 Kelly 上限")
    ap.add_argument("--no-cal", action="store_true", help="不跑校准版（仅原始概率）")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        rows = load_predictions(db)
        odds_all = load_odds(db)
    finally:
        db.close()

    print(f"已完赛且有赛果的 predictions: {len(rows)} 场")
    print(f"odds 表有赔率的 fixture: {len(odds_all)} 场")

    # 仅保留有 actual 结果的 prediction 行，且能解析为 dict
    rows_by_fid = {}
    for r in rows:
        d = dict(r._mapping)
        rows_by_fid[d["fixture_id"]] = d

    odds_map = {}
    aligned = 0
    for fid, snaps in odds_all.items():
        if fid not in rows_by_fid:
            continue
        c = consensus_from_snapshots(snaps)
        if c["1x2"] or c["ou"] or c["ah"]:
            odds_map[fid] = c
            aligned += 1
    print(f"其中可对齐赔率(有任一笔市场)的场次: {aligned} 场")

    if not odds_map:
        print("\n⚠ 没有可用于价值投注回测的赔率数据，仅输出比分模拟。")
    else:
        f1, cf1, v1 = backtest_1x2(rows_by_fid, odds_map, args.top,
                                   args.margin, args.bankroll, args.kelly_cap, False)
        fo, cfo, vo = backtest_ou(rows_by_fid, odds_map, args.top,
                                  args.margin, args.bankroll, args.kelly_cap, False)
        fa, cfa, va = backtest_ah(rows_by_fid, odds_map, args.top,
                                  args.margin, args.bankroll, args.kelly_cap, False)

        print(f"\n参数: 置信分位={args.top}, edge阈值={args.margin}, "
              f"Kelly基准={args.bankroll}单位, 单注上限={args.kelly_cap}")

        if args.no_cal:
            print_market("胜负平 (1X2)", (f1, cf1, v1), (f1, cf1, v1))
            print_market("大小球 (Over/Under)", (fo, cfo, vo), (fo, cfo, vo))
            print_market("让球盘 (Asian Handicap)", (fa, cfa, va), (fa, cfa, va))
        else:
            f1c, cf1c, v1c = backtest_1x2(rows_by_fid, odds_map, args.top,
                                          args.margin, args.bankroll, args.kelly_cap, True)
            foc, cfoc, voc = backtest_ou(rows_by_fid, odds_map, args.top,
                                         args.margin, args.bankroll, args.kelly_cap, True)
            fac, cfac, vac = backtest_ah(rows_by_fid, odds_map, args.top,
                                         args.margin, args.bankroll, args.kelly_cap, True)
            print_market("胜负平 (1X2)", (f1, cf1, v1), (f1c, cf1c, v1c))
            print_market("大小球 (Over/Under)", (fo, cfo, vo), (foc, cfoc, voc))
            print_market("让球盘 (Asian Handicap)", (fa, cfa, va), (fac, cfac, vac))

            # 价值投注校准收益对比
            print(f"\n── 价值投注：校准前后 ROI 对比 ────────────────────────")
            print(f"  {'市场':<16} {'原始ROI':>9} {'校准ROI':>9} {'ΔROI':>9}")
            for nm, vr, vrc in (("1X2", v1, v1c), ("大小球", vo, voc), ("让球盘", va, vac)):
                rr = evaluate(vr)["roi"] if evaluate(vr)["n"] else 0
                rc = evaluate(vrc)["roi"] if evaluate(vrc)["n"] else 0
                print(f"  {nm:<16} {fmt_roi(rr):>9} {fmt_roi(rc):>9} {fmt_roi(rc - rr):>9}")

    # 比分模拟（不需要赔率）
    sc = backtest_score(list(rows_by_fid.values()))
    if sc:
        print(f"\n── 比分（无赔率，按历史命中率 + 假设赔率模拟） ──────────────")
        print(f"  样本 {sc['n']} 场 | Top1命中 {sc['top1_hit']} ({fmt_pct(sc['top1_rate'])}) "
              f"| Top3命中 {sc['top3_hit']} ({fmt_pct(sc['top3_rate'])})")
        print(f"  {'赔率':>5} {'Top1单注ROI':>12} {'Top3三注ROI':>12}")
        for o, s in sc["sim"].items():
            print(f"  {o:>5} {fmt_roi(s['top1_roi']):>12} {fmt_roi(s['top3_roi']):>12}")
        print("  说明: Top1单注成本1命中返o；Top3三注成本3仅1注命中返o。")

    print("\n完成。")


if __name__ == "__main__":
    main()
