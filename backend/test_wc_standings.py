import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from app.db.session import SessionLocal
from sqlalchemy import text

db = SessionLocal()
# 世界杯 league id = 1
cnt = db.execute(text("SELECT COUNT(*) FROM standings WHERE league_id=1")).scalar()
print('世界杯 standings 行数:', cnt)
rows = db.execute(text("SELECT season, COUNT(*) c FROM standings WHERE league_id=1 GROUP BY season")).fetchall()
for r in rows:
    print('  season', r[0], '->', r[1], '行')
db.close()
