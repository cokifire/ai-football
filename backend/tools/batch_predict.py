"""批量预测指定日期范围的所有比赛（已预测的跳过），完成后回填结果"""
import sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.db.session import SessionLocal
from sqlalchemy import text
from prediction.predict import predict_fixture
from app.services.prediction_result_service import backfill_results

DATES = ['2026-06-11']


def date_range(date_str):
    """北京时间日期 → DB查询范围（分界 10:10，含10:10）"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start = d + timedelta(hours=10, minutes=10)
    end   = d + timedelta(hours=34, minutes=10)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


db = SessionLocal()
try:
    total_ok = total_fail = 0

    for DATE in DATES:
        start, end = date_range(DATE)
        rows = db.execute(text("""
            SELECT f.id, f.home_name, f.away_name, f.league_name
            FROM fixtures f
            JOIN leagues l ON f.league_id = l.id
            WHERE f.date >= :start AND f.date < :end
              AND l.enabled = 1
              AND NOT EXISTS (SELECT 1 FROM predictions p WHERE p.fixture_id = f.id)
            ORDER BY f.date
        """), {"start": start, "end": end}).fetchall()

        print(f"\n=== {DATE} 待预测: {len(rows)} 场 ===")
        ok = fail = 0
        for i, r in enumerate(rows):
            try:
                predict_fixture(r[0], db=db)
                ok += 1
                print(f"  [{i+1}/{len(rows)}] OK: {r[1]} vs {r[2]} ({r[3]})")
            except Exception as e:
                fail += 1
                print(f"  [{i+1}/{len(rows)}] FAIL fixture={r[0]}: {e}")
        print(f"  小计: 成功 {ok} / 失败 {fail}")
        total_ok += ok
        total_fail += fail

    print(f"\n全部完成: 成功 {total_ok} / 失败 {total_fail}")

    print("\n提示: 比赛全部结束后再运行 python -c \"from app.services.prediction_result_service import backfill_results; backfill_results()\" 进行回填和统计")

finally:
    db.close()
