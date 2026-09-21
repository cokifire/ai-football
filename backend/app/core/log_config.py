import sys
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger


# 日志文件最多保留 30 天，修改这里即可统一调整保留周期。
LOG_RETENTION_DAYS = 30
LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


def _cleanup_expired_logs() -> None:
    """启动时清理超过保留期限的按日滚动日志。"""
    if not LOG_DIR.exists():
        return

    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
    for log_file in LOG_DIR.glob("football_*.log"):
        try:
            if datetime.fromtimestamp(log_file.stat().st_mtime) < cutoff:
                log_file.unlink()
        except OSError as exc:
            # 清理失败不应阻止服务启动，记录后继续运行。
            print(f"日志清理失败: {log_file}: {exc}", file=sys.stderr)


def setup_logger():
    """配置 loguru 日志"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_expired_logs()
    logger.remove()  # 移除默认 handler

    # 控制台输出
    logger.add(
        sys.stdout,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        level="DEBUG",
        colorize=True,
    )

    # 文件输出
    logger.add(
        str(LOG_DIR / "football_{time:YYYY-MM-DD}.log"),
        rotation="00:00",
        retention=f"{LOG_RETENTION_DAYS} days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="INFO",
    )

    return logger
