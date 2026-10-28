"""Firecrawl 情报抓取的额度耗尽识别与 api_status 记账测试。

覆盖的不变量 (与 api-football 的日额度闩锁对齐):

1. 异常识别真值表: HTTP 402 / 额度文案 -> 耗尽; 普通 429 限流 / 瞬时故障 -> 不耗尽。
2. _note_firecrawl_quota 命中时调 mark_quota_exhausted("firecrawl", ...) 写
   api_status (测试里把 DB 打成不可用, 只验证进程内闩锁, 不写脏行)。
3. 闩锁已置位时 scrape_match_analysis 直接快速失败, 不碰 Firecrawl SDK, 且
   返回的 Markdown 带 "获取失败，忽略该数据源" 标记 (predict.py 靠它把
   club_news 记成 UNAVAILABLE)。
4. _scrape_article_pages 撞到额度异常时向上抛, 中止整个抓取流程。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from app.services import api_status_service as svc
from tools import fetch_intelligence as fetch


class _BrokenSession:
    """任何方法都抛异常, 用来模拟 DB 不可用 (与 test_api_status_service 同一招)。"""

    def __getattr__(self, name):
        def _raise(*a, **kw):
            raise RuntimeError(f"DB unavailable in test ({name})")
        return _raise


@pytest.fixture
def no_db(monkeypatch):
    """DB 不可用 + 清空进程内 quota 缓存; 结束时再清一次, 不污染其它测试。"""
    monkeypatch.setattr(svc, "SessionLocal", lambda: _BrokenSession())
    svc.clear_cache_for_tests()
    yield
    svc.clear_cache_for_tests()


def _http_error(status: int, message: str) -> requests.exceptions.HTTPError:
    """按 firecrawl v1 SDK 的形状造一个 HTTPError: 状态码/正文挂在 response 上。"""
    response = SimpleNamespace(status_code=status, text='{"error": "%s"}' % message)
    return requests.exceptions.HTTPError(
        f"Failed to scrape URL. Error: {message}", response=response,
    )


def test_quota_detection_truth_table():
    """额度信号与普通限流/瞬时故障必须分开判, 不能单看状态码。"""
    # 402 Payment Required: Firecrawl 托管套餐点数/账期耗尽的确定信号。
    assert fetch.is_firecrawl_quota_exhausted(_http_error(402, "Payment required")) is True
    # 纯文本异常 (SDK 把 HTTP 200 body 里的 error 抛成 Exception) 也要认。
    assert fetch.is_firecrawl_quota_exhausted(Exception("Insufficient credits")) is True
    assert fetch.is_firecrawl_quota_exhausted(Exception("Team quota exceeded")) is True

    # 403/429 只有带额度文案才算耗尽; 裸 429 是每分钟/反代限流, 闩锁一天就误伤了。
    assert fetch.is_firecrawl_quota_exhausted(_http_error(429, "Rate limit exceeded")) is False
    assert fetch.is_firecrawl_quota_exhausted(Exception("Search failed. Error: Rate limit exceeded")) is False
    # 但 429 明确说额度/账期问题时仍然算。
    assert fetch.is_firecrawl_quota_exhausted(_http_error(429, "Monthly quota exceeded")) is True

    # 瞬时故障不是额度问题。
    assert fetch.is_firecrawl_quota_exhausted(_http_error(500, "Internal server error")) is False
    assert fetch.is_firecrawl_quota_exhausted(TimeoutError("Connection timeout")) is False
    assert fetch.is_firecrawl_quota_exhausted(Exception("Failed to parse Firecrawl response as JSON.")) is False


def test_status_code_extraction_across_sdk_shapes():
    """状态码可能挂在异常对象上, 也可能挂在 .response 上, 两种都要认。"""
    assert fetch._exception_status_code(SimpleNamespace(status_code=402)) == 402
    assert fetch._exception_status_code(SimpleNamespace(status=403)) == 403
    assert fetch._exception_status_code(SimpleNamespace(response=SimpleNamespace(status_code=402))) == 402
    assert fetch._exception_status_code(Exception("no status here")) is None


def test_quota_reason_redacts_api_key(no_db):
    """写进 api_status.last_error 的诊断信息不得夹带 api_key。"""
    exc = _http_error(402, "Payment required for key sk-live-abc123")
    reason = fetch._firecrawl_quota_reason("search", exc, "sk-live-abc123")

    assert "sk-live-abc123" not in reason
    assert "***" in reason
    assert "firecrawl search" in reason
    assert "status=402" in reason


def test_note_quota_marks_api_status(no_db):
    """识别到额度耗尽 -> mark_quota_exhausted("firecrawl") -> is_quota_exhausted 立即为真。"""
    assert svc.is_quota_exhausted("firecrawl") is False

    marked = fetch._note_firecrawl_quota("search", _http_error(402, "Payment required"), "k")

    assert marked is True
    assert svc.is_quota_exhausted("firecrawl") is True


def test_note_quota_ignores_non_quota_errors(no_db):
    """普通限流/瞬时故障不得把 firecrawl 闩锁置上。"""
    assert fetch._note_firecrawl_quota("search", _http_error(429, "Rate limit exceeded"), "k") is False
    assert svc.is_quota_exhausted("firecrawl") is False


def test_scrape_pages_raises_and_marks_on_quota(no_db):
    """详情页抓取撞到额度耗尽时必须向上抛 (中止整个抓取), 同时记账。"""

    class _BoomApp:
        def scrape_url(self, *args, **kwargs):
            raise _http_error(402, "Payment required")

    matches = [("web", {"url": "https://example.com/a", "title": "A vs B preview"})]

    with pytest.raises(requests.exceptions.HTTPError):
        fetch._scrape_article_pages(
            _BoomApp(),
            matches,
            limit=1,
            scrape_options=None,
            expected_year=2026,
            api_key="k",
        )

    assert svc.is_quota_exhausted("firecrawl") is True


def test_scrape_match_analysis_fast_fails_when_latched(monkeypatch, no_db):
    """闩锁已置位时, 不再发起任何 search/scrape, 直接返回带失败标记的 Markdown。"""
    # _firecrawl_quota_latched 内部是"调用时才 from ... import", 因此打桩
    # service 模块属性即可生效。
    monkeypatch.setattr(svc, "is_quota_exhausted", lambda provider=None: True)

    result = fetch.scrape_match_analysis(
        home_team="Dinamo Zagreb",
        away_team="Viking",
        competition="Champions League",
        year=2026,
        api_key="k",
    )

    assert "获取失败，忽略该数据源" in result
    assert "firecrawl" in result
# ── refresh_firecrawl_quota_status: 主动对账并写库 ──────────────

def _install_fake_firecrawl(monkeypatch, client_cls):
    """把假 firecrawl 包塞进 sys.modules, 让函数里的懒导入拿到假客户端。"""
    import sys
    import types

    module = types.ModuleType("firecrawl")
    module.V1FirecrawlApp = client_cls
    monkeypatch.setitem(sys.modules, "firecrawl", module)


class _CreditUsageApp:
    """按给定剩余点数/异常返回 credit-usage 的假客户端。"""

    def __init__(self, remaining=0, plan=1000, exc=None, **kwargs):
        self.remaining = remaining
        self.plan = plan
        self.exc = exc

    def get_credit_usage(self):
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(
            success=True,
            data=SimpleNamespace(
                remaining_credits=self.remaining,
                plan_credits=self.plan,
                billing_period_start="2026-10-23",
                billing_period_end="2026-11-22",
            ),
        )


def _probe(monkeypatch, client_cls):
    _install_fake_firecrawl(monkeypatch, client_cls)
    return fetch.refresh_firecrawl_quota_status(
        api_key="sk-test", firecrawl_base_url="http://127.0.0.1:3002",
    )


def test_probe_marks_exhausted_when_no_credits(monkeypatch, no_db):
    """剩余点数为 0 -> 写库标记耗尽, 之后 is_quota_exhausted 立即为真。"""
    assert svc.is_quota_exhausted("firecrawl") is False

    result = _probe(monkeypatch, _CreditUsageApp)

    assert result["status"] == "exhausted"
    assert result["remaining_credits"] == 0
    assert result["plan_credits"] == 1000
    assert result["billing_period_end"] == "2026-11-22"
    assert svc.is_quota_exhausted("firecrawl") is True


def test_probe_resets_latch_when_credits_available(monkeypatch, no_db):
    """还有余额 -> 清掉可能已过期的闩锁 (升级套餐/额度恢复后不必等重置日)。"""
    svc.mark_quota_exhausted("firecrawl", "stale latch from earlier")
    assert svc.is_quota_exhausted("firecrawl") is True

    result = _probe(monkeypatch, lambda **kw: _CreditUsageApp(remaining=763, **kw))

    assert result["status"] == "available"
    assert result["remaining_credits"] == 763
    assert svc.is_quota_exhausted("firecrawl") is False


def test_probe_keeps_latch_when_endpoint_unavailable(monkeypatch, no_db):
    """自托管实例没有 credit-usage 端点 -> 不动�锁锁, 返回 unknown 让被动识别兜底。"""
    svc.mark_quota_exhausted("firecrawl", "latched by passive detection")
    assert svc.is_quota_exhausted("firecrawl") is True

    result = _probe(
        monkeypatch,
        lambda **kw: _CreditUsageApp(
            exc=Exception("Failed to get credit usage. Error: Not Found"), **kw
        ),
    )

    assert result["status"] == "unknown"
    assert "Not Found" in result["error"]
    # 已有的闩锁必须原样保留, 不能因为查不动就放行
    assert svc.is_quota_exhausted("firecrawl") is True


def test_probe_unknown_when_field_missing(monkeypatch, no_db):
    """响应缺 remaining_credits 时按 unknown 处理, 不猜。"""

    class _NoFieldApp:
        def __init__(self, **kwargs):
            pass

        def get_credit_usage(self):
            return SimpleNamespace(success=True, data=SimpleNamespace(plan_credits=1000))

    result = _probe(monkeypatch, _NoFieldApp)

    assert result["status"] == "unknown"
    assert result["remaining_credits"] is None
