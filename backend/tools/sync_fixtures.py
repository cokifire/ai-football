"""补完赛子数据，断点续跑，抗网络波动
    免费的api-football api 不能获取当前日期前两天的比赛数据，包括比赛 fixture_id
"""
from datetime import datetime
import time
from app.db.session import SessionLocal
from app.models.fixture import Fixture
from app.services.fixture_service import sync_completed_sub_data

def count_remaining():
    """独立 session 查询，避免长连接超时"""
    db = SessionLocal()
    try:
        finished = {"FT", "AET", "PEN", "AWD", "WO"}
        return db.query(Fixture).filter(
            Fixture.status_short.in_(finished),
            Fixture.sub_data_synced == False,
        ).count()
    finally:
        db.close()

started = datetime.now()

# 初始状态
finished = {"FT", "AET", "PEN", "AWD", "WO"}
db = SessionLocal()
total = db.query(Fixture).filter(Fixture.status_short.in_(finished)).count()
done = db.query(Fixture).filter(Fixture.status_short.in_(finished), Fixture.sub_data_synced == True).count()
db.close()
print(f"[{started.strftime('%H:%M:%S')}] 完赛 {total} 场, 已完成 {done}, 待处理 {total - done}")

round_num = 0
fail_count = 0

while True:
    try:
        remaining = count_remaining()
        if remaining == 0:
            break

        round_num += 1
        elapsed = (datetime.now() - started).total_seconds() / 60
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 第 {round_num} 轮: 待处理 {remaining} 场 (已运行 {elapsed:.1f} 分钟)")

        db = SessionLocal()
        try:
            sync_completed_sub_data(db)
            fail_count = 0
        finally:
            db.close()

    except Exception as e:
        fail_count += 1
        print(f"  错误 (连续{fail_count}次): {e}")
        time.sleep(10)
        if fail_count > 10:
            print("  连续失败超过10次，退出")
            break

elapsed = (datetime.now() - started).total_seconds() / 60
print(f"[{datetime.now().strftime('%H:%M:%S')}] 完成! 总耗时 {elapsed:.1f} 分钟")
