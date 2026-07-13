"""诊断:检查配置/DB/该 fixture 是否已就绪,不输出任何密钥明文."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.db.session import SessionLocal
from sqlalchemy import text

print("== 配置检查 ==")
print(f"db_host={settings.db_host} db_name={settings.db_name} db_user={settings.db_user}")
print(f"api_football_key 已配置: {bool(settings.api_football_key)}")
print(f"api_football_base_url: {settings.api_football_base_url or '(空)'}")
print(f"deepseek_api_key 已配置: {bool(settings.deepseek_api_key)}")
print(f"deepseek_base_url: {settings.deepseek_base_url}")

print("\n== 数据库连接 ==")
try:
    db = SessionLocal()
    r = db.execute(text("SELECT 1")).fetchone()
    print("连接成功:", r)
except Exception as e:
    print("连接失败:", repr(e))
    sys.exit(0)

print("\n== fixture 1494202 ==")
row = db.execute(text(
    "SELECT id, home_id, away_id, league_id, season, home_name, away_name, date "
    "FROM fixtures WHERE id=:fid"
), {"fid": 1494202}).fetchone()
if row:
    d = dict(row._mapping)
    print("已存在:", d)
else:
    print("fixtures 表中不存在该 fixture")

print("\n== standings 表是否有 113 联赛数据 ==")
try:
    n = db.execute(text("SELECT COUNT(*) FROM standings WHERE league_id=113")).fetchone()[0]
    print("standings(league 113) 行数:", n)
except Exception as e:
    print("standings 查询失败:", repr(e))
db.close()
