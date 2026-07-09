"""
API-Football 直连测试脚本
用法: python test_api.py
"""

import sys
import io
# 强制 stdout 使用 UTF-8 避免 Windows GBK 错误
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import httpx
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("API_FOOTBALL_KEY", "")
BASE_URL = os.getenv("API_FOOTBALL_BASE_URL", "https://v3.football.api-sports.io")

print("=" * 60)
print(f"BASE_URL: {BASE_URL}")
print(f"KEY: {API_KEY[:8]}...{API_KEY[-4:] if len(API_KEY) > 12 else '(short)'}")
print(f"KEY 长度: {len(API_KEY)}")
print("=" * 60)

def test_endpoint(endpoint: str, params: dict, label: str):
    url = f"{BASE_URL}/{endpoint}"
    headers = {"x-apisports-key": API_KEY}
    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=30.0)
        status = resp.status_code
        remaining = resp.headers.get("x-ratelimit-requests-remaining", "N/A")
        limit = resp.headers.get("x-ratelimit-requests-limit", "N/A")
        print(f"\n[{label}] {url}?{'&'.join(f'{k}={v}' for k,v in params.items())}")
        print(f"  HTTP {status}  |  RateLimit: {remaining}/{limit}")
        
        if status != 200:
            print(f"  ❌ 非 200 响应: {resp.text[:500]}")
            return
        
        data = resp.json()
        errors = data.get("errors", [])
        if errors:
            print(f"  ⚠️  API 错误: {errors}")
            return
        
        items = data.get("response", [])
        results = data.get("results", 0)
        print(f"  ✅ results={results}, items={len(items)}")
        
        if items:
            # 展示第1条数据的关键字段
            first = items[0]
            fixture = first.get("fixture", {})
            league = first.get("league", {})
            teams = first.get("teams", {})
            print(f"  📋 示例: [{fixture.get('id')}] {teams.get('home',{}).get('name','?')} vs {teams.get('away',{}).get('name','?')}")
            print(f"      联赛: {league.get('name','?')}  |  赛季: {league.get('season','?')}")
            print(f"      日期: {fixture.get('date','?')}  |  状态: {fixture.get('status',{}).get('short','?')}")
            # 打印完整 keys
            print(f"      响应字段: {list(first.keys())}")
        
        # 检查 paging
        paging = data.get("paging", {})
        if paging:
            print(f"  📄 分页: current={paging.get('current')}, total={paging.get('total')}")
            
    except Exception as e:
        print(f"  ❌ 异常: {type(e).__name__}: {e}")

# ──── 测试1: 状态/配额 ────
print("\n" + "=" * 60)
print("1. 检查 API 状态")
print("=" * 60)
test_endpoint("status", {}, "status")

# ──── 测试2: 各联赛 fixtures ────
print("\n" + "=" * 60)
print("2. 测试联赛赛程 (league + season)")
print("=" * 60)

# 测试几个关键联赛
test_leagues = [
    (1, 2026, "World Cup"),
    (2, 2026, "UCL"),
    (2, 2025, "UCL 2025/26"),
    (39, 2026, "Premier League"),
    (39, 2025, "Premier League 2025/26"),
    (61, 2026, "Ligue 1"),
    (140, 2026, "La Liga"),
]

for lid, year, name in test_leagues:
    test_endpoint("fixtures", {"league": lid, "season": year}, f"league={lid} ({name}) season={year}")

# ──── 测试3: 按日期查询 ────
print("\n" + "=" * 60)
print("3. 测试按日期查询 (确保 API key 有效)")
print("=" * 60)
today = datetime.now().strftime("%Y-%m-%d")
test_endpoint("fixtures", {"date": today}, f"date={today}")

# ──── 测试4: 按日期查询 World Cup ────
print("\n" + "=" * 60)
print("4. 测试特定日期 + 联赛组合")
print("=" * 60)
test_endpoint("fixtures", {"date": today, "league": 1}, f"date={today} + league=1 (World Cup)")

# ──── 测试5: 检查 leagues 端点 ────
print("\n" + "=" * 60)
print("5. 测试 leagues 端点返回的当前赛季")
print("=" * 60)
test_endpoint("leagues", {"id": 1}, "league id=1")
test_endpoint("leagues", {"id": 39}, "league id=39 (PL)")
test_endpoint("leagues", {"id": 2}, "league id=2 (UCL)")

# ──── 测试6: 子数据端点（events, lineups等） ────
print("\n" + "=" * 60)
print("6. 测试子数据端点（使用今天的 fixture ID）")
print("=" * 60)

# 先获取一个今天的 fixture ID
today = datetime.now().strftime("%Y-%m-%d")
resp = httpx.get(
    f"{BASE_URL}/fixtures",
    headers={"x-apisports-key": API_KEY},
    params={"date": today},
    timeout=30.0,
)
if resp.status_code == 200:
    items = resp.json().get("response", [])
    if items:
        test_fixture_id = items[0]["fixture"]["id"]
        print(f"使用 fixture_id={test_fixture_id} 测试子数据端点")
        test_endpoint("fixtures/events", {"fixture": test_fixture_id}, "events")
        test_endpoint("fixtures/lineups", {"fixture": test_fixture_id}, "lineups")
        test_endpoint("fixtures/statistics", {"fixture": test_fixture_id}, "statistics")
        test_endpoint("fixtures/players", {"fixture": test_fixture_id}, "players")
    else:
        print("没有找到今天的 fixtures")
else:
    print(f"获取今天 fixtures 失败: {resp.status_code}")

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)

# ──── 测试7: from+to 日期范围 ────
print("\n" + "=" * 60)
print("7. 测试 from+to 日期范围（尝试减少 API 调用）")
print("=" * 60)
today = datetime.now()
week_ago = (today - timedelta(days=7)).strftime("%Y-%m-%d")
next_week = (today + timedelta(days=7)).strftime("%Y-%m-%d")
today_str = today.strftime("%Y-%m-%d")

test_endpoint("fixtures", {"from": today_str, "to": today_str}, f"from={today_str} to={today_str} (1 day)")
test_endpoint("fixtures", {"from": week_ago, "to": today_str}, f"from={week_ago} to={today_str} (7 days)")
test_endpoint("fixtures", {"from": week_ago, "to": next_week}, f"from={week_ago} to={next_week} (14 days)")

print("\n" + "=" * 60)
print("所有测试完成")
print("=" * 60)
