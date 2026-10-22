"""
市场侧工具：去水、隐含 λ/T 反推、EV 表、报价一致性自检
======================================================
依赖：core.py 同目录
"""
from math import exp, factorial
exec(open('core.py').read())

RHO = -0.03


def report_books(books, names=None):
    """逐机构打印去水结果与水位，返回均值 [ph, pd, pa]"""
    acc = [0.0, 0.0, 0.0]
    print("  %-18s %6s %6s %6s %8s | %7s %7s %7s"
          % ("机构", "主", "平", "客", "水位", "主%", "平%", "客%"))
    for row in books:
        n, h, d, a = row
        p = devig(h, d, a)
        print("  %-18s %6.2f %6.2f %6.2f %7.2f%% | %6.1f%% %6.1f%% %6.1f%%"
              % (n, h, d, a, overround(h, d, a) * 100,
                 p[0] * 100, p[1] * 100, p[2] * 100))
        for i in range(3):
            acc[i] += p[i]
    m = [x / len(books) for x in acc]
    print("  %-18s %28s | %6.1f%% %6.1f%% %6.1f%%"
          % ("均值", "", m[0] * 100, m[1] * 100, m[2] * 100))
    return m


def market_implied(m, verbose=True):
    """由市场三路反推 λ / T / 派生盘口"""
    lh, la, rr, T, resid = fit_market_lambda(m, RHO)
    if verbose:
        P = lambda x: f"{100*x:.1f}%"
        share = m[0] / (m[0] + m[2])
        print(f"  市场隐含 λ {lh:.3f}/{la:.3f}  T {T:.3f}  残差 {resid:.5f}"
              f" → {P(rr['h'])}/{P(rr['d'])}/{P(rr['a'])}")
        print(f"  两路份额(主) {share:.4f} → 等效 Elo 差(含HFA) "
              f"{400*(1 if share>0.5 else -1)*abs(__import__('math').log10((1-share)/share if share!=0.5 else 1)):+.0f}")
        print("  市场 O1.5 %s O2.5 %s O3.5 %s O4.5 %s BTTS %s | 0-0 %s" % (
            P(rr['ou'][1.5]), P(rr['ou'][2.5]), P(rr['ou'][3.5]),
            P(rr['ou'][4.5]), P(rr['btts']), P(rr['sc'][(0, 0)])))
        print("  市场 TOP6: " + " ".join(
            f"{i}-{j} {P(v)}" for (i, j), v in top_scores(rr, 6)))
    return lh, la, rr, T, resid


def ev_table(res, rows, label_width=16):
    """EV 表。

    rows = [(市场名, 模型概率 或 None, 赔率, 备注), ...]
    概率为 None 时（走水类盘口）不给 EV。
    """
    print("  %-*s %9s %9s %10s" % (label_width, "市场", "模型概率", "最佳赔率", "名义EV"))
    for name, p, o, *_ in rows:
        ps = "    —    " if p is None else f"{p*100:7.1f}%"
        ev = "     —    " if p is None else f"{p*o*100-100:+9.1f}%"
        print("  %-*s %s %9.2f %s" % (label_width, name, ps, o, ev))


def consistency_check(m, ou25_odds):
    """市场自检：1X2 反推的 T 与大小球直接报价对应的 T 是否一致。

    ou25_odds = (over_price, under_price)
    返回 (T_from_1x2, O2.5_from_1x2, O2.5_direct, 差值pp)
    """
    lh, la, rr, T, _ = fit_market_lambda(m, RHO)
    o25_1x2 = rr['ou'][2.5]
    o, u = ou25_odds
    o25_direct = (1 / o) / (1 / o + 1 / u)
    print(f"  1X2 反推 T = {T:.3f} → O2.5 {o25_1x2*100:.1f}%")
    print(f"  O/U 直接去水      → O2.5 {o25_direct*100:.1f}%")
    print(f"  差值 {(o25_direct-o25_1x2)*100:+.1f}pp"
          f"（>5pp 说明市场内部口径不一致，需在报告 §10 记录）")
    return T, o25_1x2, o25_direct, (o25_direct - o25_1x2) * 100
