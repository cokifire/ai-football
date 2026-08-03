"""
用真实浏览器(Playwright)渲染 Flashscore 单场 stats 页, 抓取
    Expected goals (xG)   和   Goals prevented
两行数据, 并落库/打印。

URL 规律 (来自示例 https://www.flashscore.com/match/football/aik-lzqk4S68/orgryte-CGT7Kq4j/summary/stats/overall/):
    https://www.flashscore.com/match/football/<home_slug>-<home_hash>/<away_slug>-<away_hash>/summary/stats/overall/
其中 slug 是队名小写、去非字母数字 (空格/连字符等去掉), hash 取自 flashscore_team_map.json。

本脚本基于 flashscore_team_map.json 里已有的 hash:
  - 用 hash -> api_football_id 反查 teams 表得到队名, 再生成 slug;
  - 因此要抓的比赛双方 hash 必须都在 flashscore_team_map.json 中。

用法
----
    # 抓取示例比赛 (aik vs orgryte)
    python tools/fetch_flashscore_match_xg.py --pair lzqk4S68 CGT7Kq4j

    # 一次抓多场, 用 --pairs 传 "hash1,hash2" 列表 (空格分隔每组)
    python tools/fetch_flashscore_match_xg.py --pairs lzqk4S68 CGT7Kq4j 4Kh5hPE1 rBi9iqU7

    # 结果写 JSON 文件 (默认也打印)
    python tools/fetch_flashscore_match_xg.py --pair lzqk4S68 CGT7Kq4j --out xg_result.json
"""
import sys
import json
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re
from sqlalchemy import text
from playwright.sync_api import sync_playwright

from app.db.session import SessionLocal

MAP_FILE = Path(__file__).resolve().parent / "flashscore_team_map.json"

# 比赛 URL 模板
URL_TMPL = (
    "https://www.flashscore.com/match/football/"
    "{home_slug}-{home_hash}/{away_slug}-{away_hash}/summary/stats/overall/"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def slugify(name: str) -> str:
    """生成 Flashscore 风格的 slug: 小写 + 去变音符 + 去非字母数字。"""
    s = name.lower()
    s = "".join(c for c in s if c.isalnum())  # 最简单稳妥: 只保留字母数字
    return s


def load_hash_to_name(db):
    """flashscore hash -> 队名 (来自 teams 表)。"""
    rows = db.execute(text("SELECT id, name FROM teams")).fetchall()
    id_to_name = {r[0]: r[1] for r in rows}
    override = json.loads(MAP_FILE.read_text(encoding="utf-8")) if MAP_FILE.exists() else {}
    hash_to_name = {}
    missing = []
    for h, tid in override.items():
        name = id_to_name.get(tid)
        if name:
            hash_to_name[h] = name
        else:
            missing.append((h, tid))
    if missing:
        print(f"[warn] {len(missing)} 个 hash 在 teams 表找不到对应队名: {missing[:5]}...")
    return hash_to_name


def build_url(home_hash, away_hash, hash_to_name):
    home_name = hash_to_name.get(home_hash)
    away_name = hash_to_name.get(away_hash)
    if not home_name or not away_name:
        raise ValueError(
            f"hash 未解析到队名: home={home_hash}({home_name}) away={away_hash}({away_name})"
        )
    return URL_TMPL.format(
        home_slug=slugify(home_name),
        home_hash=home_hash,
        away_slug=slugify(away_name),
        away_hash=away_hash,
    )


def extract_stats(page):
    """从已渲染的 stats 页抽取 xG 与 Goals prevented。

    Flashscore stats 页 body 纯文本布局: 每个统计项占 3 行
        <主队值>
        <统计项名称>          例如 'Expected goals (xG)' / 'Goals prevented'
        <客队值>
    因此解析时: 找到标签行, 其上=home, 其下=away。
    """
    page.wait_for_function(
        "() => document.body.innerText.includes('Expected goals') "
        "&& document.body.innerText.includes('Goals prevented')",
        timeout=30000,
    )
    lines = page.evaluate(
        "() => document.body.innerText.split('\\n').map(s=>s.trim()).filter(Boolean)"
    )

    def num(s):
        if s is None:
            return None
        m = re.findall(r"-?\d+\.?\d*", s)
        return m[0] if m else None

    def find_pair(keyword):
        for i, ln in enumerate(lines):
            if keyword.lower() in ln.lower():
                home = lines[i - 1] if i - 1 >= 0 else None
                away = lines[i + 1] if i + 1 < len(lines) else None
                return num(home), num(away)
        return None, None

    xg_home, xg_away = find_pair("Expected goals")
    gp_home, gp_away = find_pair("Goals prevented")
    return {
        "home": {"xg": xg_home, "goals_prevented": gp_home},
        "away": {"xg": xg_away, "goals_prevented": gp_away},
        "lines": lines,
    }


def scrape_match_xg(home_hash, away_hash, hash_to_name):
    """抓取单场比赛的 xG 与 Goals prevented, 返回结构:
        {
          "home": {"xg": str|None, "goals_prevented": str|None},
          "away": {"xg": str|None, "goals_prevented": str|None},
          "home_hash": ..., "away_hash": ...,
          "home_name": ..., "away_name": ..., "url": ...
        }
    hash_to_name: dict[flashscore_hash] -> 队名 (用于生成 slug)。
    """
    url = build_url(home_hash, away_hash, hash_to_name)
    print(f"[fetch] {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, locale="en-US")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # 处理同意弹窗
            for sel in ["text=AGREE", "text=I ACCEPT", "text=Accept", "text=OK"]:
                try:
                    page.click(sel, timeout=3000)
                    break
                except Exception:
                    pass
            # 等待 stats 异步加载完成 (Flashscore 统计表为 JS 异步注入)
            page.wait_for_timeout(9000)
            stats = extract_stats(page)
        finally:
            browser.close()
    stats["home_hash"] = home_hash
    stats["away_hash"] = away_hash
    stats["home_name"] = hash_to_name.get(home_hash)
    stats["away_name"] = hash_to_name.get(away_hash)
    stats["url"] = url
    return stats


# 向后兼容别名
fetch_one = scrape_match_xg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", nargs=2, metavar=("HOME_HASH", "AWAY_HASH"),
                    help="单场比赛: 主队hash 客队hash")
    ap.add_argument("--pairs", nargs="+", metavar="HASH",
                    help="多场: 依次 home1 away1 home2 away2 ... (成对)")
    ap.add_argument("--out", default=None, help="结果输出 JSON 路径")
    args = ap.parse_args()

    if args.pair:
        pairs = [tuple(args.pair)]
    elif args.pairs:
        if len(args.pairs) % 2 != 0:
            print("错误: --pairs 必须是成对的 hash")
            sys.exit(1)
        pairs = [(args.pairs[i], args.pairs[i + 1]) for i in range(0, len(args.pairs), 2)]
    else:
        # 默认示例
        pairs = [("lzqk4S68", "CGT7Kq4j")]
        print("[info] 未指定 --pair/--pairs, 使用默认示例 (aik vs orgryte)")

    db = SessionLocal()
    try:
        hash_to_name = load_hash_to_name(db)
    finally:
        db.close()

    results = []
    for home, away in pairs:
        try:
            r = fetch_one(home, away, hash_to_name)
            results.append(r)
            print(f"  {r['home_name']} (xG={r['home']['xg']}, GP={r['home']['goals_prevented']})"
                  f"  vs  {r['away_name']} (xG={r['away']['xg']}, GP={r['away']['goals_prevented']})")
        except Exception as e:
            print(f"[error] pair {home}/{away}: {e}")
        time.sleep(2)  # 礼貌间隔

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[saved] {args.out}")


if __name__ == "__main__":
    main()
