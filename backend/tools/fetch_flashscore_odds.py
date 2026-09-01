"""
用真实浏览器 (Playwright) 渲染 Flashscore 单场 odds 页，抓取

    1X2      —— 主胜 / 平局 / 客胜
    亚盘     —— Asian Handicap（让球盘口 + 主客赔率）
    大小球   —— Over/Under（进球数盘口 + 大/小赔率）

作为 API-Football `/odds` 拉取失败时的兜底数据源。

URL 规律
--------
    https://www.flashscore.com/match/football/
        <home_slug>-<home_hash>/<away_slug>-<away_hash>/odds/<market>/full-time/[?mid=<mid>]

    market: 1x2-odds | asian-handicap | over-under
    slug   : 队名小写并去掉所有非字母数字字符（hash 正确时 slug 写错也会被重定向）

关键坑：hash 对只标识"两队"，不标识"哪一场"
--------------------------------------------
仅用两队 hash 拼出的 URL 会被 Flashscore 重定向到两队之间的**某一场**比赛，
实测优先命中"最近已结束的那场"，而不是我们要预测的未来场次。例如
Mjallby(主) vs Djurgarden 于 2026-09-04，pair URL 却落到 2026-09-01 已结束的
Djurgarden(主) vs Mjallby，赔率完全错配。

解决办法（本模块的核心逻辑）：
  1. 先直接打开 pair URL，核对页面 header 的比赛日期（dd.mm.yyyy HH:MM，实测与
     库里的北京时间一致）与双方队名；一致则直接使用。
  2. 不一致则从页面面包屑取出联赛链接 -> 打开 `<联赛>/fixtures/` 赛程页 ->
     按"日期 ±1 天 + 双方队名"定位那一行 -> 取出该行的 `?mid=`。
  3. 用 `?mid=` 重新打开 odds 页并再次核对日期，仍不一致则抛错（宁可无赔率，
     也不能喂给 LLM 错场次的赔率）。

输出结构与 API-Football 的 `odds_data` 同构，可直接喂给
`prediction.predict._odds_to_text` / `_save_odds`：

    {"odds_data": [{"bookmaker": "Flashscore", "entries": [entry]}]}

其中 entry 为各家赔率均值（市场共识），字段与 API-Football 解析结果一致：
    date / home_odd / draw_odd / away_odd / home_raw / draw_raw / away_raw
    ah_line / ah_home_odd / ah_away_odd / ah_home_raw / ah_away_raw
    ou_line / ou_over_odd / ou_under_odd / ou_over_raw / ou_under_raw

用法
----
    # 按 fixture id 抓取（自动从 DB 取队名/日期，并从 team_map 反查 hash）
    python tools/fetch_flashscore_odds.py --fixture 1494190

    # 直接指定 hash 与比赛信息
    python tools/fetch_flashscore_odds.py --pair S0XtXM1E 4Kh5hPE1 \
        --home "Mjallby AIF" --away "Djurgardens IF" --date 2026-09-04

    # 结果写 JSON
    python tools/fetch_flashscore_odds.py --fixture 1494190 --out odds.json
"""
import argparse
import json
import re
import sys
from datetime import date as date_cls, datetime
from pathlib import Path

from loguru import logger
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MAP_FILE = Path(__file__).resolve().parent / "flashscore_team_map.json"

ODDS_URL_TMPL = (
    "https://www.flashscore.com/match/football/"
    "{home_slug}-{home_hash}/{away_slug}-{away_hash}/odds/{market}/full-time/"
)

# Flashscore 的 market 路径片段
MARKET_PATH = {
    "1x2": "1x2-odds",
    "asian_handicap": "asian-handicap",
    "over_under": "over-under",
}

# 合成的"庄家"名：本模块输出的是各家均值（市场共识），而非某一家
CONSENSUS_BOOKMAKER = "Flashscore"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 比赛日期核对容差（天）。库里是北京时间，Flashscore 页面按比赛当地时区显示，
# 跨时区可能相差 1 天。
DATE_TOLERANCE_DAYS = 1

# 队名里无辨识度的通用词，不参与匹配
_NAME_STOPWORDS = {
    "if", "fc", "fk", "sk", "ac", "as", "bk", "cf", "cd", "ud", "sc", "rc",
    "sv", "ii", "u19", "u21", "u23", "the",
}


# ────────────────────────────── 文本/数值工具 ──────────────────────────────

def slugify(name: str) -> str:
    """生成 Flashscore 风格的 slug：小写 + 只保留字母数字。"""
    return "".join(c for c in (name or "").lower() if c.isalnum())


def _norm(name: str) -> str:
    """规范化队名以便比较：小写、非字母数字转空格。"""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _stem(name: str) -> str:
    """取队名中最有辨识度的词干（最长单词的前 5 个字符），用于宽松匹配。

    Flashscore 常显示短名（"Djurgarden"），库里存全名（"Djurgardens IF"），
    直接包含判断会失败，用词干前缀则能命中。
    """
    words = [w for w in _norm(name).split() if w not in _NAME_STOPWORDS]
    if not words:
        return _norm(name)[:5]
    return max(words, key=len)[:5]


def _names_match(a: str, b: str) -> bool:
    """两支球队名是否指向同一队（兼容全名/短名/前后缀差异）。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return _stem(a) in nb or _stem(b) in na


def _row_has_team(row_norm: str, team: str) -> bool:
    """赛程页的一行文本里是否出现了指定球队。"""
    if not row_norm:
        return False
    for w in _norm(team).split():
        if w in _NAME_STOPWORDS or len(w) < 3:
            continue
        if w[:5] in row_norm:
            return True
    return False


def _as_date(value):
    """把 date/datetime/字符串统一为 datetime.date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_cls):
        return value
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    if m:
        return date_cls(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _parse_dm(text: str, target: date_cls):
    """从赛程页文本里解析 'dd.mm.' 形式的日期，按离 target 最近选年份。

    赛程页只显示 '04.09. 01:00'（无年份），需要结合目标日期推断年份。
    """
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.", text or "")
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    best = None
    for year in (target.year - 1, target.year, target.year + 1):
        try:
            cand = date_cls(year, month, day)
        except ValueError:
            continue
        diff = abs((cand - target).days)
        if best is None or diff < best[0]:
            best = (diff, cand)
    return best[1] if best else None


def _dates_match(page_date, target, tol_days: int = DATE_TOLERANCE_DAYS) -> bool:
    if page_date is None or target is None:
        return False
    return abs((page_date - target).days) <= tol_days


def _parse_line(raw: str):
    """解析盘口线文本。

    Flashscore 的四分盘写作 '-1.5, -2'（即 -1.75），这里取各档均值。
    依赖页面使用 '.' 作为小数点（已通过 UA + locale=en-US 固定为英文站）。
    """
    parts = [p.strip() for p in str(raw or "").split(",") if p.strip()]
    if not parts:
        return None
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return None
    return round(sum(vals) / len(vals), 2)


def _to_float(raw: str):
    """把赔率文本转 float，过滤占位/被庄家撤下的无效值。"""
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not (1.0 <= val <= 1000.0):
        return None
    return val


# ────────────────────────────── 浏览器交互 ──────────────────────────────

# 抽取赔率表每一行：庄家名 / 盘口线 / 各位赔率（含是否被撤单）
_ROWS_JS = """
() => Array.from(document.querySelectorAll('div.ui-table__row')).map(r => {
    const img = r.querySelector('img[alt]');
    const lineEl = r.querySelector('div[class*="wcl-oddsCell"]');
    return {
        bookmaker: img ? (img.getAttribute('alt') || '').trim() : '',
        line: lineEl ? (lineEl.innerText || '').trim() : '',
        odds: Array.from(r.querySelectorAll('a.oddsCell__odd')).map(a => ({
            text: (a.innerText || '').trim(),
            removed: !!a.querySelector('.oddsCell__lineThrough'),
        })),
    };
})
"""

# 面包屑里的联赛链接（形如 /football/sweden/allsvenskan/，文本含 "- ROUND 19"）
_LEAGUE_JS = """
() => {
    const out = [];
    document.querySelectorAll('a[href*="/football/"]').forEach(a => {
        const href = ((a.getAttribute('href') || '').split('?')[0]).trim();
        const text = (a.innerText || '').trim();
        if (/^\\/football\\/[^/]+\\/[^/]+\\/?$/.test(href) && /round\\s*\\d+/i.test(text)) {
            out.push({href: href, text: text});
        }
    });
    return out;
}
"""

# 赛程页：每个 ?mid= 链接向上找到最近的、含日期的容器，返回其文本
_FIXTURES_JS = """
() => {
    const out = [];
    document.querySelectorAll('a[href*="mid="]').forEach(a => {
        const href = a.getAttribute('href') || '';
        const m = href.match(/mid=([A-Za-z0-9]+)/);
        if (!m) return;
        let el = a, text = '';
        for (let i = 0; i < 6 && el; i++) {
            const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            if (/\\d{1,2}\\.\\d{1,2}\\./.test(t)) { text = t; break; }
            el = el.parentElement;
        }
        out.push({mid: m[1], text: text.slice(0, 200)});
    });
    return out;
}
"""


def _accept_consent(page) -> None:
    """点掉 Flashscore 的隐私/同意弹窗（若存在）。"""
    for sel in ["text=AGREE", "text=I ACCEPT", "text=Accept", "text=OK"]:
        try:
            page.click(sel, timeout=2500)
            break
        except Exception:
            pass


def _odds_url(home_slug, home_hash, away_slug, away_hash, market, mid=None) -> str:
    url = ODDS_URL_TMPL.format(
        home_slug=home_slug, home_hash=home_hash,
        away_slug=away_slug, away_hash=away_hash,
        market=MARKET_PATH[market],
    )
    return f"{url}?mid={mid}" if mid else url


def _goto_odds(page, url: str) -> None:
    """打开赔率页并等待表格异步渲染完成。"""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    _accept_consent(page)
    try:
        page.wait_for_selector("div.ui-table__row", timeout=25000)
    except Exception:
        logger.debug(f"Flashscore 赔率表未出现（可能该场无赔率）: {url}")
    page.wait_for_timeout(2000)  # 等赔率动画/占位值替换完成


def _read_page_info(page) -> dict:
    """读取当前比赛页的身份信息：日期、双方队名（按主/客顺序）、面包屑联赛链接。"""
    text = page.evaluate("() => document.body.innerText") or ""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # header 里的 'dd.mm.yyyy HH:MM'，实测与库里的北京时间一致
    page_date = None
    for ln in lines[:80]:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})", ln)
        if m:
            try:
                page_date = date_cls(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                page_date = None
            break

    # 双方队名：优先取正文首行的 'X v Y dd/mm/yyyy'（未开赛与已结束的页面都有），
    # 取不到再退回 title（形如 'MJA - DJU | Mjallby v Djurgarden 03/09/2026, Odds ...'）
    home = away = None
    teams_re = re.compile(r"^\s*(.+?)\s+v\s+(.+?)\s+\d{1,2}/\d{1,2}/\d{4}\b")
    for ln in lines[:10]:
        m = teams_re.match(ln)
        if m:
            home, away = m.group(1).strip(), m.group(2).strip()
            break
    if home is None:
        m = re.search(r"(?:^|\|)\s*([^|]+?)\s+v\s+([^|]+?)\s+\d{1,2}/\d{1,2}/\d{4}\b",
                      page.title() or "")
        if m:
            home, away = m.group(1).strip(), m.group(2).strip()

    leagues = page.evaluate(_LEAGUE_JS) or []
    return {"date": page_date, "home": home, "away": away,
            "leagues": leagues, "url": page.url}


def _matches_target(info: dict, target: date_cls, home_name: str, away_name: str) -> bool:
    """页面比赛是否与目标 fixture 一致（日期 + 主客双方）。"""
    if not _dates_match(info.get("date"), target):
        return False
    return _names_match(info.get("home"), home_name) and _names_match(info.get("away"), away_name)


def _find_mid_on_fixtures(page, target: date_cls, home_name: str, away_name: str,
                          max_scrolls: int = 3):
    """在联赛赛程页按 '日期 + 双方队名' 找到目标比赛的 ?mid=。"""
    for attempt in range(max_scrolls + 1):
        for row in page.evaluate(_FIXTURES_JS) or []:
            row_date = _parse_dm(row.get("text", ""), target)
            if not _dates_match(row_date, target):
                continue
            row_norm = _norm(row.get("text", ""))
            if _row_has_team(row_norm, home_name) and _row_has_team(row_norm, away_name):
                return row["mid"], row.get("text", "")
        if attempt < max_scrolls:
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(1500)
    return None, None


def _resolve_mid(page, info: dict, target: date_cls, home_name: str, away_name: str):
    """pair URL 命中的场次不对时，经联赛赛程页拿到目标场次的 match id。"""
    leagues = info.get("leagues") or []
    if not leagues:
        raise ValueError("未能从页面面包屑解析出联赛链接，无法定位目标场次")
    league_href = leagues[0]["href"].rstrip("/")
    fixtures_url = f"https://www.flashscore.com{league_href}/fixtures/"
    logger.info(f"Flashscore pair URL 命中错误场次，改由联赛赛程定位: {fixtures_url}")
    page.goto(fixtures_url, wait_until="domcontentloaded", timeout=60000)
    _accept_consent(page)
    page.wait_for_timeout(5000)

    mid, row_text = _find_mid_on_fixtures(page, target, home_name, away_name)
    if not mid:
        raise ValueError(
            f"联赛赛程页未找到 {target} {home_name} vs {away_name} 对应的场次"
        )
    logger.info(f"Flashscore 定位到目标场次 mid={mid}（赛程行: {row_text[:80]}）")
    return mid


# ────────────────────────────── 赔率解析 ──────────────────────────────

def _read_rows(page) -> list[dict]:
    """读取当前赔率页所有行，转成结构化 dict。"""
    rows = []
    for raw in page.evaluate(_ROWS_JS) or []:
        odds = [_to_float(o["text"]) for o in raw.get("odds", [])]
        if any(o is None for o in odds):
            continue  # 占位值 / 已被庄家撤下
        if any(o.get("removed") for o in raw.get("odds", [])):
            continue
        rows.append({
            "bookmaker": raw.get("bookmaker") or "",
            "line": _parse_line(raw.get("line", "")),
            "odds": odds,
        })
    return rows


def _parse_market(rows: list[dict], kind: str) -> list[dict]:
    """按盘口类型把原始行解析成业务结构。

    1X2 行有 3 个赔率（主/平/客）；亚盘与大小球行有 2 个赔率 + 1 个盘口线。
    """
    out = []
    for r in rows:
        odds = r["odds"]
        if kind == "1x2":
            if len(odds) != 3:
                continue
            out.append({
                "bookmaker": r["bookmaker"],
                "home": odds[0], "draw": odds[1], "away": odds[2],
            })
        elif kind == "asian_handicap":
            if len(odds) != 2 or r["line"] is None:
                continue
            out.append({
                "bookmaker": r["bookmaker"],
                "line": r["line"], "home": odds[0], "away": odds[1],
            })
        elif kind == "over_under":
            if len(odds) != 2 or r["line"] is None:
                continue
            out.append({
                "bookmaker": r["bookmaker"],
                "line": r["line"], "over": odds[0], "under": odds[1],
            })
    return out


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _build_entry(one_xtwo: list[dict], asian: list[dict], over_under: list[dict],
                 snapshot_date: str) -> dict | None:
    """把各家原始盘口汇总为一条"市场共识"记录。

    取各庄家原始赔率的算术平均，再用与 API-Football 相同的归一化方式
    （隐含概率 = (1/赔率) / Σ(1/赔率)）换算，保证两套数据源口径一致。
    亚盘与大小球取"主客（大/小）均值最接近"的盘口线，即庄家认为最均衡的盘。
    """
    entry = {"date": snapshot_date}

    if one_xtwo:
        ho = _mean([r["home"] for r in one_xtwo])
        dr = _mean([r["draw"] for r in one_xtwo])
        aw = _mean([r["away"] for r in one_xtwo])
        total = 1 / ho + 1 / dr + 1 / aw
        entry.update({
            "home_odd": round(1 / ho / total, 3),
            "draw_odd": round(1 / dr / total, 3),
            "away_odd": round(1 / aw / total, 3),
            "home_raw": round(ho, 2), "draw_raw": round(dr, 2), "away_raw": round(aw, 2),
        })

    if asian:
        by_line: dict[float, list[dict]] = {}
        for r in asian:
            by_line.setdefault(r["line"], []).append(r)
        best = min(by_line.items(),
                   key=lambda kv: abs(_mean([x["home"] for x in kv[1]])
                                      - _mean([x["away"] for x in kv[1]])))
        line, rows = best
        ho = _mean([r["home"] for r in rows])
        aw = _mean([r["away"] for r in rows])
        total = 1 / ho + 1 / aw
        entry.update({
            "ah_line": line,
            "ah_home_odd": round(1 / ho / total, 3),
            "ah_away_odd": round(1 / aw / total, 3),
            "ah_home_raw": round(ho, 2), "ah_away_raw": round(aw, 2),
        })

    if over_under:
        by_line = {}
        for r in over_under:
            by_line.setdefault(r["line"], []).append(r)
        best = min(by_line.items(),
                   key=lambda kv: abs(_mean([x["over"] for x in kv[1]])
                                      - _mean([x["under"] for x in kv[1]])))
        line, rows = best
        ov = _mean([r["over"] for r in rows])
        un = _mean([r["under"] for r in rows])
        total = 1 / ov + 1 / un
        entry.update({
            "ou_line": line,
            "ou_over_odd": round(1 / ov / total, 3),
            "ou_under_odd": round(1 / un / total, 3),
            "ou_over_raw": round(ov, 2), "ou_under_raw": round(un, 2),
        })

    # 三类盘口一个都没有时视为抓取失败
    if not (one_xtwo or asian or over_under):
        return None
    return entry


# ────────────────────────────── 主入口 ──────────────────────────────

def scrape_match_odds(home_hash: str, away_hash: str, hash_to_name: dict,
                      match_date, markets: list[str] | None = None,
                      headless: bool = True) -> dict:
    """抓取单场比赛的 1X2 / 亚盘 / 大小球赔率。

    参数
    ----
    home_hash / away_hash : flashscore_team_map.json 中的 8 位 hash
    hash_to_name         : {hash: 队名}，用于生成 slug 与核对页面队名
    match_date           : 目标比赛日期（库里的北京时间），**必填**，用于核对场次
    markets              : 需要抓取的盘口，默认三类全抓

    返回
    ----
    dict，含 `odds_data`（与 API-Football 同构，可直接落库/渲染提示词）；
    抓取失败时 `odds_data` 为 None 并附 `error` 字段。
    """
    home_name = hash_to_name.get(home_hash)
    away_name = hash_to_name.get(away_hash)
    if not home_name or not away_name:
        raise ValueError(
            f"hash 未解析到队名: home={home_hash}({home_name}) away={away_hash}({away_name})"
        )
    target = _as_date(match_date)
    if target is None:
        raise ValueError(f"scrape_match_odds 必须提供可解析的 match_date，收到 {match_date!r}")

    markets = markets or ["1x2", "asian_handicap", "over_under"]
    home_slug, away_slug = slugify(home_name), slugify(away_name)

    result = {
        "home_hash": home_hash, "away_hash": away_hash,
        "home_name": home_name, "away_name": away_name,
        "match_date": target.isoformat(),
        "resolved_via": None, "mid": None, "url": None,
        "page_date": None, "page_home": None, "page_away": None,
        "raw": {}, "odds_data": None, "error": None,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent=UA, locale="en-US")
        try:
            mid = None
            url = _odds_url(home_slug, home_hash, away_slug, away_hash, "1x2")
            logger.info(f"Flashscore 赔率抓取: {url}")
            _goto_odds(page, url)
            info = _read_page_info(page)

            if _matches_target(info, target, home_name, away_name):
                result["resolved_via"] = "direct"
            else:
                mid = _resolve_mid(page, info, target, home_name, away_name)
                result["resolved_via"] = "league-fixtures"
                result["mid"] = mid
                url = _odds_url(home_slug, home_hash, away_slug, away_hash, "1x2", mid)
                _goto_odds(page, url)
                info = _read_page_info(page)
                if not _matches_target(info, target, home_name, away_name):
                    raise ValueError(
                        f"按 mid={mid} 打开后比赛日期 {info['date']} 仍与目标 {target} 不一致"
                    )

            result["url"] = url
            result["page_date"] = info["date"].isoformat() if info["date"] else None
            result["page_home"] = info["home"]
            result["page_away"] = info["away"]

            for market in markets:
                if market != "1x2":
                    _goto_odds(page, _odds_url(home_slug, home_hash, away_slug,
                                               away_hash, market, mid))
                result["raw"][market] = _parse_market(_read_rows(page), market)

            entry = _build_entry(
                result["raw"].get("1x2", []),
                result["raw"].get("asian_handicap", []),
                result["raw"].get("over_under", []),
                date_cls.today().isoformat(),
            )
            if entry is None:
                result["error"] = "页面无可用赔率（三类盘口均为空）"
                logger.warning(f"Flashscore 无可用赔率: {url}")
            else:
                result["odds_data"] = [
                    {"bookmaker": CONSENSUS_BOOKMAKER, "entries": [entry]}
                ]
        finally:
            browser.close()

    return result


# ────────────────────────────── CLI ──────────────────────────────

def load_hash_to_name(db) -> dict:
    """flashscore hash -> 队名（来自 teams 表 + team_map 反查）。"""
    from sqlalchemy import text

    rows = db.execute(text("SELECT id, name FROM teams")).fetchall()
    id_to_name = {r[0]: r[1] for r in rows}
    override = json.loads(MAP_FILE.read_text(encoding="utf-8")) if MAP_FILE.exists() else {}
    hash_to_name, missing = {}, []
    for h, tid in override.items():
        name = id_to_name.get(tid)
        if name:
            hash_to_name[h] = name
        else:
            missing.append((h, tid))
    if missing:
        logger.warning(f"{len(missing)} 个 hash 在 teams 表找不到队名: {missing[:5]}...")
    return hash_to_name


def _load_fixture(db, fixture_id: int):
    """按 fixture id 取出 (home_hash, away_hash, hash_to_name, date, 队名)。"""
    from sqlalchemy import text

    row = db.execute(text(
        "SELECT id, home_id, away_id, date, home_name, away_name FROM fixtures WHERE id = :fid"
    ), {"fid": fixture_id}).fetchone()
    if not row:
        raise SystemExit(f"比赛不存在 fixture_id={fixture_id}")
    override = json.loads(MAP_FILE.read_text(encoding="utf-8")) if MAP_FILE.exists() else {}
    id_to_hash = {tid: h for h, tid in override.items()}
    home_hash, away_hash = id_to_hash.get(row[1]), id_to_hash.get(row[2])
    if not home_hash or not away_hash:
        raise SystemExit(
            f"fixture {fixture_id} 的球队不在 flashscore_team_map.json: "
            f"home={row[4]}({row[1]}) away={row[5]}({row[2]})"
        )
    return home_hash, away_hash, {home_hash: row[4], away_hash: row[5]}, row[3]


def main() -> None:
    ap = argparse.ArgumentParser(description="抓取 Flashscore 单场赔率（1X2 / 亚盘 / 大小球）")
    ap.add_argument("--fixture", type=int, help="按 fixture id 抓取（自动取队名与日期）")
    ap.add_argument("--pair", nargs=2, metavar=("HOME_HASH", "AWAY_HASH"))
    ap.add_argument("--home", help="主队名（与 --pair 配合）")
    ap.add_argument("--away", help="客队名（与 --pair 配合）")
    ap.add_argument("--date", help="比赛日期 YYYY-MM-DD（与 --pair 配合，必填）")
    ap.add_argument("--out", help="结果输出 JSON 路径")
    args = ap.parse_args()

    if args.fixture:
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            home_hash, away_hash, hash_to_name, match_date = _load_fixture(db, args.fixture)
        finally:
            db.close()
    elif args.pair:
        if not (args.home and args.away and args.date):
            ap.error("--pair 需同时提供 --home / --away / --date")
        home_hash, away_hash = args.pair
        hash_to_name = {home_hash: args.home, away_hash: args.away}
        match_date = args.date
    else:
        ap.error("需指定 --fixture 或 --pair")

    result = scrape_match_odds(home_hash, away_hash, hash_to_name, match_date)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"[saved] {args.out}")
    if result.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
