"""启动时的轻量 schema 兼容处理。

predictions 等表由 raw SQL + 手动迁移维护，生产环境经常没有执行最新迁移。
这里对「代码依赖但库里可能还没有」的列做幂等补建，避免迁移未执行时读写报错。
"""

from loguru import logger
from sqlalchemy import text

from app.db.session import SessionLocal


def _ensure_column(table: str, column: str, definition: str) -> None:
    """确保 ``table.column`` 存在；不存在则按 definition 补建。

    启动阶段的兼容检查，任何异常都只记录不抛出，避免拿不到库就起不了服务。
    """
    db = SessionLocal()
    try:
        exists = db.execute(text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :tname "
            "AND COLUMN_NAME = :cname"
        ), {"tname": table, "cname": column}).scalar()
        if exists:
            return
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN {definition}"))
        db.commit()
        logger.info("已补建列 {}.{}", table, column)
    except Exception as exc:  # noqa: BLE001 - 启动阶段的兼容检查不应阻断服务
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("检查/补建 {}.{} 失败: {}", table, column, exc)
    finally:
        db.close()


def ensure_prediction_intelligence_column() -> None:
    """确保 predictions.intelligence_markdown 列存在（迁移未执行时自动补建）。"""
    _ensure_column("predictions", "intelligence_markdown",
                   "intelligence_markdown TEXT NULL COMMENT '外部比赛情报 Markdown'")


def ensure_prediction_llm_model_column() -> None:
    """确保 predictions.llm_model 列存在（迁移未执行时自动补建）。"""
    _ensure_column("predictions", "llm_model",
                   "llm_model VARCHAR(64) NULL COMMENT '生成本次预测的 LLM 模型名'")
