"""API-Football 共享限流器的并发与退避行为测试。

用缩短的时间窗（4 次 / 2 秒）代替真实的 10 次 / 60 秒，让并发用例在秒级跑完；
断言的是"任意窗口内不超过服务端额度"这条不变量，与具体取值无关。
"""
import threading
import time

import pytest

from app.core import api_football as af


@pytest.fixture
def fast_limiter(monkeypatch):
    """把窗口缩到 4 次/2 秒（有效额度 3，间隔 0.667s），并换成全新实例。"""
    monkeypatch.setattr(af, "API_RATE_LIMIT", 4)
    monkeypatch.setattr(af, "API_RATE_WINDOW", 2.0)
    monkeypatch.setattr(af, "EFFECTIVE_RATE_LIMIT", 3)
    monkeypatch.setattr(af, "MIN_REQUEST_INTERVAL", 2.0 / 3)
    return af._ApiRateLimiter()


def test_rate_limit_backoff_base_uses_min_interval(monkeypatch):
    """无 Retry-After 时，429 退避基数取 MIN_REQUEST_INTERVAL（约 6.7s），上限 10s。

    常规节流与退避共用同一个基数，避免两者口径不一致。
    """
    monkeypatch.setattr(af, "MIN_REQUEST_INTERVAL", 6.667)
    monkeypatch.setattr(af, "RATE_LIMIT_MAX_BACKOFF", 10.0)
    assert af._rate_limit_wait_seconds(None, 0) == pytest.approx(6.667)
    # 6.667 * 2 = 13.3 被上限 10 截断
    assert af._rate_limit_wait_seconds(None, 1) == pytest.approx(10.0)
    assert af._rate_limit_wait_seconds(None, 2) == pytest.approx(10.0)


def test_consecutive_acquires_respect_min_interval(fast_limiter):
    """从第 2 个请求起，相邻两次真实发出时刻的间隔不小于节流间隔。

    这就是"均匀节流从启动后第 2 个请求就生效"的行为约定：第 1 个请求
    立即放行，之后的每个请求都要排队等间隔。
    """
    issued = []
    for _ in range(3):
        fast_limiter.acquire()
        issued.append(time.monotonic())

    assert len(issued) == 3
    gaps = [issued[i + 1] - issued[i] for i in range(len(issued) - 1)]
    for g in gaps:
        assert g >= 2.0 / 3 - 0.05, f"相邻请求间隔 {g:.3f}s 低于节流间隔 0.667s"


def test_concurrent_acquire_never_exceeds_server_limit(fast_limiter):
    """多线程争用时，任意 2s 窗口内不得超过服务端额度 4。

    这是整套限流修复的核心不变量：predict 路径与赛程同步路径共用同一个
    限流器，两边叠加也不能把服务端推过 10 次/分钟。
    """
    issues = []
    lock = threading.Lock()

    def worker(n):
        for _ in range(n):
            fast_limiter.acquire()
            with lock:
                issues.append(time.monotonic())

    threads = [threading.Thread(target=worker, args=(6,)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(issues) == 18
    worst = max(
        sum(1 for x in issues if start <= x < start + 2.0)
        for start in issues
    )
    assert worst <= 4, f"任意 2s 窗口内出现 {worst} 次请求，超过服务端额度 4"


def test_penalize_only_moves_window_forward(fast_limiter):
    """penalize 只能把窗口往后推：短于既有间隔的惩罚不应前移窗口。"""
    fast_limiter.acquire()
    before = fast_limiter._next_slot

    # 0 秒惩罚不应改动窗口
    fast_limiter.penalize(0.0)
    assert fast_limiter._next_slot == pytest.approx(before)

    # 短于既有间隔（0.667s）的惩罚同样不应把窗口往前挪
    fast_limiter.penalize(0.5)
    assert fast_limiter._next_slot == pytest.approx(before)

    # 长于既有间隔的惩罚才真正后移
    now = time.monotonic()
    fast_limiter.penalize(2.0)
    assert fast_limiter._next_slot >= now + 2.0 - 1e-6
    assert fast_limiter._next_slot > before


def test_wait_for_drain_empties_window(fast_limiter):
    """drain 之后窗口必须清空，下一个任务才能从干净窗口开始。"""
    for _ in range(4):
        fast_limiter.acquire()

    slept = fast_limiter.wait_for_drain(keep=0, max_wait=5.0)
    assert slept > 0
    assert len(fast_limiter._timestamps) == 0


def test_wait_for_drain_slept_is_clamped(fast_limiter):
    """返回值必须是真实休眠时长，不能超过 max_wait（否则日志会虚报）。"""
    for _ in range(4):
        fast_limiter.acquire()

    max_wait = 0.3
    t0 = time.monotonic()
    slept = fast_limiter.wait_for_drain(keep=0, max_wait=max_wait)
    elapsed = time.monotonic() - t0

    assert slept <= max_wait + 0.2, f"slept={slept:.3f} 超过 max_wait={max_wait}"
    assert elapsed <= max_wait + 0.4


def test_quota_latch_fast_fails(monkeypatch):
    """当日额度耗尽后，后续请求应立刻失败而不是继续消耗额度重试。"""
    monkeypatch.setattr(af.settings, "api_football_key", "dummy-key")
    af.reset_quota_latch()
    assert af.is_quota_exhausted() is False

    af.mark_quota_exhausted("test")
    assert af.is_quota_exhausted() is True

    with pytest.raises(af.ApiFootballQuotaExceeded):
        af.api_football_get_sync("fixtures")

    # 复位后（下一个调度 tick）恢复
    af.reset_quota_latch()
    assert af.is_quota_exhausted() is False
