from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from app.core.security import ReadAuth

router = APIRouter()

# 日志目录以项目目录为基准，避免服务从不同工作目录启动时读错位置。
LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
MAX_LINES = 1000


def _log_files() -> list[Path]:
    return sorted(LOG_DIR.glob("football_*.log"), reverse=True)


def _validate_log_name(log_name: str) -> Path:
    # 只允许后端自己生成的文件名，防止通过接口读取任意路径。
    if not log_name or Path(log_name).name != log_name or not log_name.startswith("football_"):
        raise HTTPException(status_code=400, detail="无效的日志文件名")
    path = LOG_DIR / log_name
    if path.suffix != ".log" or not path.is_file():
        raise HTTPException(status_code=404, detail="日志文件不存在")
    return path


@router.get("/logs/files")
async def list_log_files(_: ReadAuth):
    """返回可查看的日志文件列表。"""
    return {
        "files": [
            {
                "name": path.name,
                "date": path.stem.removeprefix("football_"),
                "size": path.stat().st_size,
            }
            for path in _log_files()
        ]
    }


@router.get("/logs")
async def read_logs(
    _: ReadAuth,
    log_name: str | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=100),
    level: str = Query(default="ALL", pattern="^(ALL|DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
    lines: int = Query(default=300, ge=50, le=MAX_LINES),
):
    """读取指定日志文件的末尾内容，避免一次返回整个日志文件。"""
    selected = _validate_log_name(log_name) if log_name else (_log_files()[0] if _log_files() else None)
    if selected is None:
        return {"log_name": None, "lines": [], "total": 0}

    try:
        with selected.open("r", encoding="utf-8", errors="replace") as log_file:
            content = log_file.readlines()
    except OSError as exc:
        logger.warning("读取日志文件失败: {}", exc)
        raise HTTPException(status_code=500, detail="日志文件读取失败") from exc

    filtered = content
    if keyword:
        query = keyword.casefold()
        filtered = [line for line in filtered if query in line.casefold()]
    if level != "ALL":
        filtered = [line for line in filtered if f"| {level:<8}" in line or f"| {level}" in line]

    result = [line.rstrip("\n\r") for line in filtered[-lines:]]
    return {
        "log_name": selected.name,
        "lines": result,
        "total": len(filtered),
        "returned": len(result),
        "generated_at": date.today().isoformat(),
    }
