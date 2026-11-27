"""让球/大小球派生值的宿主预计算。

公式与 skills/football-bayes/scripts/core.py 一致（Dixon-Coles ρ=-0.03、
blend_total 恒按 市场65%/模型35% 加权）。此前派生依赖 LLM 在沙箱里 exec
skill 的 core.py，是否真正执行不可控，导致让球/大小球与宿主 P1 脱钩；
改为宿主预计算并注入 prompt（【参考派生值】），LLM 只做判断与例外偏离。
"""
from math import exp, factorial

RHO = -0.03


def _pmf(k, lam):
    return exp(-lam) * lam ** k / factorial(k)


def _tau(i, j, lh, la, rho):
    if i == 0 and j == 0:
        return 1 - lh * la * rho
    if i == 0 and j == 1:
        return 1 + lh * rho
    if i == 1 and j == 0:
        return 1 + la * rho
    if i == 1 and j == 1:
        return 1 - rho
    return 1.0


def poisson_matrix(lh, la, rho=RHO, maxk=12):
    """Dixon-Coles 修正的比分矩阵（归一化），返回 sc/h/d/a/T。"""
    sc = {}
    for i in range(maxk + 1):
        for j in range(maxk + 1):
            sc[(i, j)] = _pmf(i, lh) * _pmf(j, la) * _tau(i, j, lh, la, rho)
    z = sum(sc.values())
    sc = {k: v / z for k, v in sc.items()}
    h = sum(v for (i, j), v in sc.items() if i > j)
    d = sum(v for (i, j), v in sc.items() if i == j)
    a = sum(v for (i, j), v in sc.items() if i < j)
    return {"sc": sc, "h": h, "d": d, "a": a, "T": lh + la}


def solve_lambda(share, total, rho=RHO):
    """已知两路份额 + 总进球，反解 λh/λa。share = P(主胜)/(P(主胜)+P(客胜))。"""
    lo, hi = 0.05, max(total - 0.05, 0.1)
    for _ in range(60):
        mid = (lo + hi) / 2
        r = poisson_matrix(mid, total - mid, rho)
        denom = r["h"] + r["a"]
        s = r["h"] / denom if denom > 0 else 0.5
        if s < share:
            lo = mid
        else:
            hi = mid
    lh = (lo + hi) / 2
    return lh, total - lh, poisson_matrix(lh, total - lh, rho)


def total_from_over25(share, p_over25, target=2.5, rho=RHO):
    """由两路份额 + P(总进球>target) 反解总进球 T。"""
    p = min(max(float(p_over25), 0.01), 0.99)
    lo, hi = 0.5, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        _, _, rr = solve_lambda(share, mid, rho)
        over = sum(v for (i, j), v in rr["sc"].items() if i + j > target)
        if over < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def blend_total(share, p_market_over25, p_model_over25, w_model=0.35, target=2.5, rho=RHO):
    """市场去水 P(>target) 与模型 P(>target) 恒按 (1-w):w 加权后反解 total。"""
    p_blend = (1.0 - float(w_model)) * float(p_market_over25) + float(w_model) * float(p_model_over25)
    return total_from_over25(share, p_blend, target, rho)


def _ah_unit(sc, L):
    """单条盘口线的赢/走水概率。L = 给主队的让球数（正 = 主队受让）。"""
    w = l = 0.0
    for (i, j), v in sc.items():
        d = i - j + L
        if d > 0:
            w += v
        elif d == 0:
            l += v
    return w, l


def ah_cover(sc, line):
    """亚盘主队覆盖概率（走水按半赢计入；line 与 handicap_num 同向：正=客让）。

    四分之一盘在相邻半球盘间线性插值。
    """
    b = int(line // 1)
    frac = line - b
    if abs(frac) < 1e-9:
        w, l = _ah_unit(sc, line)
        return w + l / 2
    if abs(frac - 0.5) < 1e-9:
        w, l = _ah_unit(sc, b + 0.5)
        return w + l / 2
    if abs(frac - 0.25) < 1e-9:
        w1, l1 = _ah_unit(sc, b)
        w2, l2 = _ah_unit(sc, b + 0.5)
        return 0.5 * (w1 + l1 / 2) + 0.5 * (w2 + l2 / 2)
    if abs(frac - 0.75) < 1e-9:
        w1, l1 = _ah_unit(sc, b + 0.5)
        w2, l2 = _ah_unit(sc, b + 1)
        return 0.5 * (w1 + l1 / 2) + 0.5 * (w2 + l2 / 2)
    raise ValueError(line)


def ou_over_cover(sc, line):
    """大球覆盖概率（走水按半赢计入）。四分之一盘拆相邻半盘。"""
    frac = round(line % 1, 4)
    if frac in (0.25, 0.75):
        return 0.5 * ou_over_cover(sc, line - 0.25) + 0.5 * ou_over_cover(sc, line + 0.25)
    over = push = 0.0
    for (i, j), v in sc.items():
        t = i + j
        if t > line:
            over += v
        elif t == line:
            push += v
    return over + push / 2


def convert_ou_prob(share, p_line, line, target=2.5, rho=RHO):
    """把去水大球概率从盘口线 line 换算到 target 线。

    去水概率 ≈ 覆盖概率（大 = P(t>line) + P(t==line)/2，四分之一盘拆相邻半盘），
    二分反解 total 后返回 target 线的覆盖概率。
    用于：odds 没有 2.5 线时，把「最均衡线 L 的 P(>L)」正确换算成 P(>2.5)，
    而不是直接当 P(>2.5) 用（高线时低估 ~15-20pp）。
    """
    p = min(max(float(p_line), 0.01), 0.99)
    lo, hi = 0.5, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        _, _, rr = solve_lambda(share, mid, rho)
        if ou_over_cover(rr["sc"], line) < p:
            lo = mid
        else:
            hi = mid
    total = (lo + hi) / 2
    _, _, res = solve_lambda(share, total, rho)
    return ou_over_cover(res["sc"], target)


def build_derived_context(p1, market_over25_prob, model_over25_prob, cons=None):
    """生成注入 prompt 的【参考派生值】文本；输入不足或计算失败时返回空串。

    p1: {"home","draw","away"} 后验三路概率；share 取 P(主)/(P(主)+P(客))。
    market_over25_prob 为 None 时按 50% 兜底（与 skill 的 blend_total 约定一致）。
    cons: market_odds.consensus 的结果，用于取「两侧隐含概率最接近 50/50」的线。
    """
    try:
        home = float(p1.get("home", 0.0))
        away = float(p1.get("away", 0.0))
    except (TypeError, ValueError, AttributeError):
        return ""
    if home + away <= 0:
        return ""
    share = home / (home + away)
    p_mkt = float(market_over25_prob) if market_over25_prob is not None else 0.5
    p_mod = float(model_over25_prob) if model_over25_prob is not None else p_mkt
    try:
        total = blend_total(share, p_mkt, p_mod)
        lh, la, res = solve_lambda(share, total)
    except Exception:
        return ""
    sc = res["sc"]
    lines = [
        f"share={share:.3f}  total={total:.2f}  λ主={lh:.2f} λ客={la:.2f}",
        f"模型自洽三路: 主{res['h']:.0%} 平{res['d']:.0%} 客{res['a']:.0%}",
        "大小球模型覆盖: " + " ".join(
            f"大{L:g}={ou_over_cover(sc, L):.0%}" for L in (1.5, 2.5, 3.0, 3.5, 4.5)
        ),
    ]
    cons = cons or {}
    ou = cons.get("ou") or {}
    if ou:
        try:
            ln = min(ou, key=lambda l: abs(1 / ou[l]["over"] - 1 / ou[l]["under"]))
            oc = ou_over_cover(sc, float(ln))
            lines.append(f"市场最均衡大小线 {ln:g}: 大={oc:.0%} 小={1 - oc:.0%}")
        except Exception:
            pass
    ahm = cons.get("ah") or {}
    if ahm:
        try:
            ln = float(min(ahm, key=lambda l: abs(1 / ahm[l]["home_side"] - 1 / ahm[l]["away_side"])))
            hc = ah_cover(sc, ln)
            lines.append(f"市场最均衡亚盘线 handicap_num={ln:+.2f}: 主队覆盖={hc:.0%} 客队覆盖={1 - hc:.0%}")
        except Exception:
            pass
    return "\n".join(lines)
