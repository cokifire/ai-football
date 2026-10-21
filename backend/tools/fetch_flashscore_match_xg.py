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
    # 抓取示例比赛 (aik vs orgryte)。--date 必填, 抓取时会拿它核对页面上的场次日期
    python tools/fetch_flashscore_match_xg.py --pair lzqk4S68 CGT7Kq4j --date 2026-07-26

    # 一次抓多场, --pairs 按主客成对; --date 按同样顺序重复, 一场一个
    python tools/fetch_flashscore_match_xg.py \
        --pairs lzqk4S68 CGT7Kq4j 4Kh5hPE1 rBi9iqU7 \
        --date 2026-07-26 --date 2026-08-02

    # 结果写 JSON 文件 (默认也打印)
    python tools/fetch_flashscore_match_xg.py --pair lzqk4S68 CGT7Kq4j \
        --date 2026-07-26 --out xg_result.json
"""
import sys
import json
import time
import argparse
import unicodedata
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 同 backfill_xg.py：app/core/config.py 的 env_file = ".env" 按 CWD
# 解析，从仓库根直接运行会退回 localhost:3306 默认值。按脚本位置显式加载，
# 保证从任意目录运行都拿到同一套 DB 配置。
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

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

# ── 浏览器参数：与赔率抓取 tools/fetch_flashscore_odds.py 共用同一套反检测配置 ──
# Flashscore 会把带自动化特征的浏览器甩到站点默认页（如 /snooker/）；两边必须
# 用同一份配置，否则没对齐的那侧会在批量运行时被系统性拦截。
# 本文件原先没有这套配置，导致补跑 125 场时有约一半请求被重定向，页面读不到
# 日期而误报"日期不一致"。
from tools.fetch_flashscore_odds import UA, CHROME_ARGS, STEALTH_JS, _VIEWPORT

# 判定"还停在比赛页"的标记。stats 页路径恒为 /match/football/<...>/summary/stats/
# ，比赔率侧的 /match/ 前缀更严格，避免停在 /match/snooker/ 这类别的运动上。
STATS_URL_MARK = "/match/football/"

# 被甩到站点默认页时，换新标签页重试的次数 / 两次重试之间的退避秒数。
# 实测重定向率约 1/2，重试 4 次后残留失败概率约 1/16。
STATS_RETRY = 4
RETRY_BACKOFF_SEC = 3


def slugify(name: str) -> str:
    """生成 Flashscore URL 可接受的 ASCII slug。

    Flashscore 的路由不接受 ``Bayern München`` 这类 Unicode slug，且会把
    ``Bodo/Glimt`` 规范为 ``bodo-glimt``。保留词间分隔符也让 Flashscore
    有机会将别名重定向到它的 canonical URL。
    """
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


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


def _parse_cli_date(value: str):
    """解析 --date 参数，返回 datetime。

    接受 ``YYYY-MM-DD`` 与 ``'YYYY-MM-DD HH:MM[:SS]'``。只给日期就够了：
    scrape_match_xg 拿它跟页面显示的日期比对时允许 ±1 天容差，正好覆盖
    页面按当地时区显示、与库里的北京时间可能差一天的情况。
    """
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"无法解析日期 {value!r}，请用 YYYY-MM-DD 或 'YYYY-MM-DD HH:MM'"
    )


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


def _fresh_page(browser):
    """新建一个带反检测配置的干净标签页。

    每次重试都用新的 context（而不是复用旧的），因为一旦请求 series 被判定为
    爬虫，旧 context 里的 cookie/指纹会持续被拒。
    """
    ctx = browser.new_context(
        user_agent=UA, locale="en-US", viewport=_VIEWPORT,
        has_touch=False, is_mobile=False,
    )
    ctx.add_init_script(STEALTH_JS)
    return ctx.new_page()


def _goto_stats(url: str, browser, settle_ms: int = 9000):
    """打开 stats 页并停在比赛页上；被 Flashscore 甩走就换新标签页重试。

    Flashscore 识别出爬虫时不返回错误页，而是把浏览器重定向到站点默认页
    （https://www.flashscore.com/snooker/）。此时页面里没有日期、没有数据，
    不重试就会一路走到"日期不一致"的误判里。
    """
    page = _fresh_page(browser)
    for attempt in range(1, STATS_RETRY + 1):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _accept_consent(page)
        page.wait_for_timeout(settle_ms)
        if STATS_URL_MARK in page.url:
            return page
        print(f"[warn] Flashscore 重定向到非比赛页 {page.url}，"
              f"第 {attempt}/{STATS_RETRY} 次重试")
        if attempt < STATS_RETRY:
            page = _fresh_page(browser)
            time.sleep(RETRY_BACKOFF_SEC)
    raise RuntimeError(
        f"Flashscore 连续 {STATS_RETRY} 次把 {url} 重定向到非比赛页"
    )


def _derive_stats_url_from_match_url(match_url: str) -> str:
    """把 H2H 里点击得到的比赛详情 URL (?mid=xxx) 换成 stats URL。"""
    parts = urlsplit(match_url)
    # match_url 形如 https://.../match/football/<slug>/<slug>/?mid=xxx
    path = parts.path.rstrip("/") + "/summary/stats/overall/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _normalize_team_label(name):
    """去掉常见后缀, 用于和 Flashscore H2H 行里的短队名做包含匹配。"""
    if not name:
        return ""
    for suf in (" FC", " FF", " U21", " II"):
        name = name.replace(suf, "")
    return name.strip().lower()


def _resolve_via_h2h(page, stats_url, match_date, home_hash=None, away_hash=None, hash_to_name=None):
    """通过 H2H 列表点击对应日期的历史比赛, 返回正确的 stats URL。

    当 team-hash pair 被 Flashscore 解析到错误场次时, 在 H2H 栏里按比赛日期
    定位。由于 Flashscore 列表显示的是比赛当地日期, 而目标 match_date 为北京时间,
    这里以 ±1 天容差匹配 (见 _dates_match)。同时要求行内包含双方队名, 避免误命中。
    """
    h2h_url = _derive_h2h_url(stats_url)
    print(f"[h2h] 尝试按日期定位 H2H: {h2h_url}")
    page.goto(h2h_url, wait_until="domcontentloaded", timeout=60000)
    _accept_consent(page)
    # 等待 H2H 行渲染 (懒加载, 给足时间)
    try:
        page.wait_for_selector("a.h2h__row", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(3000)

    home_label = _normalize_team_label(hash_to_name.get(home_hash)) if hash_to_name else ""
    away_label = _normalize_team_label(hash_to_name.get(away_hash)) if hash_to_name else ""

    # 优先在 Head-to-head 区里找, 退化到全页 a.h2h__row
    candidates = [
        page.locator("div.h2h__section", has_text="Head-to-head matches").locator("a.h2h__row"),
        page.locator("a.h2h__row"),
    ]
    best_row = None
    best_diff = None
    for rows in candidates:
        for i in range(rows.count()):
            row = rows.nth(i)
            row_text = (row.inner_text() or "").lower()
            row_date = _parse_date(row_text)
            if row_date is None:
                continue
            if not _dates_match(row_date, match_date, tol_days=1):
                continue
            match_url = row.get_attribute("href")
            if not match_url:
                continue
            # H2H 行的 href 包含双方 Flashscore hash。优先以它验证球队，避免
            # 本地名称与页面英文名称不同（如 Bayern München / Bayern Munich）
            # 而误过滤掉正确比赛。没有 hash 时才退回到文本名称校验。
            match_url_lower = match_url.lower()
            if home_hash and away_hash:
                if home_hash.lower() not in match_url_lower or away_hash.lower() not in match_url_lower:
                    continue
            elif home_label and away_label:
                if home_label not in row_text or away_label not in row_text:
                    continue
            diff = abs((date(*row_date) - date(*_as_date_tuple(match_date))).days)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_row = match_url
        if best_row:
            break
    if not best_row:
        raise ValueError(
            f"在 Head-to-head matches 中未找到日期接近 {match_date} 的比赛"
        )
    resolved_stats_url = _derive_stats_url_from_match_url(best_row)
    print(f"[h2h] 定位到历史比赛: {best_row}")
    return resolved_stats_url


def extract_stats(page):
    """从已渲染的 stats 页抽取 xG / Goals prevented / Passes in final third。

    Flashscore stats 页 body 纯文本布局: 每个统计项占 3 行
        <主队值>
        <统计项名称>          例如 'Expected goals (xG)' / 'Goals prevented'
                             / 'Passes in final third'
        <客队值>
    因此解析时: 找到标签行, 其上=home, 其下=away。

    注意: 并非每场都展示 'Goals prevented' 与 'Passes in final third'
    (低级别联赛/老比赛常缺这些行)。因此只等待 'Expected goals' 出现即可;
    缺失项返回 None, 由上层 upsert 跳过 (不写入)。
    """
    page.wait_for_function(
        "() => document.body.innerText.includes('Expected goals')",
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
    pft_home, pft_away = find_pair("Passes in final third")
    return {
        "home": {"xg": xg_home, "goals_prevented": gp_home, "passes_final_third": pft_home},
        "away": {"xg": xg_away, "goals_prevented": gp_away, "passes_final_third": pft_away},
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
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            # 打开 stats 页并确保真的停在比赛页（被甩走会换新标签页重试）
            page = _goto_stats(url, browser)

            # 精确核对比赛日期: 若页面日期与目标不一致 (含未开赛/未来场次),
            # 通过 H2H 列表按日期找正确的历史比赛
            page_date = _get_page_match_date(page)
            if not _dates_match(page_date, match_date):
                print(f"[fetch] 日期不一致 page={page_date} target={match_date}, 尝试 H2H 回退")
                resolved_url = _resolve_via_h2h(
                    page, page.url, match_date,
                    home_hash=home_hash, away_hash=away_hash, hash_to_name=hash_to_name,
                )
                print(f"[fetch] 重新加载历史场次: {resolved_url}")
                # 这条 URL 也得重试：H2H 跳转同样可能被甩到默认页
                page = _goto_stats(resolved_url, browser)
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
    ap.add_argument("--date", action="append", type=_parse_cli_date, default=None,
                    metavar="YYYY-MM-DD[ HH:MM]",
                    help="比赛日期, 用于核对页面场次; --pairs 时按场次顺序重复, 一场一个")
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

    # scrape_match_xg 必须拿到 match_date 才能核对场次（为 None 直接抛错），
    # 所以这里要求日期数量与场次数严格一致：共用一个日期会静默抓到错场次。
    dates = args.date or []
    if len(dates) != len(pairs):
        print(f"错误: 需要为 {len(pairs)} 场比赛各提供一个 --date，实际给了 {len(dates)} 个")
        print("      --pair 用 1 个；--pairs 按场次顺序重复 --date，例如")
        print("      --pairs h1 a1 h2 a2 --date 2026-07-26 --date 2026-08-02")
        sys.exit(1)

    db = SessionLocal()
    try:
        hash_to_name = load_hash_to_name(db)
    finally:
        db.close()

    results = []
    for (home, away), match_date in zip(pairs, dates):
        try:
            r = fetch_one(home, away, hash_to_name, match_date)
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
