"""Persisted team Elo maintenance."""

from loguru import logger

from prediction.bayes import sync_finished_elos


def update_finished_elos(db) -> int:
    """Apply every completed, not-yet-ledgered fixture and commit atomically."""
    try:
        updated = sync_finished_elos(db)
        if updated:
            db.commit()
            logger.info("Elo 更新完成: {} 场", updated)
        return updated
    except Exception:
        db.rollback()
        raise
