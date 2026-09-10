"""
从 Flashscore 的淘汰赛/抽签页 (/draw/) 抓取对阵图(bracket)。

与 fetch_flashscore_standings.py 的区别
--------------------------------------
* 积分榜页 (/standings/overall/) 有表格 + 球队链接(可拿到 hash);
* 对阵页 (/draw/) **没有** /team/ 链接, 只有队名 + 队徽 + 各回合比分,
  因此这里只取展示所需信息, 不产出 hash。队名 -> api_football team_id 的
  转换交由调用方(standing_service)用「本联赛积分榜候选集」完成, 以避免
  诸如 "Barcelona SC" 被误匹配成西甲 "Barcelona" 的问题。

用法
----
    python tools/fetch_flashscore_draw.py [league_id] [--json]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from playwright.sync_api import sync_playwright

# league_id -> Flashscore 淘汰赛对阵页 URL (末尾必须是 /draw/)
DRAW_URLS = {
    13: "https://www.flashscore.com/football/south-america/copa-libertadores/standings/SlYIvtWi/draw/",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 页面 JS: 把 .draw__round -> .bracket 结构抽成纯数据
_EXTRACT_JS = """
() => [...document.querySelectorAll('div.draw__round')].map(round => {
    const labelEl = round.querySelector('.draw__label');
    const label = labelEl ? (labelEl.innerText || '').trim() : '';

    const readSide = (bracket, side) => {
        const row = bracket.querySelector('.bracket__participantRow--' + side);
        if (!row) return null;
        const img = row.querySelector('img');
        const nameEl = row.querySelector('span[data-testid="wcl-scores-simple-text-01"]');
        const name = (nameEl ? (nameEl.innerText || '').trim() : '')
                     || (img ? (img.getAttribute('alt') || '') : '');
        if (!name) return null;
        return {
            name: name,
            logo: img ? (img.getAttribute('src') || '') : '',
            // Flashscore 用加粗标出晋级方
            winner: nameEl ? /wcl-bold/.test(nameEl.className || '') : false,
        };
    };

    const readScores = (bracket, side) => {
        const res = bracket.querySelector('.bracket__result--' + side);
        if (!res) return [];
        return [...res.querySelectorAll('.bracket__score')].map(span => {
            const sup = span.querySelector('sup');
            const pen = sup ? (sup.innerText || '').replace(/[^0-9]/g, '') : '';
            const goals = (span.innerText || '').replace(/\\(\\d+\\)/g, '').trim();
            return {goals: goals, pen: pen || null};
        });
    };

    const matches = [];
    [...round.querySelectorAll('div.bracket')].forEach(b => {
        const home = readSide(b, 'home');
        const away = readSide(b, 'away');
        if (!home && !away) return;   // 未抽签/空位
        matches.push({
            home: home, away: away,
            home_scores: readScores(b, 'home'),
            away_scores: readScores(b, 'away'),
        });
    });

    return {name: label, matches: matches};
})
"""


def scrape_draw(url: str):
    """打开对阵页并返回轮次数据。失败返回 None。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_UA, locale="en-US")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # 隐私/同意弹窗
            for sel in ["text=AGREE", "text=I ACCEPT", "text=Accept", "text=OK"]:
                try:
                    page.click(sel, timeout=3000)
                    break
                except Exception:
                    pass
            page.wait_for_selector("div.draw__round", timeout=30000)
            page.wait_for_timeout(2000)  # 等队徽与比分渲染
            rounds = page.evaluate(_EXTRACT_JS)
        except Exception as e:
            logger.error(f"抓取对阵页失败: {type(e).__name__}: {e}")
            return None
        finally:
            browser.close()

    rounds = [r for r in (rounds or []) if r.get("matches")]
    if not rounds:
        logger.warning(f"对阵页未解析到任何轮次: {url}")
        return None
    return {"url": url, "rounds": rounds}


def get_draw(league_id: int):
    url = DRAW_URLS.get(league_id)
    if not url:
        logger.error(f"未配置 league_id={league_id} 的 Flashscore 对阵页 URL")
        return None
    logger.info(f"抓取联赛 {league_id} 淘汰赛对阵: {url}")
    return scrape_draw(url)


if __name__ == "__main__":
    args = sys.argv[1:]
    lid = int(args[0]) if args and args[0].isdigit() else 13
    data = get_draw(lid)
    if data:
        if "--json" in args:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            for r in data["rounds"]:
                print(f"\n=== {r['name']} ===")
                for m in r["matches"]:
                    fmt = lambda s: "/".join(
                        f"{x['goals']}{'(' + x['pen'] + ')' if x.get('pen') else ''}"
                        for x in s
                    )
                    h = m["home"] or {"name": "?", "winner": False}
                    a = m["away"] or {"name": "?", "winner": False}
                    print(f"  {'*' if h['winner'] else ' '} {h['name']:<24} {fmt(m['home_scores']):<12}"
                          f" vs {'*' if a['winner'] else ' '} {a['name']:<24} {fmt(m['away_scores'])}")
