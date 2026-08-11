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
import re
import sys
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
}

OVERRIDE_FILE = Path(__file__).resolve().parent / "flashscore_team_map.json"

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
    parts = norm(s).split()
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
def fetch_blob(url: str):
    """用真实浏览器打开页面, 返回 (积分榜文本, [球队 hash 列表])。"""
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
        # 处理隐私/同意弹窗
        for sel in ["text=AGREE", "text=I ACCEPT", "text=Accept", "text=OK"]:
            try:
                page.click(sel, timeout=3000)
                break
            except Exception:
                pass
        # 淘汰赛/抽签类页面(如欧冠)会重定向到 /draw/ 而没有积分榜表格, 直接放弃
        if "/draw/" in page.url:
            logger.warning(f"页面重定向到抽签视图, 无积分榜表格: {page.url}")
            browser.close()
            return "", []
        # 等待积分榜文本块渲染
        page.wait_for_function(
            "() => { const els=[...document.querySelectorAll('div,section')];"
            " return els.some(e=>{const t=e.innerText||''; return t.includes('TEAM')&&t.includes('PTS');}); }",
            timeout=30000,
        )
        data = page.evaluate(
            """() => {
                const els=[...document.querySelectorAll('div,section')];
                const el = els.find(e=>{const t=e.innerText||'';
                    return t.includes('TEAM')&&t.includes('PTS')&&e.querySelectorAll('a[href*="/team/"]').length>=10;});
                if(!el) return {blob:'', links:[]};
                const links=[...el.querySelectorAll('a[href*="/team/"]')].map(a=>({
                    name: a.textContent.trim(),
                    hash: a.getAttribute('href').split('/').filter(Boolean).pop(),
                }));
                return {blob: el.innerText, links};
            }"""
        )
        browser.close()
    return data["blob"], data["links"]


def parse_standings(blob: str):
    """把积分榜文本解析为队伍字典列表。每行 15 个字段:
    rank. / name / MP / W / D / L / GF:GA / GD / PTS / sep / F1..F5。"""
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
        played = int(lines[i + 2])
        won = int(lines[i + 3])
        draw = int(lines[i + 4])
        lost = int(lines[i + 5])
        gf, ga = map(int, lines[i + 6].split(":"))
        gd = int(lines[i + 7])
        points = int(lines[i + 8])
        j = i + 10
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


def run(league_id: int, db=None, dry_run: bool = False):
    """抓取并写入某联赛积分榜。

    db: 传入外部 Session 时复用它(由调用方负责最终 commit/close);
        为 None 时自行创建 SessionLocal 并提交/关闭。
    """
    url = URLS.get(league_id)
    if not url:
        logger.error(f"未配置 league_id={league_id} 的 Flashscore URL")
        return

    logger.info(f"抓取联赛 {league_id} 积分榜: {url}")
    blob, links = fetch_blob(url)
    if not blob:
        logger.error("未能抓取到积分榜文本 (可能被反爬拦截)")
        return

    season = detect_season(blob)
    teams = parse_standings(blob)
    logger.info(f"解析到 {len(teams)} 支球队, 赛季={season}")

    # 用队名(归一化)对齐 Flashscore hash, 避免链接顺序与文本行错位
    hash_by_name = {}
    for ln in links:
        hash_by_name.setdefault(norm(ln["name"]), ln["hash"])

    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        team_rows = load_teams(db)
        override = load_override()
        # group_name 用联赛名称(与 API-Football 来源一致, 如 "Allsvenskan"),
        # 而不是写死的 "overall"
        league_name = (
            db.query(League.name)
            .filter(League.id == league_id)
            .scalar()
            or f"league_{league_id}"
        )
        unresolved = []

        for t in teams:
            fs_hash = hash_by_name.get(norm(t["name"]))
            tid, how = resolve(t["name"], fs_hash, team_rows, override)
            if tid == 0:
                unresolved.append(t["name"])
                logger.warning(f"  [未解析] {t['name']} (Flashscore hash={fs_hash})")
                continue
            # 取 api_football 规范队名 + 徽标 URL
            row = next((r for r in team_rows if r["id"] == tid), None)
            team_name = row["name"] if row else t["name"]
            team_logo = row["logo"] if row else ""
            # 持久化 hash->id 以便审阅/覆盖
            if fs_hash:
                override[fs_hash] = tid
            logger.info(f"  {t['rank']:>2}. {t['name']:<18} -> id={tid:<5} ({how}) PTS={t['points']}")

            if dry_run:
                continue

            # 以 (league_id, season, team_id) 为主键刷新, 复用已有 group_name 避免重复
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
                _apply(existing, t, team_name, team_logo, league_name)
            else:
                s = Standing(
                    league_id=league_id, season=season,
                    group_name=league_name, team_id=tid,
                    team_name=team_name,
                )
                _apply(s, t, team_name, team_logo, league_name)
                db.add(s)

        if not dry_run:
            db.commit()
            logger.info(f"已写入/更新 {len(teams) - len(unresolved)} 条 standings (league={league_id}, season={season})")
        save_override(override)
        if unresolved:
            logger.warning(f"有 {len(unresolved)} 支队未解析, 请手动填入 {OVERRIDE_FILE.name}: {unresolved}")
    finally:
        if own_db:
            db.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    lid = int(args[0]) if args and args[0].isdigit() else 113
    dry = "--dry-run" in args
    run(lid, dry_run=dry)
