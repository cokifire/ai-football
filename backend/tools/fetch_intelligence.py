import json
import os
from firecrawl import Firecrawl


FIRECRAWL_API_KEY = "fc-242d2ac59c7e4803a988747c99d8195d"
# 1. 初始化 Firecrawl
app = Firecrawl(api_key=FIRECRAWL_API_KEY)


def scrape_match_analysis():
    query = build_query(
        home_team="Dinamo Zagreb",
        away_team="Viking",
        competition="Champions League",
        year=2026,
    )
    # 2. 调用 /v2/search 一站式搜索并直接提取网页正文 Markdown
    search_results = app.search(
        query=query,
        sources=["news", "web"],
        limit=2,
        scrape_options={
            "formats": ["markdown"]
        },
    )

    return search_results


# 构造精确关键词（定向检索 ESPN 等权威体育网）
def build_query(
    *,
    home_team: str,
    away_team: str,
    competition: str,
    year: int | str,
    site: str = "espn.com",
) -> str:
    """Build a `site:<domain>` search query from match parameters.

    Args:
        home_team: Name of the home team, e.g. "Dinamo Zagreb".
        away_team: Name of the away team, e.g. "Viking".
        competition: Competition name, e.g. "Champions League".
        year: Season / year, e.g. 2026.
        site: Restrict results to this domain (default "espn.com").

    Returns:
        A search-engine query string.
    """
    parts = [
        f"site:{site}",
        home_team,
        away_team,
        competition,
        str(year),
    ]
    return " ".join(parts)


def _to_jsonable(obj):
    """Convert SDK result objects (e.g. Pydantic models) to plain JSONable data."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


# 3. 运行爬取
if __name__ == "__main__":
    results = scrape_match_analysis()
    print(json.dumps(_to_jsonable(results), indent=2, ensure_ascii=False))
