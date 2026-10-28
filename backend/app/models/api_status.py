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
* Firecrawl 的额度语义与 API-Football 不同：托管套餐是月度配额
  （当前账号每月 1000 次调用，每月 9 号重置 — 以 credit-usage 账期为准，
  可用 settings.firecrawl_quota_reset_day 覆盖），不是 UTC 自然日。因此
  quota_exhausted 的自动复位按 provider 的重置节奏判断（见
  api_status_service._quota_reset_since）：API-Football 跨 UTC 天放行，
  Firecrawl 要撑到当月重置日才放行，否则每天放行一次只会反复重试到再次
  耗尽。自托管实例没有月度配额时，可用控制面板的 reset 接口手动复位。
"""
from sqlalchemy import Column, String, Boolean, Date, DateTime, Text
from datetime import datetime

from app.db.base import Base


# 主键常量。脚本里统一从这里取，避免散落的字符串拼写错误。
PROVIDER_API_FOOTBALL = "api-football"
# 外部比赛情报抓取 (Firecrawl search/scrape) 的额度状态主键。
PROVIDER_FIRECRAWL = "firecrawl"


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
