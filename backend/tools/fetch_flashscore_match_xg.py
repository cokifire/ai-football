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
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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


def _format_flashscore_date(match_date):
    """将日期格式化为 Flashscore H2H 列表显示形式 'dd.mm.yy'。"""
    if isinstance(match_date, datetime):
        match_date = match_date.date()
    if isinstance(match_date, date):
        return match_date.strftime("%d.%m.%y")
    return str(match_date)


def _is_future_match(page):
    """当前页是否为未开赛页面 (无 stats)。"""
    text = page.evaluate("() => document.body.innerText") or ""
    return "Statistics will be available once the match starts" in text


def _parse_date(text):
    """从文本中解析日期, 返回 (year, month, day) 或 None。

    兼容 Flashscore 的 'dd/mm/yyyy' 与 'dd.mm.yy' 两种写法。
    """
    if not text:
        return None
    m = re.search(r"(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})", text)
    if not m:
        return None
    a, b, c = (int(x) for x in m.groups())
    if c < 100:
        c += 2000
    # 兼容 日/月/年 与 月/日/年: 取 1..12 为月
    # Flashscore 全部使用欧洲 dd/mm 日期格式；当 a、b 都 ≤12 时两种解读
    # 都合法，这里优先 dd/mm（day-first），否则退回到唯一合法解读。
    a_is_month = 1 <= a <= 12 and 1 <= b <= 31
    b_is_month = 1 <= b <= 12 and 1 <= a <= 31
    if a_is_month and b_is_month:
        # 两者都合法 → Flashscore 惯例为 dd/mm，所以 b 是月 a 是日
        month, day = b, a
    elif a_is_month:
        month, day = a, b
    elif b_is_month:
        month, day = b, a
    else:
        return None
    return (c, month, day)


def _get_page_match_date(page):
    """从 stats 页解析当前比赛日期, 返回 (year, month, day) 或 None。

    页面标题形如 'SIR 4-1 GOT | Sirius v Goteborg 26/07/2026, Stats',
    或从 body 中匹配 '26/07/2026' 这类日期。
    """
    text = page.evaluate("() => document.body.innerText") or ""
    return _parse_date(text)


def _as_date_tuple(target_date):
    """把目标日期统一为 (year, month, day)。支持 date/datetime/字符串。"""
    if isinstance(target_date, datetime):
        return (target_date.year, target_date.month, target_date.day)
    if isinstance(target_date, date):
        return (target_date.year, target_date.month, target_date.day)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(target_date))
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _dates_match(page_date, target_date, tol_days=1):
    """比较页面日期与目标日期是否一致 (允许 ±tol_days 天容差)。

    容差用于解决时区差异: 本地库 f.date 为北京时间 (UTC+8),
    而 Flashscore 页面显示的比赛日期为比赛当地时区, 两者可能相差 1 天
    (例如北京 07-25 凌晨的比赛, 当地为 07-24 下午)。
    """
    if page_date is None:
        return False
    target = _as_date_tuple(target_date)
    if target is None:
        return False
    try:
        d1 = date(*page_date)
        d2 = date(*target)
    except Exception:
        return False
    return abs((d1 - d2).days) <= tol_days


def _accept_consent(page):
    """点击 Flashscore 的同意/接受弹窗 (如果存在)。"""
    for sel in ["text=AGREE", "text=I ACCEPT", "text=Accept", "text=OK"]:
        try:
            page.click(sel, timeout=3000)
            break
        except Exception:
            pass


def _derive_h2h_url(stats_url: str) -> str:
    """把 stats URL 换成同场比赛的 h2h URL。"""
    return stats_url.replace("/summary/stats/overall/", "/h2h/overall/")


def _derive_stats_url_from_match_url(match_url: str) -> str:
    """把 H2H 里点击得到的比赛详情 URL (?mid=xxx) 换成 stats URL。"""
    parts = urlsplit(match_url)
    # match_url 形如 https://.../match/football/<slug>/<slug>/?mid=xxx
    path = parts.path.rstrip("/") + "/summary/stats/overall/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _resolve_via_h2h(page, stats_url, match_date):
    """通过 H2H 列表点击对应日期的历史比赛, 返回正确的 stats URL。

    当 team-hash pair 被 Flashscore 解析到错误场次时, 在 H2H 栏里按比赛日期
    定位。由于 Flashscore 列表显示的是比赛当地日期, 而目标 match_date 为北京时间,
    这里以 ±1 天容差匹配 (见 _dates_match)。
    """
    h2h_url = _derive_h2h_url(stats_url)
    print(f"[h2h] 尝试按日期定位 H2H: {h2h_url}")
    page.goto(h2h_url, wait_until="domcontentloaded", timeout=60000)
    _accept_consent(page)
    page.wait_for_timeout(6000)

    section = page.locator("div.h2h__section", has_text="Head-to-head matches")
    rows = section.locator("a.h2h__row")
    best_row = None
    best_diff = None
    for i in range(rows.count()):
        row = rows.nth(i)
        row_text = row.inner_text() or ""
        row_date = _parse_date(row_text)
        if row_date is None:
            continue
        if _dates_match(row_date, match_date, tol_days=1):
            match_url = row.get_attribute("href")
            if not match_url:
                continue
            diff = abs((date(*row_date) - date(*_as_date_tuple(match_date))).days)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_row = match_url
    if not best_row:
        raise ValueError(
            f"在 Head-to-head matches 中未找到日期接近 {match_date} 的比赛"
        )
    resolved_stats_url = _derive_stats_url_from_match_url(best_row)
    print(f"[h2h] 定位到历史比赛: {best_row}")
    return resolved_stats_url


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


def scrape_match_xg(home_hash, away_hash, hash_to_name, match_date):
    """抓取单场比赛的 xG 与 Goals prevented, 返回结构:
        {
          "home": {"xg": str|None, "goals_prevented": str|None},
          "away": {"xg": str|None, "goals_prevented": str|None},
          "home_hash": ..., "away_hash": ...,
          "home_name": ..., "away_name": ..., "url": ...
        }
    hash_to_name: dict[flashscore_hash] -> 队名 (用于生成 slug)。
    match_date: 必选, 抓取前会核对比赛日期; 当 Flashscore 把 team-hash pair
                解析到未来那场时, 通过 H2H 列表点击对应日期的历史比赛。
    """
    if match_date is None:
        raise ValueError("scrape_match_xg 必须提供 match_date 以核对比赛日期")
    url = build_url(home_hash, away_hash, hash_to_name)
    print(f"[fetch] {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, locale="en-US")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _accept_consent(page)

            # 等待 stats 异步加载完成 (Flashscore 统计表为 JS 异步注入)
            page.wait_for_timeout(9000)

            # 精确核对比赛日期: 若页面日期与目标不一致 (含未开赛/未来场次),
            # 通过 H2H 列表按日期找正确的历史比赛
            page_date = _get_page_match_date(page)
            if not _dates_match(page_date, match_date):
                print(f"[fetch] 日期不一致 page={page_date} target={match_date}, 尝试 H2H 回退")
                resolved_url = _resolve_via_h2h(page, page.url, match_date)
                print(f"[fetch] 重新加载历史场次: {resolved_url}")
                page.goto(resolved_url, wait_until="domcontentloaded", timeout=60000)
                _accept_consent(page)
                page.wait_for_timeout(9000)
                page_date = _get_page_match_date(page)
                if not _dates_match(page_date, match_date):
                    raise ValueError(
                        f"抓取的比赛日期 {page_date} 与目标日期 {match_date} 仍不一致"
                    )

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
