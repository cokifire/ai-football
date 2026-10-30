"""一次性修复：重算已被错误写入的 score_in_top3。

背景：commit 36283a4（2026-08-28）从 predictions 的 SELECT 列表里删掉了
llm_over_under / llm_handicap 两列，导致 Top3 比分判断从 pred[5](llm_score)
错位到 pred[5](llm_ou_type)，2026-08-28 之后所有回填的 score_in_top3 恒为 0。
错位已在 prediction_result_service 修好，这里只把历史脏值重算一遍。

只走本地库，不发任何外部请求：仅处理已有终态比分的比赛。
"""

import sys

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.prediction_result_service import FINISHED, _backfill_one


def main() -> int:
    db = SessionLocal()
    updated = 0
    total = 0
    try:
        fids = [
            int(r[0])
            for r in db.execute(
                text(
                    "SELECT p.fixture_id FROM predictions p "
                    "JOIN fixtures f ON f.id = p.fixture_id "
                    "WHERE f.status_short IN :finished "
                    "AND f.fulltime_home IS NOT NULL "
                    "AND f.fulltime_away IS NOT NULL "
                    "AND IFNULL(p.llm_score, '') <> '' "
                    "ORDER BY p.match_date"
                ),
                {"finished": tuple(sorted(FINISHED))},
            ).fetchall()
        ]
        total = len(fids)
        print("待重算: %d 场" % total)
        for i, fid in enumerate(fids, 1):
            try:
                if _backfill_one(db, fid):
                    updated += 1
            except Exception as exc:
                print("  失败 fixture=%s: %s" % (fid, exc))
            if i % 100 == 0:
                db.commit()
                print("  已处理 %d/%d（更新 %d）" % (i, total, updated))
        db.commit()
    finally:
        db.close()
    print("重算完成: %d/%d" % (updated, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
