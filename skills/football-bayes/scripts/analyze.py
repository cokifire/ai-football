"""
端到端驱动模板（改 CONFIG 即可复用）
====================================
用法：
    python3 analyze.py           # 直接跑（先改下方 CONFIG）
或在新脚本里：
    cfg = dict(...); exec(open('analyze.py').read().split('CONFIG =')[0])
    run_full(cfg)

依赖：core.py 在同目录。
"""
import statistics as st
from math import exp, factorial, log10
exec(open('core.py').read())

RHO = -0.03


# ============================================================ 配置区 ======
CONFIG = {
    # --- 名称 ---
    'home': 'HOME', 'away': 'AWAY',

    # --- Elo 锚 ---
    'elo_h': 1600, 'elo_a': 1600,
    'hfa': 85,              # 主场优势（加到主队）
    'elo_weight': 0.6,      # P0 中 Elo 锚的权重（1-它为 xG 锚权重）

    # --- xG 锚（同联赛；跨联赛改用 cross=True）---
    'cross': False,
    # 同联赛：
    'LG': 1.60, 'LG_ref': None,
    'xg_h': 1.50, 'xga_h': 1.20,     # 主队 每场
    'xg_a': 1.30, 'xga_a': 1.30,     # 客队 每场
    'xg_h_home': None, 'xga_h_home': None,   # 主队主场拆分（可为 None）
    'xg_a_away': None, 'xga_a_away': None,   # 客队客场拆分（可为 None）
    # 跨联赛：
    'LG_h': None, 'LG_a': None,
    # 桥接
    'w': 1.0,               # 桥接权重
    'alpha': 0.25,          # 主客场拆分收缩指数（0 = 不拆分）
    # 转化率
    'goals_h': None, 'xg_total_h': None,   # 主队赛季进球 / 赛季 xG（用于 f）
    'goals_a': None, 'xg_total_a': None,
    'conv_h_league': 0.95, 'conv_a_league': 0.95,

    # --- 总进球（仅在 xG 锚不可得时才需要显式指定）---
    'T_override': None,

    # --- 似然净修正（pp）---
    'NET': (0.0, 0.0, 0.0),

    # --- 稳健性网格 ---
    'grid': dict(hfa=(70, 85, 100), w=(0.5, 0.75, 1.0),
                 we=(0.4, 0.6, 0.8), alpha=(0.0, 0.25, 0.5),
                 upd=(True, False)),
}
# <<< END CONFIG >>>


# ============================================================ 引擎 ========
def _shrink(season, split, alpha):
    if split is None or alpha == 0:
        return season
    return season * (split / season) ** alpha


def build(c):
    """返回 (lh, la, T, rx_model, elo_share, elo_model)"""
    # ---- 转化率因子
    def f(g, xgt, conv):
        if g is None:
            return 1.0
        return finishing_factor(g, xgt, conv)

    f_h = f(c['goals_h'], c['xg_total_h'], c['conv_h_league'])
    f_a = f(c['goals_a'], c['xg_total_a'], c['conv_a_league'])

    if c['cross']:
        # 跨联赛：不做主客场拆分（拆分归属通常不可靠）
        lh = cross_league_lambda(c['xg_h'], c['LG_h'], c['xga_a'], c['LG_a'],
                                 c['LG_ref'] or 1.65, f_h)
        la = cross_league_lambda(c['xg_a'], c['LG_a'], c['xga_h'], c['LG_h'],
                                 c['LG_ref'] or 1.65, f_a)
    else:
        xh = _shrink(c['xg_h'], c['xg_h_home'], c['alpha'])
        ah_ = _shrink(c['xga_a'], c['xga_a_away'], c['alpha'])
        xa = _shrink(c['xg_a'], c['xg_a_away'], c['alpha'])
        hh = _shrink(c['xga_h'], c['xga_h_home'], c['alpha'])
        lh = bridge_lambda(xh, ah_, c['LG'], c['w'], f_h)
        la = bridge_lambda(xa, hh, c['LG'], c['w'], f_a)

    T = c['T_override'] if c['T_override'] else lh + la
    rx = model(lh, la, RHO)
    share = two_way(c['elo_h'] + c['hfa'] - c['elo_a'])
    _, _, re_ = solve_lambda(share, T, RHO)
    return lh, la, T, rx, share, re_, f_h, f_a


def run_full(c=None, verbose=True):
    c = dict(CONFIG, **(c or {}))
    lh, la, T, rx, share, re_, f_h, f_a = build(c)

    we = c['elo_weight']
    p0 = (we * re_['h'] + (1 - we) * rx['h'],
          we * re_['d'] + (1 - we) * rx['d'],
          we * re_['a'] + (1 - we) * rx['a'])
    NET = c['NET']
    p1 = tuple(p0[i] + NET[i] / 100 for i in range(3))
    _, _, r = solve_lambda(p1[0] / (p1[0] + p1[2]), T, RHO)

    if not verbose:
        return dict(p0=p0, p1=p1, r=r, T=T, lh=lh, la=la,
                    share=share, rx=rx, re=re_, f_h=f_h, f_a=f_a)

    P = lambda x: f"{100*x:.1f}%"
    print("=" * 78)
    print(f"【P0 先验】 {c['home']} vs {c['away']}")
    print(f"  xG 锚   λ {lh:.3f}/{la:.3f}  T {T:.3f}  → "
          f"{P(rx['h'])} / {P(rx['d'])} / {P(rx['a'])}")
    print(f"  Elo 锚  份额 {share:.4f} (差 {c['elo_h']+c['hfa']-c['elo_a']:+d})  → "
          f"{P(re_['h'])} / {P(re_['d'])} / {P(re_['a'])}")
    print(f"  两锚分歧(客队侧) {abs(rx['a']-re_['a'])*100:.1f}pp")
    print(f"  P0 → {P(p0[0])} / {P(p0[1])} / {P(p0[2])}")
    print(f"  NET {NET[0]:+.1f}/{NET[1]:+.1f}/{NET[2]:+.1f}  "
          f"P1 → {P(p1[0])} / {P(p1[1])} / {P(p1[2])}")
    print(f"  ★ 模型自洽 λ {r['lh']:.3f}/{r['la']:.3f} T {r['T']:.3f} → "
          f"{P(r['h'])} / {P(r['d'])} / {P(r['a'])}")

    print("\n【总进球 / BTTS】")
    print("  O1.5 %s | O2.5 %s | O3.5 %s | O4.5 %s | BTTS %s | U2.5 %s" % (
        P(r['ou'][1.5]), P(r['ou'][2.5]), P(r['ou'][3.5]),
        P(r['ou'][4.5]), P(r['btts']), P(1 - r['ou'][2.5])))

    print("\n【亚洲盘：客队受让为正】")
    for L in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5):
        print(f"   {c['away']} +{L:<5} 覆盖 {P(away_receiving(r, L))}"
              f"   （{c['home']} -{L} 覆盖 {P(home_giving(r, L))}）")
    print(f"   校验 +0.50 = X2 = {P(r['d']+r['a'])} | "
          f"{c['home']} -0.50 = 主队独胜 = {P(r['h'])}")
    print(f"   1X {P(r['h']+r['d'])} | X2 {P(r['d']+r['a'])} | 12 {P(r['h']+r['a'])}"
          f" | DNB主 {P(ah(r,0))} 客 {P(1-ah(r,0))}")

    print("\n【比分 TOP10】 " + "  ".join(
        f"{i}-{j} {P(v)}" for (i, j), v in top_scores(r, 10)))
    print(f"   0-0 {P(r['sc'][(0,0)])} | 1-1 {P(r['sc'][(1,1)])}")

    cs = clean_sheets(r)
    print(f"   主队零封 {P(cs['home_cs'])} | 客队零封 {P(cs['away_cs'])}"
          f" | 主进球≥1 {P(cs['home_scores'])} | 客进球≥1 {P(cs['away_scores'])}")

    m = margins(r)
    print(f"   主赢2+ {P(m['home_2plus'])} | 主赢1 {P(m['home_1'])}"
          f" | 平 {P(m['draw'])} | 客 {P(r['a'])}")

    print("\n【球队进球】")
    print(f"   {c['home']} λ{r['lh']:.3f}: O0.5 {P(team_goal_prob(r['lh'],1))}"
          f" | O1.5 {P(team_goal_prob(r['lh'],2))}"
          f" | O2.5 {P(team_goal_prob(r['lh'],3))}")
    print(f"   {c['away']} λ{r['la']:.3f}: O0.5 {P(team_goal_prob(r['la'],1))}"
          f" | O1.5 {P(team_goal_prob(r['la'],2))}"
          f" | O2.5 {P(team_goal_prob(r['la'],3))}")

    print("\n【公平赔率】")
    print(f"   {c['home']} {1/r['h']:.2f} | 平 {1/r['d']:.2f} | {c['away']} {1/r['a']:.2f}"
          f" || O2.5 {1/r['ou'][2.5]:.2f} U2.5 {1/(1-r['ou'][2.5]):.2f}"
          f" || BTTS-Y {1/r['btts']:.2f} N {1/(1-r['btts']):.2f}")

    # ---- 稳健性网格
    # 同联赛：扫 HFA × 桥接 w × Elo 权重 × α × 似然
    # 跨联赛：扫 HFA × LG_ref × Elo 权重 × 似然（α 与 w 在跨联赛模式下不生效）
    g = c['grid']
    res = []
    if c['cross']:
        lgs = g.get('LG_ref', (1.40, 1.50, 1.65, 1.80, 1.90))
        for hfa in g['hfa']:
            for lg in lgs:
                for we2 in g['we']:
                    for upd in g['upd']:
                        cc = dict(c, hfa=hfa, LG_ref=lg, elo_weight=we2,
                                  NET=NET if upd else (0, 0, 0))
                        o = run_full(cc, verbose=False)
                        res.append((o['r']['h'], o['r']['d'],
                                    o['r']['a'], o['r']['T']))
    else:
        for hfa in g['hfa']:
            for w in g['w']:
                for we2 in g['we']:
                    for al in g['alpha']:
                        for upd in g['upd']:
                            cc = dict(c, hfa=hfa, w=w, elo_weight=we2,
                                      alpha=al, NET=NET if upd else (0, 0, 0))
                            o = run_full(cc, verbose=False)
                            res.append((o['r']['h'], o['r']['d'],
                                        o['r']['a'], o['r']['T']))
    print(f"\n【稳健性网格 {len(res)} 组】")
    for i, nm in enumerate([c['home'], '平', c['away']]):
        v = [x[i] for x in res]
        print(f"   {nm:<8} [{min(v)*100:.1f}%, {max(v)*100:.1f}%]  中位 {st.median(v)*100:.1f}%")
    vT = [x[3] for x in res]
    print(f"   T        [{min(vT):.3f}, {max(vT):.3f}]  中位 {st.median(vT):.3f}")

    # ---- 剧本
    s = scripts(r)
    print(f"\n【剧本】 A 主两球+ {P(s['A'])} | B 主一球胜 {P(s['B'])}"
          f" | C 平 {P(s['C'])} | D 客取胜 {P(s['D'])} | 合计 {P(s['total'])}")

    return dict(p0=p0, p1=p1, r=r, T=T, lh=lh, la=la,
                share=share, rx=rx, re=re_, grid=res)


if __name__ == '__main__':
    run_full()
