"""
football-bayes 模型内核 v2.0
============================
纯 Python / 无第三方依赖。所有脚本通过 `exec(open('core.py').read())` 引入。

!!! 方向约定（最容易出错的地方）!!!
  model(lh, la)         ->  dict, lh = 主队 λ, la = 客队 λ
  res['sc'][(i, j)]     ->  比分 i-j（主 i / 客 j）的概率
  ah(res, L)            ->  **主队受让 L 球** 时主队的覆盖概率（含走水半注）
                            因此「客队受让 +L」的覆盖概率 = 1 - ah(res, -L)
  ah(res, 0)            ->  主队 DNB 的覆盖概率（平局半注）
"""

from math import exp, factorial, log10


# ---------------------------------------------------------------- 基本分布
def pois(k, lam):
    """泊松概率质量函数"""
    return exp(-lam) * lam ** k / factorial(k)


def tau(i, j, lh, la, rho=-0.03):
    """Dixon-Coles 低分修正。rho 固定 -0.03（见 references/pitfalls.md）"""
    if i == 0 and j == 0: return 1 - lh * la * rho
    if i == 0 and j == 1: return 1 + lh * rho
    if i == 1 and j == 0: return 1 + la * rho
    if i == 1 and j == 1: return 1 - rho
    return 1.0


def model(lh, la, rho=-0.03, maxk=12):
    """构建比分矩阵并返回全部派生量。

    返回 dict:
      h, d, a      主胜 / 平 / 客胜
      btts         双方都进球
      ou[L]        float 键 1.5 / 2.5 / 3.5 / 4.5  —— **不要用整数 2 索引**
      sc[(i,j)]    比分概率矩阵
      T, lh, la    总进球与 λ
    """
    mat = {}
    for i in range(maxk + 1):
        for j in range(maxk + 1):
            mat[(i, j)] = pois(i, lh) * pois(j, la) * tau(i, j, lh, la, rho)
    s = sum(mat.values())
    for k in mat:
        mat[k] /= s
    h = sum(v for (i, j), v in mat.items() if i > j)
    d = sum(v for (i, j), v in mat.items() if i == j)
    a = sum(v for (i, j), v in mat.items() if i < j)
    btts = sum(v for (i, j), v in mat.items() if i > 0 and j > 0)
    ou = {}
    for L in (1.5, 2.5, 3.5, 4.5):
        ou[L] = sum(v for (i, j), v in mat.items() if i + j > L)
    return dict(h=h, d=d, a=a, btts=btts, ou=ou, sc=mat, T=lh + la, lh=lh, la=la)


def solve_lambda(share, total, rho=-0.03):
    """已知「两路份额 + 总进球」反解 λ。

    share = P(主胜) / (P(主胜) + P(客胜))
    total = lh + la
    返回 (lh, la, model_dict)
    """
    lo, hi = 0.05, total - 0.05
    for _ in range(60):
        mid = (lo + hi) / 2
        r = model(mid, total - mid, rho)
        s = r['h'] / (r['h'] + r['a'])
        if s < share:
            lo = mid
        else:
            hi = mid
    lh = (lo + hi) / 2
    return lh, total - lh, model(lh, total - lh, rho)


def two_way(dr):
    """Elo 差值 -> 两路（非平局）胜率。dr = 主队 Elo + HFA - 客队 Elo"""
    return 1.0 / (10 ** (-dr / 400.0) + 1.0)


# ---------------------------------------------------------------- 亚洲盘
def ah_unit(res, L):
    """单条盘口线的赢/走水概率。L = 给主队的让球数（正 = 主队受让）。"""
    w = l = 0.0
    for (i, j), v in res['sc'].items():
        d = i - j + L
        if d > 0:   w += v
        elif d == 0: l += v
    return w, l


def ah(res, line):
    """亚洲盘覆盖概率（主队视角）。四分之一盘在相邻半球盘间线性插值。"""
    b = int(line // 1)
    frac = line - b
    if abs(frac) < 1e-9:
        w, l = ah_unit(res, line); return w + l / 2
    if abs(frac - 0.5) < 1e-9:
        w, l = ah_unit(res, b + 0.5); return w + l / 2
    if abs(frac - 0.25) < 1e-9:
        w1, l1 = ah_unit(res, b)
        w2, l2 = ah_unit(res, b + 0.5)
        return 0.5 * (w1 + l1 / 2) + 0.5 * (w2 + l2 / 2)
    if abs(frac - 0.75) < 1e-9:
        w1, l1 = ah_unit(res, b + 0.5)
        w2, l2 = ah_unit(res, b + 1)
        return 0.5 * (w1 + l1 / 2) + 0.5 * (w2 + l2 / 2)
    raise ValueError(line)


def home_giving(res, L):
    """【推荐】主队让 L 球的覆盖概率。等价于 ah(res, -L)。"""
    return ah(res, -L)


def away_receiving(res, L):
    """【推荐】客队受让 +L 球的覆盖概率。等价于 1 - ah(res, -L)。"""
    return 1 - ah(res, -L)


def ev_ah_minus1(res, odds):
    """让 1 球亚盘（−1.0 AH）的名义 EV。

    正确口径：赢 2 球+ 按**净赔率** (odds-1) 结算；赢 1 球走水；平/负全亏。
        EV = w2 * (odds - 1) - (d + a)
    错误口径 w2*odds - (d+a) 会虚高恰好 w2*100 个百分点。
    """
    w2 = sum(v for k, v in res['sc'].items() if k[0] - k[1] >= 2)
    return w2 * (odds - 1) - (res['d'] + res['a'])


# ---------------------------------------------------------------- 市场
def devig(h, d, a):
    """单机构 1X2 去水（按隐含概率比例缩放）。返回 (ph, pd, pa)。"""
    ih, idd, ia = 1 / h, 1 / d, 1 / a
    s = ih + idd + ia
    return ih / s, idd / s, ia / s


def devig_mean(books):
    """books = [(name, h, d, a), ...] -> 各机构去水后取均值"""
    acc = [0.0, 0.0, 0.0]
    for _, h, d, a in books:
        p = devig(h, d, a)
        for i in range(3):
            acc[i] += p[i]
    return [x / len(books) for x in acc]


def overround(h, d, a):
    """水位（博彩公司抽水率）"""
    return 1 / h + 1 / d + 1 / a - 1


def fit_market_lambda(m, rho=-0.03, lo=1.5, hi=6.0, step=0.02):
    """由市场三路概率反推 λ 与 T。

    m = [ph, pd, pa]（已去水）。在 T 上做网格搜索，两路份额由 m 固定。
    返回 (lh, la, model_dict, T, residual)
    """
    share = m[0] / (m[0] + m[2])
    best, bl = 1e9, None
    T = lo
    while T <= hi:
        lh, la, rr = solve_lambda(share, T, rho)
        e = (rr['h'] - m[0]) ** 2 + (rr['d'] - m[1]) ** 2 + (rr['a'] - m[2]) ** 2
        if e < best:
            best, bl = e, (lh, la, rr, T)
        T += step
    lh, la, rr, T = bl
    return lh, la, rr, T, best


# ---------------------------------------------------------------- 桥接
def bridge_lambda(xg_for, xga_against, LG, w=1.0, f=1.0):
    """xG -> λ 的比率桥接。

        λ = (xg_for^w * xga_against^w) / LG^(2w - 1) * f

    w = 1.0  比率桥接（默认；对强队不压缩）
    w = 0.5  几何平均（**有偏**，会把 λ 往联赛均值拉，系统性低估强队）
    f        该队转化率因子
    """
    return (xg_for ** w) * (xga_against ** w) / (LG ** (2 * w - 1)) * f


def finishing_factor(goals, xg, league_conv, shrink=0.7):
    """转化率因子：本队实际转化率向联赛均值收缩。

    f = shrink * league_conv + (1 - shrink) * (goals / xg)
    """
    return shrink * league_conv + (1 - shrink) * (goals / xg)


def cross_league_lambda(xg_h, LG_h, xga_a, LG_a, LG_ref, f_h):
    """跨联赛桥接（两队分属不同联赛时使用）。

    先把双方各自归一化到自己的联赛基线，再乘统一参考基线 LG_ref。
    """
    return (xg_h / LG_h) * (xga_a / LG_a) * LG_ref * f_h


def bridge_backtest(matches, LG, ws=(0.5, 0.75, 1.0)):
    """桥接权重 w 的逐场回测。

    matches = [(xg_for_i, opp_xga_per_game_i), ...]  （不含转化率因子）
    返回 {w: predicted_goals}
    """
    out = {}
    for w in ws:
        out[w] = sum((xg * oxga) ** w / LG ** (2 * w - 1) for xg, oxga in matches)
    return out


# ---------------------------------------------------------------- 剧本 / 汇总
def scripts(res):
    """剧本四分（A/B/C/D 合计 100%）。返回 dict。"""
    sc = res['sc']
    A = sum(v for k, v in sc.items() if k[0] - k[1] >= 2)   # 主队两球+
    B = sum(v for k, v in sc.items() if k[0] - k[1] == 1)   # 主队一球胜
    C = sum(v for k, v in sc.items() if k[0] == k[1])       # 平局
    D = sum(v for k, v in sc.items() if k[1] - k[0] >= 1)   # 客队取胜
    return dict(A=A, B=B, C=C, D=D, total=A + B + C + D)


def top_scores(res, n=10):
    return sorted(res['sc'].items(), key=lambda x: -x[1])[:n]


def margins(res):
    """胜负差分布"""
    sc = res['sc']
    return {
        'home_2plus': sum(v for k, v in sc.items() if k[0] - k[1] >= 2),
        'home_1':     sum(v for k, v in sc.items() if k[0] - k[1] == 1),
        'draw':       res['d'],
        'away_1':     sum(v for k, v in sc.items() if k[1] - k[0] == 1),
        'away_2':     sum(v for k, v in sc.items() if k[1] - k[0] == 2),
        'away_3plus': sum(v for k, v in sc.items() if k[1] - k[0] >= 3),
    }


def clean_sheets(res):
    sc = res['sc']
    return {
        'home_cs': sum(v for k, v in sc.items() if k[1] == 0),
        'away_cs': sum(v for k, v in sc.items() if k[0] == 0),
        'home_scores': 1 - sum(v for k, v in sc.items() if k[0] == 0),
        'away_scores': 1 - sum(v for k, v in sc.items() if k[1] == 0),
    }


def team_goal_prob(lam, k):
    """P(该队进球 >= k)"""
    return 1 - sum(exp(-lam) * lam ** i / factorial(i) for i in range(k))


def pct(x):
    return f"{100 * x:.1f}%"
