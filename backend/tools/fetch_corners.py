"""拉取指定 fixture 的最新赔率,提取角球相关市场并做共识分析.

用法:
  python tools/fetch_corners.py fixture_id
"""
import sys, json, re, os
from pathlib import Path
from statistics import median
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from app.core.config import settings


def _api_get(path, params, timeout=15.0):
    r = httpx.get(
        f"{settings.api_football_base_url}/{path}",
        headers={"x-apisports-key": settings.api_football_key},
        params=params, timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def fetch_odds(fid):
    data = _api_get("odds", {"fixture": fid}).get("response", [])
    return data


def main():
    fid = int(sys.argv[1]) if len(sys.argv) > 6 else ValueError("请输入 fixture_id")
    print(f"拉取 fixture {fid} 赔率 ...")
    try:
        data = fetch_odds(fid)
    except Exception as e:
        print("赔率拉取失败:", e)
        return
    if not data:
        print("API 未返回该 fixture 的赔率(可能免费版限制或比赛不可见)")
        return

    fx = data[0]
    lg = fx.get("league", {})
    fix = fx.get("fixture", {})
    print("=" * 60)
    print(f"联赛: {lg.get('name')} ({lg.get('country')}) 赛季 {lg.get('season')}")
    print(f"比赛时间: {fix.get('date')}")
    print(f"赔率更新: {fx.get('update')}")
    print("=" * 60)

    # 收集所有含 Corner 的 bet
    corner_bets = {}  # bet_name -> {bookmaker: {value: odd}}
    for bm in fx.get("bookmakers", []):
        bname = bm.get("name")
        for bet in bm.get("bets", []):
            bname_bet = bet.get("name", "")
            if "corner" in bname_bet.lower() or "角" in bname_bet:
                d = corner_bets.setdefault(bname_bet, {})
                for v in bet.get("values", []):
                    d.setdefault(v["value"], {})[bname] = float(v["odd"])

    if not corner_bets:
        print("\n未在该场赔率中找到角球相关市场。可用的非角球 bet 类型:")
        seen = set()
        for bm in fx.get("bookmakers", []):
            for bet in bm.get("bets", []):
                if bet["name"] not in seen:
                    seen.add(bet["name"])
                    print("  -", bet["name"])
        return

    print(f"\n找到角球市场: {list(corner_bets.keys())}")
    print(f"共 {len(fx.get('bookmakers', []))} 家博彩公司")

    for bet_name, vals in corner_bets.items():
        print("\n" + "-" * 60)
        print(f"市场: {bet_name}")
        # 每个 value(如 Over 9.5) 取各庄家赔率中位数
        lines = {}
        for value, bm_odds in vals.items():
            lines[value] = median(list(bm_odds.values()))
        # 排序输出
        def sort_key(v):
            # 提取数字用于排序
            import re
            m = re.findall(r"[\d.]+", v)
            return float(m[0]) if m else 0
        for value in sorted(lines, key=sort_key):
            odds = lines[value]
            n = len(vals[value])
            print(f"  {value:<14} 中位赔率 {odds:<6} (n={n})")

    # 重点:Match Corners (总角球 Over/Under) -> 隐含总角球数
    mc = corner_bets.get("Match Corners")
    if mc:
        print("\n" + "=" * 60)
        print("【总角球隐含概率分析】")
        # 对每个 Over/Under 线, 计算该方向隐含概率(取中位数赔率)
        over_und = {}
        for value, bm_odds in mc.items():
            med = median(list(bm_odds.values()))
            imp = 1 / med
            over_und[value] = imp
        # 计算大小球分界: 找 Over X.5 与 Under X.5 配对
        import re
        # 简单估算总角球期望: 用相邻 Over/Under 隐含概率插值
        # 收集 Over 线
        overs = {float(re.findall(r"[\d.]+", k)[0]): v for k, v in over_und.items() if k.lower().startswith("over")}
        unders = {float(re.findall(r"[\d.]+", k)[0]): v for k, v in over_und.items() if k.lower().startswith("under")}
        # 隐含概率需归一(含抽水)
        # 估算总角球中位数: 找使 P(Over x.5)≈0.5 的 x
        total_est = None
        for line in sorted(overs):
            p = overs[line]
            # 粗略: 抽水约 6-8%, 这里直接用隐含概率
            if p >= 0.5:
                # Over line 的概率仍>0.5 说明总角球很可能大于该线
                total_est = line + 0.5
                break
        if total_est is None and overs:
            total_est = max(overs) + 1.0
        print("  Over/Under 隐含概率(未去抽水):")
        for line in sorted(set(list(overs)+list(unders))):
            o = overs.get(line); u = unders.get(line)
            s = ""
            if o is not None: s += f"Over{line}:{o:.0%}  "
            if u is not None: s += f"Under{line}:{u:.0%}"
            print(f"    {s}")
        if total_est:
            print(f"  => 市场隐含总角球约 {total_est:.1f} 个(粗略, 未去抽水)")

    # 队伍角球 Team Corners
    tc = corner_bets.get("Team Corners")
    if tc:
        print("\n【队伍角球分析】")
        home_over = {float(re.findall(r"[\d.]+", k)[0]): v for k, v in tc.items() if "home" in k.lower() and "over" in k.lower()}
        away_over = {float(re.findall(r"[\d.]+", k)[0]): v for k, v in tc.items() if "away" in k.lower() and "over" in k.lower()}
        if home_over:
            print("  主队角球Over隐含:")
            for line in sorted(home_over):
                print(f"    Over {line}: {home_over[line]:.0%}")
        if away_over:
            print("  客队角球Over隐含:")
            for line in sorted(away_over):
                print(f"    Over {line}: {away_over[line]:.0%}")


if __name__ == "__main__":
    main()
