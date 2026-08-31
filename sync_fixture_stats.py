import os
import sys
BACKEND_DIR = '/home/ubuntu/ai-football/backend'
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

import requests
from app.db.session import SessionLocal
from sqlalchemy import text
from app.core.config import settings

db = SessionLocal()
API_KEY = settings.api_football_key
API_BASE = settings.api_football_base_url
headers = {"x-apisports-key": API_KEY}

def sync_stats(fixture_id: int):
    print(f"同步fixture={fixture_id}统计数据...")
    # 拉取统计
    r = requests.get(f"{API_BASE}/fixtures/statistics", params={"fixture": fixture_id}, headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"请求失败，状态码{r.status_code}，返回：{r.text[:100]}")
        return False
    data = r.json()
    if not data.get('response'):
        print(f"无返回数据，错误信息：{data.get('errors', '无')}")
        return False
    # 先删除旧数据
    db.execute(text("DELETE FROM fixture_statistics WHERE fixture_id = :fid"), {'fid': fixture_id})
    # 写入新数据
    total = 0
    for team_stat in data['response']:
        team_id = team_stat['team']['id']
        team_name = team_stat['team']['name']
        for stat in team_stat['statistics']:
            stat_type = stat['type']
            stat_value = stat['value']
            if stat_value is None:
                continue
            # 转成字符串
            if isinstance(stat_value, (int, float)):
                stat_value = str(stat_value)
            elif isinstance(stat_value, dict) and stat_value.get('total') is not None:
                stat_value = str(stat_value['total'])
            # 写入
            db.execute(text("""
            INSERT INTO fixture_statistics (fixture_id, team_id, team_name, stat_type, stat_value, created_at)
            VALUES (:fid, :tid, :tname, :stype, :svalue, NOW())
            """), {
                'fid': fixture_id,
                'tid': team_id,
                'tname': team_name,
                'stype': stat_type,
                'svalue': stat_value
            })
            total +=1
    db.commit()
    print(f"fixture={fixture_id} 同步完成，写入{total}条统计记录")
    return True

if __name__ == "__main__":
    fixture_ids = [1552137, 1552135, 1552127, 1552126, 1552125, 1552124, 1552123]
    success = 0
    for fid in fixture_ids:
        if sync_stats(fid):
            success +=1
    print(f"\n同步完成：成功{success}场，失败{len(fixture_ids)-success}场")
    db.close()
