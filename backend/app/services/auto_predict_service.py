"""赛前自动预测：每天12:00预测当日所有未开始的比赛"""

from datetime import datetime, timedelta
from sqlalchemy import text
from loguru import logger

from app.db.session import SessionLocal


def auto_predict(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        now = datetime.now()
        # 当天北京日期：北京时间10:00为分界
        bj_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = bj_today + timedelta(hours=10, minutes=10)
        end   = start + timedelta(hours=24)

        rows = db.execute(text("""
            SELECT f.id, f.home_name, f.away_name, f.league_name
            FROM fixtures f
            JOIN leagues l ON f.league_id = l.id
            WHERE f.date >= :start AND f.date < :end
              AND f.status_short = 'NS'
              AND l.enabled = 1
              AND (
                  NOT EXISTS (SELECT 1 FROM predictions p WHERE p.fixture_id = f.id)
                  OR EXISTS (
                      SELECT 1 FROM predictions p
                      WHERE p.fixture_id = f.id
                        AND (
                            p.llm_win IS NULL OR p.llm_score IS NULL
                            OR p.llm_handicap_num IS NULL OR p.llm_handicap_team IS NULL
                            OR p.llm_handicap_pct IS NULL
                            OR p.llm_ou_line IS NULL OR p.llm_ou_type IS NULL
                            OR p.llm_ou_pct IS NULL
                        )
                  )
                  OR EXISTS (
                      SELECT 1 FROM predictions p
                      WHERE p.fixture_id = f.id
                        AND EXISTS (
                            SELECT 1 FROM fixture_lineups fl
                            WHERE fl.fixture_id = f.id
                              AND fl.created_at > p.updated_at
                        )
                  )
              )
            ORDER BY f.date
        """), {"start": start, "end": end}).fetchall()

        if not rows:
            return

        logger.info(f"自动预测: {len(rows)} 场")
        for i, r in enumerate(rows):
            try:
                from prediction.predict import predict_fixture
                logger.info(f"  [{i+1}/{len(rows)}] {r[1]} vs {r[2]} ({r[3]})")
                predict_fixture(r[0], db=db)
            except Exception as e:
                logger.warning(f"  预测失败 fixture={r[0]}: {e}")

    finally:
        if own_db:
            db.close()
