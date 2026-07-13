"""
基于实时赔率(调用 API-Football 查询)预测单场比赛.

流程:
  1. 通过 API 拉取该 fixture 的实时赔率(各博彩公司)
  2. 去除抽水(庄家利润),得到各庄家隐含概率,再取中位数得到"市场共识概率"
  3. 用 Poisson 模型反解主客队预期进球 λ_home / λ_away,使 1X2 概率与共识一致
  4. 生成最可能的 Top3 比分
  5. 结合亚洲让球(Asian Handicap)与大小球(Over/Under)市场给出倾向

作为模块导入时,调用 predict_from_odds(fixture_id) 返回结构化 dict;
作为脚本运行时,默认读取 odds_raw_full.json,也可传入 fixture_id 走实时 API:
  python tools/predict_from_odds.py                 # 读取默认 JSON 文件
  python tools/predict_from_odds.py 1494202         # 走 API 查询实时赔率
  python tools/predict_from_odds.py path/to/od.json # 读取指定 JSON 文件
"""

import json
import os
import sys
from statistics import median

import numpy as np
from scipy.stats import poisson

# 允许脚本以 standalone 方式运行(从 backend 目录解析 app 包)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ODDS_PATH = os.path.join(os.path.dirname(__file__), "..", "odds_raw_full.json")


# ── 实时赔率拉取(API-Football) ──────────────────────────────────────────────
def _api_get(path: str, params: dict, timeout: float = 10.0) -> dict:
    """请求 API-Football 指定接口,返回解析后的 JSON。"""
    from app.core.config import settings
    import httpx
    r = httpx.get(
        f"{settings.api_football_base_url}/{path}",
        headers={"x-apisports-key": settings.api_football_key},
        params=params,
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def fetch_odds_via_api(fixture_id: int) -> dict | None:
    """调用 API 查询该 fixture 的实时赔率,返回含 bookmakers 的 fixture 对象;失败返回 None。"""
    try:
        fx = _api_get("fixtures", {"id": fixture_id}).get("response", [])
        if not fx:
            return None
        date_str = (fx[0].get("fixture", {}) or {}).get("date", "")[:10]
        params = {"fixture": fixture_id}
        if date_str:
            params["date"] = date_str
        data = _api_get("odds", params).get("response", [])
        if not data:
            return None
        return data[0]
    except Exception as e:
        print(f"实时赔率获取失败 fixture_id={fixture_id}: {e}")
        return None


# ── 分析逻辑(与数据来源无关) ───────────────────────────────────────────────
def _load(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 兼容 顶层数组 或 {"response": [...]} 结构
    if isinstance(data, dict) and "response" in data:
        return data["response"]
    if isinstance(data, list):
        return data
    return [data]


def _collect_1x2(fixture: dict) -> list[dict]:
    """收集所有庄家的 Match Winner(Home/Draw/Away) 赔率."""
    rows = []
    for bm in fixture.get("bookmakers", []):
        for bet in bm.get("bets", []):
            if bet.get("name") != "Match Winner":
                continue
            vals = {v["value"]: float(v["odd"]) for v in bet.get("values", [])}
            ho, dr, aw = vals.get("Home"), vals.get("Draw"), vals.get("Away")
            if ho and dr and aw:
                rows.append({
                    "bookmaker": bm.get("name"),
                    "home": ho, "draw": dr, "away": aw,
                })
            break
    return rows


def _implied(rows: list[dict]) -> dict:
    """计算每个庄家的隐含概率(去除抽水),并取中位数作为市场共识."""
    pH, pD, pA = [], [], []
    for r in rows:
        s = 1 / r["home"] + 1 / r["draw"] + 1 / r["away"]
        pH.append((1 / r["home"]) / s)
        pD.append((1 / r["draw"]) / s)
        pA.append((1 / r["away"]) / s)
    cons = {
        "home": median(pH), "draw": median(pD), "away": median(pA),
        "n_bookmakers": len(rows),
    }
    # 重新归一化到 100%
    tot = cons["home"] + cons["draw"] + cons["away"]
    cons["home"] /= tot
    cons["draw"] /= tot
    cons["away"] /= tot
    return cons


def _fit_poisson(cons: dict) -> tuple[float, float]:
    """网格搜索 λ_home, λ_away,使 Poisson 1X2 概率逼近市场共识.

    注意:必须用向量化批量计算,否则在 430×430 网格下会触发分钟级的超时
    (原实现每次内层循环都重复调用 poisson.pmf,耗时约 60s,超过前端 60s 超时,
    导致前端先报"赔率预测失败"而后端才返回 200)。
    """
    target_home, target_draw, target_away = cons["home"], cons["draw"], cons["away"]
    grid = np.linspace(0.2, 4.5, 430)
    goals = np.arange(15)

    # ph[k, a] = P(主队进 a 球 | λ=grid[k]); pa 同理(对称,同一网格)
    PH = poisson.pmf(goals[None, :], grid[:, None])   # (N, 15)
    PA = PH

    # 主胜: Σ_a ph[a] * Σ_{b<a} pa[b] ；即 ph 与"pa 前缀和(左移)"的逐元素乘加
    cum_pa = np.cumsum(PA, axis=1)                       # (N, 15)
    cum_pa_shift = np.concatenate(
        [np.zeros((grid.size, 1)), cum_pa[:, :-1]], axis=1
    )                                                     # (N, 15), 其中 [j,a]=Σ_{b<a} pa[j,b]
    home = np.einsum("ka,ja->kj", PH, cum_pa_shift)      # (N, N)
    draw = np.einsum("ka,ja->kj", PH, PA)               # (N, N)
    away = 1 - home - draw

    err = (
        (home - target_home) ** 2
        + (draw - target_draw) ** 2
        + (away - target_away) ** 2
    )
    ik, ij = divmod(int(np.argmin(err)), grid.size)
    return float(grid[ik]), float(grid[ij])


def _top3_scores(lh: float, la: float, max_goals: int = 8) -> list[dict]:
    scores = []
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = poisson.pmf(h, lh) * poisson.pmf(a, la)
            scores.append({"score": f"{h}-{a}", "prob": round(float(p), 4)})
    scores.sort(key=lambda x: x["prob"], reverse=True)
    return scores[:3]


def _collect_markets(fixture: dict) -> dict:
    """聚合亚洲让球与大小球市场的共识(取中位数)."""
    ah, ou = [], []
    for bm in fixture.get("bookmakers", []):
        for bet in bm.get("bets", []):
            name = bet.get("name")
            if name == "Asian Handicap":
                vals = {v["value"]: float(v["odd"]) for v in bet.get("values", [])}
                for k, odd in vals.items():
                    ah.append({"line": k, "odd": odd})
            elif name == "Goals Over/Under":
                vals = {v["value"]: float(v["odd"]) for v in bet.get("values", [])}
                for k, odd in vals.items():
                    ou.append({"line": k, "odd": odd})
    # 按让球线/大小球线分组,取中位数赔率
    def _median_by_key(items: list[dict], key: str):
        out = {}
        for it in items:
            out.setdefault(it[key], []).append(it["odd"])
        return {k: round(median(v), 3) for k, v in out.items()}

    return {"asian_handicap": _median_by_key(ah, "line"),
            "over_under": _median_by_key(ou, "line")}


def analyze(fixture: dict) -> dict:
    """对一份赔率 fixture 做完整分析,返回结构化结果(dict)。"""
    league = fixture.get("league", {})
    fx = fixture.get("fixture", {})

    rows = _collect_1x2(fixture)
    if not rows:
        return {"error": "未找到 Match Winner 赔率"}
    cons = _implied(rows)
    lh, la = _fit_poisson(cons)
    top3 = _top3_scores(lh, la)
    markets = _collect_markets(fixture)

    # 胜负倾向
    fav_name, fav_p = max(
        (("主胜", cons["home"]), ("平局", cons["draw"]), ("客胜", cons["away"])),
        key=lambda x: x[1],
    )

    # 大小球(2.5)倾向
    ou25 = markets["over_under"]
    over25 = ou25.get("Over 2.5")
    under25 = ou25.get("Under 2.5")
    ou_verdict = None
    if over25 and under25:
        p_over = (1 / over25) / (1 / over25 + 1 / under25)
        ou_verdict = {"type": "大球" if p_over > 0.5 else "小球", "prob": round(p_over, 3)}

    # 让球(主让1.5)倾向
    ah_home_15 = markets["asian_handicap"].get("Home -1.5")
    ah_verdict = None
    if ah_home_15:
        ah_verdict = {
            "line": "Home -1.5",
            "odd": ah_home_15,
            "implied_win": round(1 / ah_home_15, 3),
        }

    return {
        "league": league.get("name"),
        "country": league.get("country"),
        "season": league.get("season"),
        "match_date": fx.get("date"),
        "odds_updated": fixture.get("update"),
        "n_bookmakers": cons["n_bookmakers"],
        "consensus": {
            "home": round(cons["home"], 4),
            "draw": round(cons["draw"], 4),
            "away": round(cons["away"], 4),
        },
        "poisson": {
            "lambda_home": round(lh, 3),
            "lambda_away": round(la, 3),
            "total_goals": round(lh + la, 3),
        },
        "top3": [{"score": t["score"], "prob": t["prob"]} for t in top3],
        "asian_handicap": [{"line": k, "odd": v} for k, v in sorted(markets["asian_handicap"].items())],
        "over_under": [{"line": k, "odd": v} for k, v in sorted(markets["over_under"].items())],
        "verdict": {
            "favorite": fav_name,
            "favorite_pct": round(fav_p, 3),
            "over_under_25": ou_verdict,
            "asian_handicap_15": ah_verdict,
        },
    }


def predict_from_odds(fixture_id: int) -> dict | None:
    """拉取实时赔率并分析,返回结构化结果;无数据返回 None。"""
    fixture = fetch_odds_via_api(fixture_id)
    if fixture is None:
        return None
    return analyze(fixture)


# ── 命令行输出 ──────────────────────────────────────────────────────────────
def _print_result(r: dict):
    if "error" in r:
        print(r["error"])
        return
    print("=" * 60)
    print(f"联赛: {r.get('league')} ({r.get('country')}) 赛季 {r.get('season')}")
    print(f"比赛时间: {r.get('match_date')}")
    print(f"赔率更新: {r.get('odds_updated')}")
    print(f"样本: {r.get('n_bookmakers')} 家博彩公司")
    print("=" * 60)

    c = r["consensus"]
    print("\n【市场共识概率】(去除抽水后)")
    print(f"  主胜: {c['home']:.1%}")
    print(f"  平局: {c['draw']:.1%}")
    print(f"  客胜: {c['away']:.1%}")

    p = r["poisson"]
    print("\n【Poisson 反解预期进球】")
    print(f"  主队 λ_home = {p['lambda_home']:.2f}")
    print(f"  客队 λ_away = {p['lambda_away']:.2f}")
    print(f"  预期总进球 = {p['total_goals']:.2f}")

    print("\n【最可能比分 Top3】")
    for t in r["top3"]:
        print(f"  {t['score']:<6} {t['prob']:.1%}")

    print("\n【亚洲让球共识赔率】")
    for item in r["asian_handicap"]:
        print(f"  {item['line']:<12} {item['odd']}")
    print("\n【大小球共识赔率】")
    for item in r["over_under"]:
        print(f"  {item['line']:<12} {item['odd']}")

    v = r["verdict"]
    print("\n" + "=" * 60)
    print("【综合研判】")
    print(f"  胜负倾向: {v['favorite']} (共识 {v['favorite_pct']:.1%})")
    if v["over_under_25"]:
        print(f"  大小球(2.5): {v['over_under_25']['type']} (大球概率 {v['over_under_25']['prob']:.1%})")
    if v["asian_handicap_15"]:
        ah = v["asian_handicap_15"]
        print(f"  让球(主让1.5): 赔率 {ah['odd']} -> 隐含主队赢2球以上概率 {ah['implied_win']:.1%}")
    print("=" * 60)


def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.isdigit():
            fixture_id = int(arg)
            data = fetch_odds_via_api(fixture_id)
            if data is None:
                print("未获取到实时赔率数据（可能该比赛暂无赔率或接口受限）")
                return
        else:
            data = _load(arg)
    else:
        data = _load(ODDS_PATH)

    if not data:
        print("未找到赔率数据")
        return

    fixture = data[0]
    result = analyze(fixture)
    _print_result(result)


if __name__ == "__main__":
    main()
