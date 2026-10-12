"""启动时的轻量 schema 兼容处理。

predictions 等表由 raw SQL + 手动迁移维护，生产环境经常没有执行最新迁移。
这里对「代码依赖但库里可能还没有」的列做幂等补建，避免迁移未执行时读写报错。
"""

from loguru import logger
from sqlalchemy import text

from app.db.session import SessionLocal


def ensure_prediction_intelligence_column() -> None:
    """确保 predictions.intelligence_markdown 列存在（迁移未执行时自动补建）。"""
    db = SessionLocal()
    try:
        exists = db.execute(text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'predictions' "
            "AND COLUMN_NAME = 'intelligence_markdown'"
        )).scalar()
        if exists:
            return
        db.execute(text(
            "ALTER TABLE predictions "
            "ADD COLUMN intelligence_markdown TEXT NULL COMMENT '外部比赛情报 Markdown'"
        ))
        db.commit()
        logger.info("已补建列 predictions.intelligence_markdown")
    except Exception as exc:  # noqa: BLE001 - 启动阶段的兼容检查不应阻断服务
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("检查/补建 predictions.intelligence_markdown 失败: {}", exc)
    finally:
        db.close()
