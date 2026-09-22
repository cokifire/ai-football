"""
通过 Flashscore 抓取联赛积分榜并写入 standings 表。

设计要点
--------
* 字段映射: Flashscore 行 (rank/team/MP/W/D/L/GF:GA/GD/PTS/form) 几乎 1:1 对应
  Standing 模型, 整体数据写入 all_* 列 (与现有 sync_standings 的 API-Football 来源一致)。
* team_id 转换 (核心问题):
  Flashscore 的球队 ID 是无意义的 8 位 hash, 与 API-Football 的整数 team_id 完全不相交。
  本项目(含老数据)统一以 API-Football 整数 team_id 作为系统主键, 因此这里通过
  「队名归一化 + 去除俱乐部前后缀(IF/BK/FF/FK/SK/IS/AIF…)」把 Flashscore 队名解析成
  teams 表中的 api_football_id, 从而与旧 standings 数据保持一致。
* 解析结果(含 flashscore hash -> api_football_id)会持久化到 flashscore_team_map.json,
  便于审阅与手动覆盖(override)。

用法
----
    python tools/fetch_flashscore_standings.py [league_id] [--dry-run]
"""
import json
import random
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from playwright.sync_api import sync_playwright
from sqlalchemy import text

from app.db.session import SessionLocal
from app.models.standing import Standing
from app.models.league import League

# league_id -> Flashscore 完整积分榜 URL (竞赛 ID 在 standings/<comp_id>/ 处)
URLS = {
    113: "https://www.flashscore.com/football/sweden/allsvenskan/standings/ltrtRhko/standings/overall/",
    2: "https://www.flashscore.com/football/europe/champions-league/standings/UiRZST3U/standings/overall/",
    71: "https://www.flashscore.com/football/brazil/serie-a-betano/standings/hdLUdQGi/standings/overall/",
    94: "https://www.flashscore.com/football/portugal/liga-portugal/standings/hhJ7LFzn/standings/overall/",
    88: "https://www.flashscore.com/football/netherlands/eredivisie/standings/zm1be8bD/standings/overall/",
    140: "https://www.flashscore.com/football/spain/laliga/standings/dWdJXP6U/standings/overall/",
    39: "https://www.flashscore.com/football/england/premier-league/standings/GO4S62sM/standings/overall/",
    135: "https://www.flashscore.com/football/italy/serie-a/standings/S0nT1X7q/standings/overall/",
    61: "https://www.flashscore.com/football/france/ligue-1/standings/4R7iGq1l/standings/overall/",
    78: "https://www.flashscore.com/football/germany/bundesliga/standings/jg0MwVuC/standings/overall/",
}

# 需要抓取「多个子页面」的联赛: 每个子页面各自还有分组(Group)。
# 格式: league_id -> [(子页面标签, URL), ...]
# 例: UEFA Nations League 分 League A/B/C/D 四个独立比赛页, 每个内部再分 Group 1..4,
# 最终 group_name 形如 "League A, Group 1"。
MULTI_URLS = {
    5: [
        ("League A", "https://www.flashscore.com/football/europe/uefa-nations-league/standings/A1uGXGgK/standings/overall/"),
        ("League B", "https://www.flashscore.com/football/europe/uefa-nations-league/standings/UoMnSfHs/standings/overall/"),
        ("League C", "https://www.flashscore.com/football/europe/uefa-nations-league/standings/O8LjREWm/standings/overall/"),
        ("League D", "https://www.flashscore.com/football/europe/uefa-nations-league/standings/6Hr3QYof/standings/overall/"),
    ],
}

OVERRIDE_FILE = Path(__file__).resolve().parent / "flashscore_team_map.json"

# 多子页面抓取之间的随机等待区间(秒), 降低被反爬封锁的概率
SCRAPE_INTERVAL = (5, 12)

# 逐队解析明细默认不打日志(太吵); CLI 传 --verbose 时打开, 便于排查映射
VERBOSE = False

# 俱乐部常见前后缀, 归一化时剥离, 以对齐 "Hammarby" <-> "Hammarby FF" 这类差异
CLUB_TOKENS = {
    "if", "bk", "ff", "fk", "sk", "is", "aif", "fc", "ac", "cf", "sc",
    "ik", "afc", "rkc", "kv", "mtk", "fk", "fk",
}


def norm(s: str) -> str:
    """小写 + 去变音符(ö->o, å->a) + 去非字母数字。"""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def core(s: str) -> str:
    """在 norm 基础上去除头尾俱乐部词, 得到队名核心。"""
    # norm() 会移除空格，不能直接拿它 split；这里保留词边界来剥离
    # "Hammarby FF" / "FF Hammarby" 这类 Flashscore 与 API-Football 的命名差异。
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    parts = re.findall(r"[a-z0-9]+", s.lower())
    while parts and parts[0] in CLUB_TOKENS:
        parts.pop(0)
    while parts and parts[-1] in CLUB_TOKENS:
        parts.pop(-1)
    return "".join(parts)


# ---------------------------------------------------------------------------
# 队名 -> api_football_id 解析
# ---------------------------------------------------------------------------
def load_teams(db):
    # 取规范名 + logo, 用于解析后回填徽标
    rows = db.execute(text("SELECT id, name, logo FROM teams")).fetchall()
    # 也纳入已有 standings 的 (team_name, team_id, team_logo): 当 teams 表规范名与
    # API-Football 实际返回的队名不一致时(如 Athletico-PR), 用 standings 里的
    # 真实队名+正确 id+徽标兜底, 提升解析命中率。
    rows += db.execute(
        text(
            "SELECT DISTINCT team_id, team_name, team_logo FROM standings "
            "WHERE team_name IS NOT NULL"
        )
    ).fetchall()
    seen, result = set(), []
    for r in rows:
        rid, rname, rlogo = r[0], r[1], (r[2] or "")
        if (rid, rname) in seen:
            continue
        seen.add((rid, rname))
        result.append(
            {
                "id": rid,
                "name": rname,
                "logo": rlogo,
                "norm": norm(rname),
                "core": core(rname),
            }
        )
    return result


def load_team_extra(db) -> dict:
    """从 fixtures 表补充 (team_id -> 队名/徽标)。

    有些球队(如 Lask Linz id=1026)尚未进入 teams 表, 但在已同步的赛程里出现过,
    这里取出其队名与徽标 URL 作为兜底, 避免写入的 standings 行缺少队名/徽标。
    仅作信息补充, 不参与队名解析(以免影响既有解析优先级)。
    """
    extra = {}
    rows = db.execute(
        text(
            "SELECT DISTINCT home_id, home_name, home_logo FROM fixtures "
            "WHERE home_id IS NOT NULL AND home_name IS NOT NULL AND home_name <> '' "
            "UNION "
            "SELECT DISTINCT away_id, away_name, away_logo FROM fixtures "
            "WHERE away_id IS NOT NULL AND away_name IS NOT NULL AND away_name <> ''"
        )
    ).fetchall()
    for rid, rname, rlogo in rows:
        if rid in extra and extra[rid]["logo"]:
            continue
        extra[rid] = {"name": rname, "logo": rlogo or ""}
    return extra


def load_override() -> dict:
    if OVERRIDE_FILE.exists():
        try:
            return json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_override(override: dict):
    OVERRIDE_FILE.write_text(
        json.dumps(override, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def resolve(fs_name: str, fs_hash: str, teams, override: dict):
    """返回 (api_football_id, 说明)。优先级: hash 覆盖 > 精确core > 精确norm > 子串 > 0。"""
    if fs_hash in override:
        return override[fs_hash], "override(hash)"
    fn, fc = norm(fs_name), core(fs_name)
    best, best_score = None, -1
    for t in teams:
        score = 0
        if t["core"] and fc and t["core"] == fc:
            score = 3
        elif t["norm"] and fn and t["norm"] == fn:
            score = 2
        elif fc and t["core"] and (fc in t["core"] or t["core"] in fc):
            score = 1
        elif fn and t["norm"] and (fn in t["norm"] or t["norm"] in fn):
            score = 1
        if score > best_score:
            best_score, best = score, t
    if best and best_score >= 1:
        return best["id"], f"name(core={fc}, match={best['name']}, score={best_score})"
    return 0, "UNRESOLVED"


# ---------------------------------------------------------------------------
# Flashscore 抓取与解析
# ---------------------------------------------------------------------------
def fetch_tables(url: str):
    """打开页面, 返回 [{"group": 组名, "blob": 表格文本, "links": [球队hash...]}]。

    Flashscore 的分组赛制(如 Nations League、解放者杯小组赛)会把每个小组渲染成
    独立的 div.tableWrapper, 表头带组名(GROUP 1 / Group 1)。单表格联赛则只有一项,
    group 为空字符串 —— 解析结果与旧的 fetch_blob 兼容。
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for sel in ["text=AGREE", "text=I ACCEPT", "text=Accept", "text=OK"]:
            try:
                page.click(sel, timeout=3000)
                break
            except Exception:
                pass
        # 淘汰赛/抽签类页面会重定向到 /draw/, 没有积分榜表格
        if "/draw/" in page.url:
            logger.warning(f"页面重定向到抽签视图, 无积分榜表格: {page.url}")
            browser.close()
            return []

        try:
            page.wait_for_selector("div.tableWrapper", timeout=30000)
        except Exception:
            logger.warning(f"未等到积分榜表格: {url}")
            browser.close()
            return []
        page.wait_for_timeout(1500)

        tables = page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('div.tableWrapper').forEach(tw => {
                    const txt = tw.innerText || '';
                    if (!txt.includes('PTS')) return;
                    const hdr = tw.querySelector('[class*="headerCell--participant"]');
                    const group = hdr
                        ? (hdr.getAttribute('title') || (hdr.innerText || '').trim())
                        : '';
                    const links = [...tw.querySelectorAll('a[href*="/team/"]')].map(a => ({
                        name: (a.innerText || '').trim()
                              || ((a.querySelector('img') || {}).alt || ''),
                        hash: a.getAttribute('href').split('/').filter(Boolean).pop(),
                    }));
                    out.push({group: group, blob: txt, links: links});
                });
                return out;
            }"""
        )
        browser.close()

    # 同一队可能在表内出现两次(队名 + 队徽各一个链接), 去重
    for tb in tables or []:
        uniq = {}
        for ln in tb["links"]:
            uniq.setdefault(ln["hash"], ln["name"])
        tb["links"] = [{"hash": h, "name": n} for h, n in uniq.items()]
    return tables or []


def parse_standings(blob: str):
    """把积分榜文本解析为队伍字典列表。每行 15 个字段:
    rank. / name / [recent_score] / MP / W / D / L / GF:GA / GD / PTS / sep / F1..F5。
    兼容两种结构：瑞典超无recent_score列，五大联赛/荷甲有recent_score列（自动检测跳过）。"""
    lines = [l.strip() for l in blob.split("\n") if l.strip()]
    teams = []
    i = 0
    while i < len(lines) and not re.match(r"^\d+\.$", lines[i]):
        i += 1
    while i < len(lines):
        if not re.match(r"^\d+\.$", lines[i]):
            break
        rank = int(lines[i].rstrip("."))
        name = lines[i + 1]
        # 检测是否有最近比分列（格式如 3-0 / 2:1）
        offset = 0
        if i + 2 < len(lines) and re.match(r"^\d+[-:]\d+$", lines[i + 2]):
            offset = 1
        played = int(lines[i + 2 + offset])
        won = int(lines[i + 3 + offset])
        draw = int(lines[i + 4 + offset])
        lost = int(lines[i + 5 + offset])
        gf, ga = map(int, lines[i + 6 + offset].split(":"))
        gd = int(lines[i + 7 + offset])
        points = int(lines[i + 8 + offset])
        j = i + 10 + offset
        form = []
        while j < len(lines) and re.match(r"^[WDL]$", lines[j]):
            form.append(lines[j])
            j += 1
        teams.append(
            dict(
                rank=rank, name=name, played=played, won=won, draw=draw,
                lost=lost, gf=gf, ga=ga, gd=gd, points=points,
                form="".join(form),
            )
        )
        i = j
    return teams


def detect_season(blob: str) -> int:
    m = re.search(r"\b(20\d{2})\b", blob)
    return int(m.group(1)) if m else datetime.now().year


def _apply(standing: Standing, t: dict, team_name: str, team_logo: str = "", group_name: str = ""):
    standing.rank = t["rank"]
    standing.team_name = team_name
    standing.team_logo = team_logo
    standing.group_name = group_name
    standing.points = t["points"]
    standing.goals_diff = t["gd"]
    standing.form = t["form"]
    standing.all_played = t["played"]
    standing.all_win = t["won"]
    standing.all_draw = t["draw"]
    standing.all_lose = t["lost"]
    standing.all_goals_for = t["gf"]
    standing.all_goals_against = t["ga"]


def _sources(league_id: int):
    """返回 [(子页面标签 or None, URL), ...]。"""
    if league_id in MULTI_URLS:
        return MULTI_URLS[league_id]
    url = URLS.get(league_id)
    return [(None, url)] if url else []


def _group_name(league_name: str, label, group: str, n_tables: int) -> str:
    """决定写入 standings.group_name 的名称。

    * 多子页面 + 组名  -> "League A, Group 1"
    * 仅子页面         -> "League A"
    * 单表格联赛       -> 联赛名(如 "Eredivisie"), 与 API-Football 来源一致
    * 单页面多组       -> "联赛名, Group A"
    """
    if label and group:
        return f"{label}, {group}"
    if label:
        return label
    if group and n_tables > 1:
        return f"{league_name}, {group}"
    return league_name


def run(league_id: int, db=None, dry_run: bool = False):
    """抓取并写入某联赛积分榜。

    db: 传入外部 Session 时复用它(由调用方负责最终 commit/close);
        为 None 时自行创建 SessionLocal 并提交/关闭。
    """
    sources = _sources(league_id)
    if not sources:
        try:
            from fetch_flashscore_draw import DRAW_URLS as _DRAW_URLS
        except Exception:
            _DRAW_URLS = {}
        if league_id in _DRAW_URLS:
            logger.info(
                f"联赛 {league_id} 处于淘汰赛阶段, 无积分榜表格 (已配置对阵页, 见 /standings/draw)"
            )
        else:
            logger.error(f"未配置 league_id={league_id} 的 Flashscore URL")
        return False

    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        team_rows = load_teams(db)
        team_extra = load_team_extra(db)
        override = load_override()
        league_name = (
            db.query(League.name)
            .filter(League.id == league_id)
            .scalar()
            or f"league_{league_id}"
        )
        unresolved = []
        season = None
        written = 0

        for idx, (label, url) in enumerate(sources):
            # 多个子页面之间随机等待, 降低被反爬封锁的概率
            if idx > 0:
                wait = random.uniform(*SCRAPE_INTERVAL)
                logger.info(f"Flashscore 子页面间隔等待 {wait:.1f}s")
                time.sleep(wait)

            logger.info(f"抓取联赛 {league_id} 积分榜: {url}")
            tables = fetch_tables(url)
            if not tables:
                logger.error(f"未能抓取到积分榜表格 (可能被反爬拦截或页面改版): {url}")
                continue

            for tb in tables:
                group_name = _group_name(league_name, label, tb["group"], len(tables))
                if season is None:
                    season = detect_season(tb["blob"])
                teams = parse_standings(tb["blob"])
                if not teams:
                    logger.warning(f"  [{group_name}] 未解析到任何球队")
                    continue
                if VERBOSE:
                    logger.info(f"  [{group_name}] 解析到 {len(teams)} 支球队")

                # 用队名(归一化)对齐 Flashscore hash, 避免链接顺序与文本行错位
                hash_by_name = {}
                for ln in tb["links"]:
                    hash_by_name.setdefault(norm(ln["name"]), ln["hash"])

                for t in teams:
                    fs_hash = hash_by_name.get(norm(t["name"]))
                    tid, how = resolve(t["name"], fs_hash, team_rows, override)
                    if tid == 0:
                        unresolved.append(t["name"])
                        logger.warning(
                            f"  [未解析] {t['name']} (group={group_name}, hash={fs_hash})"
                        )
                        continue
                    # 取 api_football 规范队名 + 徽标 URL;
                    # teams/standings 里都没有该队时, 用 fixtures 里的队名/徽标兜底
                    row = next((r for r in team_rows if r["id"] == tid), None)
                    ex = team_extra.get(tid, {})
                    team_name = (row["name"] if row else "") or ex.get("name") or t["name"]
                    team_logo = (row["logo"] if row else "") or ex.get("logo") or ""
                    # 持久化 hash->id 以便审阅/覆盖
                    if fs_hash:
                        override[fs_hash] = tid
                    if VERBOSE:
                        logger.info(
                            f"  {t['rank']:>2}. {t['name']:<24} -> id={tid:<6} ({how}) "
                            f"name={team_name} logo={'Y' if team_logo else 'N'} hash={fs_hash}"
                        )
                    written += 1

                    if dry_run:
                        continue

                    # 以 (league_id, season, team_id) 为主键刷新
                    existing = (
                        db.query(Standing)
                        .filter(
                            Standing.league_id == league_id,
                            Standing.season == season,
                            Standing.team_id == tid,
                        )
                        .first()
                    )
                    if existing:
                        _apply(existing, t, team_name, team_logo, group_name)
                    else:
                        s = Standing(
                            league_id=league_id, season=season,
                            group_name=group_name, team_id=tid,
                            team_name=team_name,
                        )
                        _apply(s, t, team_name, team_logo, group_name)
                        db.add(s)

        if not dry_run and season is not None:
            db.commit()
            logger.info(
                f"已写入/更新 {written} 条 standings (league={league_id}, season={season})"
            )
        save_override(override)
        if unresolved:
            logger.warning(
                f"有 {len(unresolved)} 支队未解析, 请手动填入 {OVERRIDE_FILE.name}: {unresolved}"
            )
        return True
    finally:
        if own_db:
            db.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    lid = int(args[0]) if args and args[0].isdigit() else 113
    dry = "--dry-run" in args
    VERBOSE = "--verbose" in args or "-v" in args
    run(lid, dry_run=dry)
