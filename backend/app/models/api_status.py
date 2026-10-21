"""外部数据源（如 api-football）的额度状态。

设计要点
--------
* 用数据库持久化 ``quota_exhausted``，原先进程内 ``_daily_quota_exhausted`` 的
  内存闩锁只对当前进程有效。改 DB 后，调度进程、手动工具脚本、批量预测脚本
  多个进程看到同一份状态。
* ``exhausted_date`` 跟随标记耗尽时的日期（**UTC 自然日**，与 API-Football
  实际重置节奏一致），用来在跨天后自动复位；这样不必依赖 cron 定时清表，
  重启后也立刻能识别"今天额度是新的一天"，且不会因为 server 本地时区漂移
  在 UTC 跨日前提早放行。
"""
from sqlalchemy import Column, String, Boolean, Date, DateTime, Text
from datetime import datetime

from app.db.base import Base


# 主键常量。脚本里统一从这里取，避免散落的字符串拼写错误。
PROVIDER_API_FOOTBALL = "api-football"


class ApiStatus(Base):
    __tablename__ = "api_status"

    provider = Column(
        String(64), primary_key=True, comment="数据源标识 (主键)",
    )
    quota_exhausted = Column(
        Boolean, nullable=False, default=False,
        comment="当日额度是否已耗尽",
    )
    exhausted_date = Column(
        Date, nullable=True,
        comment="标记耗尽时的 UTC 自然日 (与 API-Football 重置节奏一致)，用于次日自动复位",
    )
    exhausted_at = Column(
        DateTime, nullable=True,
        comment="标记耗尽的本地时间",
    )
    last_check_at = Column(
        DateTime, nullable=True,
        comment="最近一次被读取的时间",
    )
    last_error = Column(
        Text, nullable=True,
        comment="最近一次标记耗尽时的响应摘要 (去 key)",
    )
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")
