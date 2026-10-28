"""``app.services.api_status_service`` 行为测试。

覆盖以下不变量（不依赖真实的 DB 连接；测试用进程内缓存和 ``monkeypatch``）：

1. ``mark_quota_exhausted`` → ``is_quota_exhausted`` 立即返回 True，跨调用无须 DB。
2. ``reset_provider`` → 清缓存, ``is_quota_exhausted`` 再调用降级为 False（DB 不可用时
   服务退回默认未耗尽, 而不是抛异常)。
3. ``is_quota_exhausted`` 缓存命中时, 不触发 DB 写入（确保热路径 O(0)）。
4. ``get_status`` 在 DB 不可用时返回降级 dict（不会因为 DB 抖动把运维接口挂掉）。

要做什么不依赖 ``SessionLocal`` 的 DB 调用 monkeypatch？service 里 ``SessionLocal``
是从 ``app.db.session`` 引入的, 直接 ``monkeypatch.setattr(api_status_service,
'SessionLocal', ...)``。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace


import pytest

from app.services import api_status_service as svc


class _BrokenSession:
    """任何方法都抛异常, 用来模拟 DB 不可用。"""

    def __getattr__(self, name):
        def _raise(*a, **kw):
            raise RuntimeError(f"DB unavailable in test ({name})")
        return _raise


def _force_db_unavailable(monkeypatch):
    monkeypatch.setattr(svc, "SessionLocal", lambda: _BrokenSession())
    svc.clear_cache_for_tests()


def test_mark_and_read_exhausted_no_db(monkeypatch):
    """mark_quota_exhausted 设置内存缓存, 无 DB 时 is_quota_exhausted 也能立即返回 True。"""
    _force_db_unavailable(monkeypatch)
    assert svc.is_quota_exhausted("api-football") is False

    svc.mark_quota_exhausted("api-football", "rate limit daily")
    assert svc.is_quota_exhausted("api-football") is True


def test_persist_exhausted_reports_success_after_commit(monkeypatch):
    """成功 commit 后必须返回 True，不能和 DB 异常都降级成 None。"""

    class _WorkingDB:
        def __init__(self):
            self.commits = 0

        def execute(self, *args, **kwargs):
            return None

        def commit(self):
            self.commits += 1

        def close(self):
            pass

    fake = _WorkingDB()
    monkeypatch.setattr(svc, "SessionLocal", lambda: fake)

    result = svc._persist_exhausted(
        "api-football",
        "fixtures/statistics status=429",
        datetime.now(),
        date.today(),
    )

    assert result is True
    assert fake.commits == 1


def test_reset_clears_cache_and_returns_to_false(monkeypatch):
    """reset_provider 应当清内存缓存并强制下一次读取走 DB; DB 不可用时降级为 False。"""
    _force_db_unavailable(monkeypatch)

    svc.mark_quota_exhausted("api-football")
    assert svc.is_quota_exhausted("api-football") is True

    svc.reset_provider("api-football")

    # 缓存刚被清, is_quota_exhausted 会尝试走 DB, DB 不可用 -> 返回 False (降级默认值)
    assert svc.is_quota_exhausted("api-football") is False


def test_cached_read_does_not_touch_db(monkeypatch):
    """缓存命中时应直接返回值, 不调用 SessionLocal; 用计数器验证。"""
    _force_db_unavailable(monkeypatch)
    svc.mark_quota_exhausted("api-football")
    # 缓存值已就位, 此时再调 is_quota_exhausted 不应再走 DB（DB 已设为 BrokenSession
    # 任何调用都会抛异常, 因此若走到 DB 这行就会失败）。我们用单独的"坏会话计数器"
    # 而不是 lambda 来确保这一点: 把 SessionLocal 替换成"任何调用都计数器 +1"的桩。
    calls = {"n": 0}

    class _Counter:
        def __getattr__(self, name):
            calls["n"] += 1
            raise RuntimeError("never reached")

    monkeypatch.setattr(svc, "SessionLocal", lambda: _Counter())
    # 再读 5 次, 缓存命中应当不触发 SessionLocal 的任何调用
    for _ in range(5):
        assert svc.is_quota_exhausted("api-football") is True
    assert calls["n"] == 0


def test_get_status_degrades_when_db_unavailable(monkeypatch):
    """DB 不可用时 get_status 不应抛异常, 而是返回一个最小可用 dict。"""
    _force_db_unavailable(monkeypatch)
    out = svc.get_status("api-football")
    # 至少包含 provider 字段 + quota_exhausted False (默认), 不抛异常即可
    assert out["provider"] == "api-football"
    assert out["quota_exhausted"] is False


def test_normalize_aliases():
    """散落脚本里写的 'api_football' 应当被归一到 'api-football' 主键。"""
    assert svc._normalize("api_football") == "api-football"
    assert svc._normalize("api-football") == "api-football"
    assert svc._normalize("API-FOOTBALL") == "API-FOOTBALL"  # 未登记别名原样保留


def test_cross_day_reset(monkeypatch):
    """DB 里 exhausted_date 是"昨天"时, 读取应当自动复位 quota_exhausted。

    通过 monkeypatch 一个返回"昨天"日期的"伪 DB"验证这一分支. 不验证 DB 写入
    副作用, 只看返回的 exhausted 值变了。

    注: service 用 ``datetime.now(timezone.utc).date()`` 作为 ``today`` —
    与 API-Football 重置节奏一致, 不依赖 server 本地时区. 测试 stub 也按
    UTC 取"昨天"以避免本地时区漂移.
    """
    from datetime import timezone

    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)

    class _YesterdayDB:
        def __init__(self):
            self.commits = 0
            self.exec_log: list[str] = []

        def execute(self, sql, params=None):
            text = str(sql).strip().lower()
            if text.startswith("select"):
                return _FakeRow((True, yesterday))
            self.exec_log.append(text)
            return None

        def commit(self):
            self.commits += 1

        def close(self):
            pass

    class _FakeRow:
        def __init__(self, tup):
            self.tup = tup

        def fetchone(self):
            return self.tup

    fake = _YesterdayDB()

    # 用 row._mapping 也要能取, get_status 不在这路径里但保险起见:
    monkeypatch.setattr(svc, "SessionLocal", lambda: fake)
    svc.clear_cache_for_tests()

    exhausted = svc.is_quota_exhausted("api-football")
    # 自动复位跨天标志后, 应该返回 False, 并把 quota_exhausted 写回 0
    assert exhausted is False
    # 复位会触发 UPDATE — 至少一次 commit
    assert fake.commits >= 1


def test_uses_utc_not_local_for_cross_day(monkeypatch):
    """验证 ``is_quota_exhausted`` 用 UTC 决定 today, 不受 server 本地时区影响.

    把 ``svc._utc_today`` monkeypatch 成 "2026-10-22", 配合一个返回
    exhausted_date="2026-10-21" (UTC 昨天) 的伪 DB.
    触发复位后, 进一步检查 service 真的用的是我们替换进去的 UTC today,
    而不是 ``datetime.now().date()`` — 也就是说, 当系统本地 "今天" 与
    UTC "今天" 不一致时, service 跟的是 UTC.
    """
    from datetime import date as _date

    fixed_utc_today = _date(2026, 10, 22)
    monkeypatch.setattr(svc, "_utc_today", lambda: fixed_utc_today)

    class _UtcYesterdayDB:
        def execute(self, sql, params=None):
            text = str(sql).strip().lower()
            if text.startswith("select"):
                # exhausted_date 比 UTC today 早一天, 应该被视作"昨天" -> 复位
                return _FakeRow((True, _date(2026, 10, 21)))
            return None

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(svc, "SessionLocal", lambda: _UtcYesterdayDB())
    svc.clear_cache_for_tests()

    # exhausted_date (2026-10-21) < _utc_today (2026-10-22) -> 跨天复位
    assert svc.is_quota_exhausted("api-football") is False


def test_utc_today_uses_timezone_aware_now():
    """``_utc_today`` 必须与 ``datetime.now(timezone.utc).date()`` 等价.

    直接调用验证底层用的是 ``timezone.utc``, 不是 naive ``datetime.now()``.
    """
    from datetime import timezone as _tz
    utc_today = svc._utc_today()
    expected = datetime.now(_tz.utc).date()

    # 不一定能严格相等 (时分秒走完跨日), 但原则上都是同一天 (测试窗口短)
    assert utc_today == expected


def test_provider_key_constant():
    """PROVIDER_API_FOOTBALL 在 model 与 service 之间必须一致, 别顺手改了一处忘了另一处。"""
    from app.models.api_status import PROVIDER_API_FOOTBALL as from_model
    from app.models.api_status import ApiStatus
    assert from_model == "api-football"
    assert ApiStatus.__tablename__ == "api_status"

class _FixedDateRow:
    """伪装成 DB 结果行: quota_exhausted=True + 指定的 exhausted_date。"""

    def __init__(self, exhausted_date):
        self.tup = (True, exhausted_date)

    def fetchone(self):
        return self.tup


class _ExhaustedDB:
    """只认 SELECT 的伪 DB: 永远返回"已耗尽 + 给定日期", 复位 UPDATE 照单全收。"""

    def __init__(self, exhausted_date):
        self.exhausted_date = exhausted_date
        self.commits = 0

    def execute(self, sql, params=None):
        if str(sql).strip().lower().startswith("select"):
            return _FixedDateRow(self.exhausted_date)
        return None

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _read_firecrawl_with(monkeypatch, exhausted_date, today):
    """在"DB 报告某日耗尽 + UTC 今天是 today"的设定下读 firecrawl 闩锁。"""
    monkeypatch.setattr(svc, "_utc_today", lambda: today)
    monkeypatch.setattr(svc, "SessionLocal", lambda: _ExhaustedDB(exhausted_date))
    svc.clear_cache_for_tests()
    return svc.is_quota_exhausted("firecrawl")


def test_last_monthly_reset_boundary():
    """月度重置边界: 当月重置日未到取上月重置日, 已到取当月; 跨年/短月都要对。"""
    assert svc._last_monthly_reset_boundary(date(2026, 10, 20), 23) == date(2026, 9, 23)
    assert svc._last_monthly_reset_boundary(date(2026, 10, 23), 23) == date(2026, 10, 23)
    assert svc._last_monthly_reset_boundary(date(2026, 10, 24), 23) == date(2026, 10, 23)
    assert svc._last_monthly_reset_boundary(date(2026, 11, 5), 23) == date(2026, 10, 23)
    # 跨年: 1 月重置日未到时应取上一年 12 月的重置日
    assert svc._last_monthly_reset_boundary(date(2027, 1, 5), 23) == date(2026, 12, 23)
    # 重置日 >28 时收敛到月末 (2 月)
    assert svc._last_monthly_reset_boundary(date(2026, 3, 1), 31) == date(2026, 2, 28)


@pytest.mark.parametrize(
    "exhausted, today, expect_latched",
    [
        # 10-10 耗尽, 本月 23 号还没到 -> 一直闩锁, 不能跨天放行
        (date(2026, 10, 10), date(2026, 10, 11), True),
        (date(2026, 10, 10), date(2026, 10, 20), True),
        # 10-24 已过 23 号重置 -> 放行
        (date(2026, 10, 10), date(2026, 10, 24), False),
        (date(2026, 10, 10), date(2026, 11, 5), False),
        # 10-25 (上次重置后) 又耗尽 -> 11 月额度也用光了, 11-23 前保持闩锁
        (date(2026, 10, 25), date(2026, 11, 5), True),
        (date(2026, 10, 25), date(2026, 11, 22), True),
        (date(2026, 10, 25), date(2026, 11, 23), False),
        # 恰好重置当天耗尽: 这个周期就用光了, 不放行
        (date(2026, 10, 23), date(2026, 10, 23), True),
    ],
)
def test_firecrawl_monthly_latch_survives_until_reset_day(monkeypatch, exhausted, today, expect_latched):
    """firecrawl 月度配额: 闩锁必须撑到每月重置日, 而不是每天放行一次。"""
    # 机制测试与账号真实重置日解耦: 固定按 23 号重置验证日期矩阵
    # (账号实际是 9 号, 由 settings 默认值表达, 见 test_..._is_configurable)。
    monkeypatch.setattr(svc.settings, "firecrawl_quota_reset_day", 23)
    latched = _read_firecrawl_with(monkeypatch, exhausted, today)
    assert latched is expect_latched


def test_firecrawl_reset_day_is_configurable(monkeypatch):
    """重置日来自 settings.firecrawl_quota_reset_day, 换账号/换套餐不用改代码。"""
    monkeypatch.setattr(svc.settings, "firecrawl_quota_reset_day", 1)
    # 重置日=1: 10-10 耗尽, 11-02 (已过 11-01 重置) -> 放行
    assert _read_firecrawl_with(monkeypatch, date(2026, 10, 10), date(2026, 11, 2)) is False
    # 10-20 还没到 11-01 -> 仍然闩锁
    assert _read_firecrawl_with(monkeypatch, date(2026, 10, 10), date(2026, 10, 20)) is True


def test_api_football_still_resets_daily(monkeypatch):
    """回归保护: api-football 保持按 UTC 天放行, 不被月度节奏带偏。"""
    monkeypatch.setattr(svc, "_utc_today", lambda: date(2026, 10, 20))
    monkeypatch.setattr(svc, "SessionLocal", lambda: _ExhaustedDB(date(2026, 10, 10)))
    svc.clear_cache_for_tests()
    assert svc.is_quota_exhausted("api-football") is False
