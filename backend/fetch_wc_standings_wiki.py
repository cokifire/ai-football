"""
从 Wikipedia 爬取历届世界杯小组赛积分榜数据
用法: python fetch_wc_standings_wiki.py
"""

import sys
import io
import os
import re
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

import requests
from bs4 import BeautifulSoup

from app.db.session import SessionLocal
from app.models.standing import Standing
from sqlalchemy import text

LEAGUE_ID = 1

# 历届世界杯（有小组赛的）
WORLD_CUPS = [
    (2026, "https://en.wikipedia.org/wiki/2026_FIFA_World_Cup"),
    (2022, "https://en.wikipedia.org/wiki/2022_FIFA_World_Cup"),
    (2018, "https://en.wikipedia.org/wiki/2018_FIFA_World_Cup"),
    (2014, "https://en.wikipedia.org/wiki/2014_FIFA_World_Cup"),
    (2010, "https://en.wikipedia.org/wiki/2010_FIFA_World_Cup"),
    (2006, "https://en.wikipedia.org/wiki/2006_FIFA_World_Cup"),
    (2002, "https://en.wikipedia.org/wiki/2002_FIFA_World_Cup"),
    (1998, "https://en.wikipedia.org/wiki/1998_FIFA_World_Cup"),
    (1994, "https://en.wikipedia.org/wiki/1994_FIFA_World_Cup"),
    (1990, "https://en.wikipedia.org/wiki/1990_FIFA_World_Cup"),
    (1986, "https://en.wikipedia.org/wiki/1986_FIFA_World_Cup"),
    (1982, "https://en.wikipedia.org/wiki/1982_FIFA_World_Cup"),
    (1978, "https://en.wikipedia.org/wiki/1978_FIFA_World_Cup"),
    (1974, "https://en.wikipedia.org/wiki/1974_FIFA_World_Cup"),
    (1970, "https://en.wikipedia.org/wiki/1970_FIFA_World_Cup"),
    (1966, "https://en.wikipedia.org/wiki/1966_FIFA_World_Cup"),
    (1962, "https://en.wikipedia.org/wiki/1962_FIFA_World_Cup"),
    (1958, "https://en.wikipedia.org/wiki/1958_FIFA_World_Cup"),
    (1954, "https://en.wikipedia.org/wiki/1954_FIFA_World_Cup"),
    (1950, "https://en.wikipedia.org/wiki/1950_FIFA_World_Cup"),
    (1930, "https://en.wikipedia.org/wiki/1930_FIFA_World_Cup"),
    # 淘汰赛世界杯（无小组赛，跳过）
    # (1938, ...), (1934, ...)
]


def _clean_team_name(raw: str) -> str:
    """清理 Wikipedia 中的球队名称"""
    text = re.sub(r'\[[a-z0-9]+\]', '', raw, flags=re.IGNORECASE)
    text = re.sub(r'\s*\(H\)\s*$', '', text)
    text = re.sub(r'\s*\(hosts?\)\s*$', '', text, flags=re.IGNORECASE)
    text = ' '.join(text.split()).strip(' ,;')
    return text


def _parse_int(text: str) -> int | None:
    """解析整数，兼容 unicode 负号 + Wikipedia 引用标记"""
    text = text.strip()
    # 处理各种负号: \u2212, \u002D (hyphen-minus), \u2013 (en-dash)
    text = text.replace('\u2212', '-').replace('\u2013', '-').replace('+', '')
    # 去掉 Wikipedia 引用标记如 [a], [1], [note]
    text = re.sub(r'\[[^\]]*\]', '', text)
    try:
        return int(text)
    except (ValueError, TypeError):
        return None


def _is_group_table(table) -> bool:
    """判断是否为小组积分榜表格"""
    ths = [t.get_text(strip=True).lower() for t in table.find_all('th')]
    th_set = set(ths)
    return len(th_set & {'pos', 'team', 'pld', 'pts'}) >= 2


def _get_group_name(table) -> str:
    """从表格及其上下文中提取 group 名称"""
    # 1. 通过前一个 h3/h4 的 id
    gid = None
    current = table
    for _ in range(10):
        prev = current.find_previous_sibling()
        if prev is None:
            current = current.parent
            continue
        if prev.name in ['h3', 'h4', 'h5']:
            gid = prev.get('id', '')
            if gid:
                break
            txt = prev.get_text(strip=True)
            m = re.search(r'Group\s+([A-H0-9]+)', txt, re.IGNORECASE)
            if m:
                gid = "Group_" + m.group(1).upper()
                break
        current = prev

    if gid:
        m = re.match(r'Group_([A-H0-9]+)', gid, re.IGNORECASE)
        if m:
            return "Group " + m.group(1)

    # 2. 通过 caption
    cap = table.find('caption')
    if cap:
        m = re.search(r'Group\s+([A-H0-9]+)', cap.get_text(strip=True), re.IGNORECASE)
        if m:
            return "Group " + m.group(1)

    return "Unknown"


def _build_col_map(header_cells) -> dict:
    """根据表头建立字段名→列索引的映射"""
    col_map = {}
    for i, th in enumerate(header_cells):
        key = th.get_text(strip=True).lower()
        # 标准化 key
        if key in ('pos', '#'):
            col_map['rank'] = i
        elif key.startswith('team'):
            col_map['team'] = i
        elif key == 'pld' or key == 'gp' or key == 'mp':
            col_map['pld'] = i
        elif key == 'w':
            col_map['w'] = i
        elif key == 'd':
            col_map['d'] = i
        elif key == 'l':
            col_map['l'] = i
        elif key == 'gf' or key == 'f' or key == 'goals_for':
            col_map['gf'] = i
        elif key == 'ga' or key == 'a' or key == 'goals_against':
            col_map['ga'] = i
        elif key == 'gd' or key == '+/-' or key == 'diff':
            col_map['gd'] = i
        elif key == 'gr' or key == 'ga/gr' or key == 'goal_ratio' or key == 'goal_average':
            col_map['gr'] = i  # Goal Ratio (pre-1970)
        elif key == 'pts' or key == 'p' or key == 'points':
            col_map['pts'] = i
        elif key == 'qualification' or key == 'notes':
            col_map['qual'] = i
    return col_map


def _parse_one_table(table) -> list[dict]:
    """解析单个积分榜表格，返回 standings 记录列表"""
    group_name = _get_group_name(table)
    col_map = None

    rows = []
    all_rows = table.find_all('tr')
    for row in all_rows:
        cells = row.find_all(['td', 'th'])

        # 表头行: 建立列映射
        if all(c.name == 'th' for c in cells) and len(cells) >= 7:
            col_map = _build_col_map(cells)
            continue

        # 跳过无映射或无足够数据的行
        if col_map is None:
            continue
        if len(cells) < 7:
            continue

        def _get_col(name: str) -> str | None:
            idx = col_map.get(name)
            if idx is not None and idx < len(cells):
                return cells[idx].get_text(strip=True)
            return None

        # 提取排名
        rank = _parse_int(_get_col('rank') or '')
        if rank is None:
            continue

        # 提取球队名
        team_raw = _get_col('team') or ''
        team_name = _clean_team_name(team_raw)
        if not team_name or len(team_name) < 2:
            continue

        # 提取统计数据
        pld = _parse_int(_get_col('pld') or '')
        w   = _parse_int(_get_col('w') or '')
        d   = _parse_int(_get_col('d') or '')
        l   = _parse_int(_get_col('l') or '')
        gf  = _parse_int(_get_col('gf') or '')
        ga  = _parse_int(_get_col('ga') or '')
        pts = _parse_int(_get_col('pts') or '')

        # GD: 优先用表格中的 GD，其次用 GR（忽略），最后计算 GF-GA
        gd = _parse_int(_get_col('gd') or '')
        if gd is None and gf is not None and ga is not None:
            gd = gf - ga

        # Pts 可能在额外列后面，尝试扫描找数字
        if pts is None:
            for i in range(len(cells) - 1, 3, -1):
                val = _parse_int(cells[i].get_text(strip=True))
                if val is not None and 0 <= val <= 30:
                    # 确认不是 GF/GA/W/D/L (已在前面找到)
                    pts = val
                    break

        if None in (pld, w, d, l, gf, ga, pts):
            continue

        rows.append({
            "group_name": group_name,
            "rank": rank,
            "team_name": team_name,
            "points": pts,
            "goals_diff": gd or (gf - ga),
            "all_played": pld,
            "all_win": w,
            "all_draw": d,
            "all_lose": l,
            "all_goals_for": gf,
            "all_goals_against": ga,
        })

    return rows


def parse_world_cup_page(soup, year: int) -> list[dict]:
    """解析 Wikipedia 世界杯页面，提取所有小组积分榜"""
    all_standings = []

    # 策略1: 按 Group_A, Group_B... 和 Group_1, Group_2... id 导航
    group_ids = [f"Group_{c}" for c in "ABCDEFGHIJKLMNO"] + [f"Group_{d}" for d in "123456789"]
    found_any = False

    for gid in group_ids:
        h_tag = soup.find(id=gid)
        if h_tag is None:
            continue
        # 跳过 TOC 中的 li/ul 元素（只要 h2/h3/h4）
        if h_tag.name not in ['h2', 'h3', 'h4']:
            continue

        # 找到 h_tag 之后第一个积分榜 wikitable
        curr = h_tag
        table = None
        for _ in range(200):
            curr = curr.find_next()
            if curr is None:
                break
            if curr.name == 'table' and 'wikitable' in (curr.get('class') or []):
                if _is_group_table(curr):
                    table = curr
                    break

        if table is None:
            continue

        # 强制使用 id 对应的 group 名称
        group_letter = gid.split('_')[1]
        group_name_forced = f"Group {group_letter}"

        rows = _parse_one_table(table)
        if rows:
            # 覆盖 group_name
            for r in rows:
                r["group_name"] = group_name_forced
            all_standings.extend(rows)
            found_any = True

    if found_any:
        # 检查是否有 Second group stage (如 1974, 1978, 1982)
        second_tables = _find_second_group_stage_tables(soup)
        for i, tbl in enumerate(second_tables):
            gname = f"Second Group {chr(65 + i)}" if i < 26 else f"Second Group {i + 1}"
            rows = _parse_one_table(tbl)
            for r in rows:
                r["group_name"] = gname
            if rows:
                all_standings.extend(rows)
        return all_standings

    # 策略2: 回退 — 全局扫描
    all_tables = soup.find_all('table', class_='wikitable')
    group_tables = [t for t in all_tables if _is_group_table(t)]
    for table in group_tables:
        rows = _parse_one_table(table)
        if rows:
            all_standings.extend(rows)

    return all_standings


def _find_second_group_stage_tables(soup) -> list:
    """找 Second group stage / Final round 的积分榜表"""
    tables = []
    for h in soup.find_all(['h2', 'h3']):
        span = h.find('span', class_='mw-headline')
        txt = span.get_text(strip=True) if span else h.get_text(strip=True)
        if 'second group' in txt.lower() or 'second round' in txt.lower():
            curr = h
            while curr:
                curr = curr.find_next_sibling()
                if curr is None:
                    break
                if curr.name in ['h2', 'h3']:
                    sp2 = curr.find('span', class_='mw-headline')
                    t2 = sp2.get_text(strip=True).lower() if sp2 else curr.get_text(strip=True).lower()
                    if any(kw in t2 for kw in ['knockout', 'bracket', 'final', 'third place', 'semi']):
                        break
                if curr.name == 'table' and 'wikitable' in (curr.get('class') or []):
                    if _is_group_table(curr):
                        tables.append(curr)
                elif curr.name:
                    for t in curr.find_all('table', class_='wikitable'):
                        if _is_group_table(t):
                            tables.append(t)
            break
    return tables


def build_team_id_map(db) -> dict:
    rows = db.execute(text("SELECT id, name FROM teams")).fetchall()
    return {r[1].lower().strip(): r[0] for r in rows}


def resolve_team_id(db, team_name: str, name_map: dict, cache: dict) -> int:
    key = team_name.lower().strip()
    if key in cache:
        return cache[key]
    if key in name_map:
        cache[key] = name_map[key]
        return name_map[key]
    row = db.execute(
        text(
            "SELECT DISTINCT home_id FROM fixtures WHERE league_id = 1 AND lower(home_name) = :n "
            "UNION SELECT DISTINCT away_id FROM fixtures WHERE league_id = 1 AND lower(away_name) = :n"
        ),
        {"n": key},
    ).fetchone()
    if row and row[0]:
        cache[key] = row[0]
        return row[0]
    cache[key] = 0
    return 0


def save_to_db(db, year: int, standings: list[dict], team_id_cache: dict) -> int:
    name_map = build_team_id_map(db)
    upserted = 0

    for s in standings:
        team_id = resolve_team_id(db, s["team_name"], name_map, team_id_cache)

        existing = (
            db.query(Standing)
            .filter(
                Standing.league_id == LEAGUE_ID,
                Standing.season == year,
                Standing.group_name == s["group_name"],
                Standing.team_name == s["team_name"],
            )
            .first()
        )

        if existing:
            existing.rank = s["rank"]
            existing.points = s["points"]
            existing.goals_diff = s["goals_diff"]
            existing.team_id = team_id if team_id != 0 else existing.team_id
            existing.all_played = s["all_played"]
            existing.all_win = s["all_win"]
            existing.all_draw = s["all_draw"]
            existing.all_lose = s["all_lose"]
            existing.all_goals_for = s["all_goals_for"]
            existing.all_goals_against = s["all_goals_against"]
        else:
            db.add(Standing(
                league_id=LEAGUE_ID,
                season=year,
                group_name=s["group_name"],
                rank=s["rank"],
                team_id=team_id,
                team_name=s["team_name"],
                team_logo="",
                points=s["points"],
                goals_diff=s["goals_diff"],
                all_played=s["all_played"],
                all_win=s["all_win"],
                all_draw=s["all_draw"],
                all_lose=s["all_lose"],
                all_goals_for=s["all_goals_for"],
                all_goals_against=s["all_goals_against"],
            ))
        upserted += 1

    db.commit()
    return upserted


def main():
    print("=" * 70)
    print("Wikipedia World Cup Standings Scraper")
    print("=" * 70)

    team_id_cache = {}
    db = SessionLocal()

    try:
        total_upserted = 0
        success = 0
        fail = 0

        for year, url in WORLD_CUPS:
            print("\n[" + str(year) + "] " + url + " ...")
            try:
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
                if resp.status_code != 200:
                    print("  X HTTP " + str(resp.status_code))
                    fail += 1
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                standings = parse_world_cup_page(soup, year)

                if not standings:
                    print("  ! No standings found")
                    fail += 1
                    continue

                upserted = save_to_db(db, year, standings, team_id_cache)
                total_upserted += upserted
                success += 1
                print("  OK " + str(upserted) + " records")

                # Show group summary
                groups = {}
                for s in standings:
                    groups.setdefault(s["group_name"], [])
                    groups[s["group_name"]].append(str(s["rank"]) + "." + s["team_name"])
                for gn in sorted(groups.keys()):
                    print("     " + gn + ": " + ", ".join(groups[gn]))

            except Exception as e:
                import traceback
                print("  X " + type(e).__name__ + ": " + str(e))
                fail += 1
                continue

            time.sleep(1.5)

        # Summary
        print("\n" + "=" * 70)
        print("Done! success=" + str(success) + " fail=" + str(fail) + " total=" + str(total_upserted))
        print("=" * 70)

        print("\nDatabase standings summary:")
        rows = db.execute(
            text(
                "SELECT season, COUNT(*) AS cnt FROM standings WHERE league_id = :lid "
                "GROUP BY season ORDER BY season DESC"
            ),
            {"lid": LEAGUE_ID},
        ).fetchall()
        for r in rows:
            print("  " + str(r[0]) + ": " + str(r[1]) + " rows")

    finally:
        db.close()


if __name__ == "__main__":
    main()
