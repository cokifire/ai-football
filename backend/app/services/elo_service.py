"""Persisted team Elo maintenance."""

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from prediction.bayes import sync_finished_elos

# app/services/elo_service.py -> app/services -> app -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]
XG_BACKFILL_SCRIPT = "tools/backfill_xg.py"
# fixtures.date 是北京时间(naive)，月份也要按北京时间算，不能跟服务器本地时区走。
BEIJING_TZ = timezone(timedelta(hours=8))
# 无人值守任务必须有上限，否则脚本卡住时 is_running 会一直是 1，挡掉之后每天的执行。
XG_BACKFILL_TIMEOUT = 60 * 60


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


def _backfill_month_xg(year: int, month: int) -> None:
    """跑一次 tools/backfill_xg.py，补该月缺失的 Flashscore xG。

    走子进程而不是 import main()：脚本自带 argparse / SessionLocal / Playwright，
    需要在 backend/ 目录下取到 .env；子进程还能把浏览器与后端进程隔离开。
    """
    cmd = [sys.executable, XG_BACKFILL_SCRIPT,
           "--year", str(year), "--month", str(month)]
    logger.info("[xG] 开始补缺 {}-{:02d}: {}", year, month, " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, cwd=str(BACKEND_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=XG_BACKFILL_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[xG] {}-{:02d} 补缺超过 {} 秒，已终止",
                       year, month, XG_BACKFILL_TIMEOUT)
        return
    except OSError as e:
        logger.warning("[xG] {}-{:02d} 补缺脚本启动失败: {}", year, month, e)
        return

    for line in (proc.stdout or "").splitlines():
        if line.strip():
            logger.info("[xG] {}", line.rstrip())
    if proc.returncode:
        logger.warning("[xG] {}-{:02d} 补缺脚本退出码 {}", year, month, proc.returncode)
    else:
        logger.info("[xG] {}-{:02d} 补缺结束", year, month)


def run_daily_elo_update(db):
    """``elo_update`` 定时任务入口：更新 Elo 后顺带补 Flashscore xG。

    ``backfill_xg.py`` 挂在这个已有任务里，不新开调度项。xG 抓取是尽力而为：
    Flashscore 不可达 / Playwright 失败都只记日志，不能影响 Elo 落账。
    """
    updated = update_finished_elos(db)

    now = datetime.now(BEIJING_TZ)
    prev = now.replace(day=1) - timedelta(days=1)
    # 同时扫上月：跨月头几天跑时，前一天刚完赛的比赛还在上个月，只扫当月会永久漏掉。
    # 脚本自身会跳过已齐备的比赛，重复扫描只是多一次 DB 查询 + 一次浏览器启动。
    for year, month in ((prev.year, prev.month), (now.year, now.month)):
        try:
            _backfill_month_xg(year, month)
        except Exception as e:
            logger.warning("[xG] {}-{:02d} 补缺异常: {}", year, month, e)

    return updated
