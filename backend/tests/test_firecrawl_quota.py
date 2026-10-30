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

import json
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

    with pytest.raises(fetch._FirecrawlQuotaExceeded) as excinfo:
            fetch._scrape_article_pages(
                _BoomApp(),
                matches,
                limit=1,
                expected_year=2026,
                api_key="k",
            )

    # 原始异常挂在 __cause__ 上, 外层 wrapper 靠它判断"已记账, 可切代理"
    assert isinstance(excinfo.value.__cause__, requests.exceptions.HTTPError)
    assert svc.is_quota_exhausted("firecrawl") is True


def test_scrape_match_analysis_fast_fails_when_latched(monkeypatch, no_db):
    """闩锁已置位时, 不再发起任何 search/scrape, 直接返回带失败标记的 Markdown。"""
    # 显式清掉代理配置: 否则会读到开发者 .env 里真实的 FIRECRAWL_PROXY,
    # 走代理通道发真实网络请求 (测试绝不能依赖外部配置/网络)。
    _setup_proxy(monkeypatch, base_url="", api_key="")
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
    """把假 firecrawl 包塞进 sys.modules, 让函数里的懒导入拿到假客户端。

    连 firecrawl.v1.client 一起造假 — 采集流程还会
    from firecrawl.v1.client import V1ScrapeOptions。
    """
    import sys
    import types

    class _FakeScrapeOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    module = types.ModuleType("firecrawl")
    module.V1FirecrawlApp = client_cls
    v1 = types.ModuleType("firecrawl.v1")
    client_mod = types.ModuleType("firecrawl.v1.client")
    client_mod.V1ScrapeOptions = _FakeScrapeOptions
    module.v1 = v1
    v1.client = client_mod
    monkeypatch.setitem(sys.modules, "firecrawl", module)
    monkeypatch.setitem(sys.modules, "firecrawl.v1", v1)
    monkeypatch.setitem(sys.modules, "firecrawl.v1.client", client_mod)


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
# ── 代理端点回退 (主额度耗尽 -> 代理 + 备用 key) ────────────────

_MAIN_URL = "http://127.0.0.1:3002"
_PROXY_URL = "http://proxy:3002"
_NO_RESULT = "# 外部比赛情报\n\n未获取到可用的外部情报。"

# 假客户端行为表: api_url -> {"search_exc"/"scrape_exc"}。测试间必须清理,
# 否则上一个测试给主端点设的 402 会漏到下一个测试。
_BEHAVIOR: dict[str, dict] = {}


@pytest.fixture(autouse=True)
def _clean_behavior():
    _BEHAVIOR.clear()
    yield
    _BEHAVIOR.clear()


class _RoutingApp:
    """假 Firecrawl 客户端: 按 api_url 查行为表, 记录每次构造。"""

    constructed: list[tuple[str, str]] = []

    def __init__(self, api_key=None, api_url=None):
        self.api_key = api_key
        self.api_url = api_url
        type(self).constructed.append((api_key, api_url))
        behavior = _BEHAVIOR.get(api_url, {})

    def search(self, **kwargs):
        exc = _BEHAVIOR.get(self.api_url, {}).get("search_exc")
        if exc is not None:
            raise exc
        return SimpleNamespace(web=[])

    def scrape_url(self, *args, **kwargs):
        exc = _BEHAVIOR.get(self.api_url, {}).get("scrape_exc")
        if exc is not None:
            raise exc
        return SimpleNamespace(data=SimpleNamespace(markdown=""))


def _setup_proxy(monkeypatch, *, base_url=_PROXY_URL, api_key="sk-proxy", http_proxy=""):
    """配置代理端点 + 安装假 SDK, 返回构造记录列表。

    http_proxy 非空时走 _ProxiedFirecrawlClient (真 requests, 由调用方
    monkeypatch fetch.requests.post), 不需要假 SDK 模块。
    """
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "firecrawl_proxy_base_url", base_url)
    monkeypatch.setattr(app_settings, "firecrawl_proxy_api_key", api_key)
    monkeypatch.setattr(app_settings, "firecrawl_proxy", http_proxy)
    _RoutingApp.constructed = []
    if not http_proxy:
        _install_fake_firecrawl(monkeypatch, _RoutingApp)
    return _RoutingApp.constructed


def _quota_http_error():
    return _http_error(402, "Payment required")


def test_latched_uses_proxy_without_touching_primary(monkeypatch, no_db):
    """闩锁已置位 + 配了代理: 直接走代理, 主端点一次都不碰。"""
    constructed = _setup_proxy(monkeypatch)
    svc.mark_quota_exhausted("firecrawl", "exhausted earlier")

    result = fetch.scrape_match_analysis(
        home_team="Dinamo Zagreb",
        away_team="Viking",
        competition="Champions League",
        year=2026,
        api_key="sk-main",
        firecrawl_base_url=_MAIN_URL,
    )

    assert constructed == [("sk-proxy", _PROXY_URL)]
    assert result == _NO_RESULT


def test_midflow_quota_falls_back_to_proxy_once(monkeypatch, no_db):
    """主端点抓取中途 402: 记账 + 切代理重抓一次, 不再试主端点。"""
    constructed = _setup_proxy(monkeypatch)
    _BEHAVIOR[_MAIN_URL] = {"search_exc": _quota_http_error()}

    result = fetch.scrape_match_analysis(
        home_team="Dinamo Zagreb",
        away_team="Viking",
        competition="Champions League",
        year=2026,
        api_key="sk-main",
        firecrawl_base_url=_MAIN_URL,
    )

    # 先建主客户端(撞 402), 再建代理客户端; 之后不再建 -> 没有递归
    assert constructed == [("sk-main", _MAIN_URL), ("sk-proxy", _PROXY_URL)]
    assert svc.is_quota_exhausted("firecrawl") is True
    assert result == _NO_RESULT


def test_proxy_exhaustion_returns_failure_markdown(monkeypatch, no_db):
    """代理额度也耗尽: 返回占位 Markdown, 不递归回退。"""
    constructed = _setup_proxy(monkeypatch)
    _BEHAVIOR[_MAIN_URL] = {"search_exc": _quota_http_error()}
    _BEHAVIOR[_PROXY_URL] = {"search_exc": _quota_http_error()}

    result = fetch.scrape_match_analysis(
        home_team="Dinamo Zagreb",
        away_team="Viking",
        competition="Champions League",
        year=2026,
        api_key="sk-main",
        firecrawl_base_url=_MAIN_URL,
    )

    assert constructed == [("sk-main", _MAIN_URL), ("sk-proxy", _PROXY_URL)]
    assert "获取失败，忽略该数据源" in result
    assert svc.is_quota_exhausted("firecrawl") is True


def test_no_proxy_keeps_fast_fail(monkeypatch, no_db):
    """没配代理时行为不变: 闩锁置位 -> 占位 Markdown, 不建任何客户端。"""
    constructed = _setup_proxy(monkeypatch, base_url="", api_key="")
    svc.mark_quota_exhausted("firecrawl", "exhausted earlier")

    result = fetch.scrape_match_analysis(
        home_team="Dinamo Zagreb",
        away_team="Viking",
        competition="Champions League",
        year=2026,
        api_key="sk-main",
        firecrawl_base_url=_MAIN_URL,
    )

    assert constructed == []
    assert "获取失败，忽略该数据源" in result


def test_proxy_base_url_empty_reuses_main_url(monkeypatch, no_db):
    """proxy_base_url 留空 = 沿用主 base_url, 只换备用 key (同端点换 key)。"""
    constructed = _setup_proxy(monkeypatch, base_url="", api_key="sk-backup")
    assert fetch._firecrawl_proxy_config() == {
        "http_proxy": "", "base_url": "", "api_key": "sk-backup",
    }

    svc.mark_quota_exhausted("firecrawl", "exhausted earlier")

    result = fetch.scrape_match_analysis(
        home_team="Dinamo Zagreb",
        away_team="Viking",
        competition="Champions League",
        year=2026,
        api_key="sk-main",
        firecrawl_base_url=_MAIN_URL,
    )

    assert constructed == [("sk-backup", _MAIN_URL)]
    assert result == _NO_RESULT


def test_proxy_config_disabled_by_default(monkeypatch):
    """三项全空即视为未启用代理, 不影响任何现有行为。"""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "firecrawl_proxy", "")
    monkeypatch.setattr(app_settings, "firecrawl_proxy_api_key", "")
    monkeypatch.setattr(app_settings, "firecrawl_proxy_base_url", "")
    assert fetch._firecrawl_proxy_config() is None


# ── HTTP 代理适配器 (_ProxiedFirecrawlClient) ──────────────────

class _FakeResponse:
    """最小 requests.Response 替身。"""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _capture_post(monkeypatch, response):
    """把 fetch.requests.post 换成记录调用的桩, 返回 calls 列表。"""
    calls = []

    def _fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(fetch.requests, "post", _fake_post)
    return calls


_PROXY_HTTP = "http://user:s3cret@10.0.0.9:8080"


def test_proxy_client_sends_proxies_and_backup_key(monkeypatch):
    """适配器必须带 proxies + 备用 key 打 /v1/search, 且脱敏只在日志层。"""
    calls = _capture_post(monkeypatch, _FakeResponse(200, {"success": True, "data": []}))

    client = fetch._ProxiedFirecrawlClient(
        api_key="sk-backup", base_url=_MAIN_URL, http_proxy=_PROXY_HTTP,
    )
    result = client.search(query="q", limit=3)

    assert len(calls) == 1
    assert calls[0]["url"] == f"{_MAIN_URL}/v1/search"
    assert calls[0]["json"]["query"] == "q"
    assert calls[0]["json"]["limit"] == 3
    assert calls[0]["proxies"] == {"http": _PROXY_HTTP, "https": _PROXY_HTTP}
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-backup"
    assert result["data"] == []


def test_proxy_client_scrape_url_shape(monkeypatch):
    """scrape_url 打 /v1/scrape, 返回内层 data (与 SDK 一致)。"""
    calls = _capture_post(
        monkeypatch,
        _FakeResponse(200, {"success": True, "data": {"markdown": "hello 2026"}}),
    )

    client = fetch._ProxiedFirecrawlClient(
        api_key="sk-backup", base_url=_MAIN_URL, http_proxy=_PROXY_HTTP,
    )
    result = client.scrape_url(
        "https://example.com/a",
        formats=["markdown"],
        only_main_content=True,
        remove_base64_images=True,
        block_ads=True,
    )

    assert calls[0]["url"] == f"{_MAIN_URL}/v1/scrape"
    assert calls[0]["json"]["url"] == "https://example.com/a"
    assert calls[0]["json"]["onlyMainContent"] is True
    assert result == {"markdown": "hello 2026"}


def test_proxy_client_non_200_raises_httperror_with_response(monkeypatch):
    """非 2xx 抛带 .response 的 HTTPError — 额度识别要读状态码。"""
    _capture_post(monkeypatch, _FakeResponse(402, text='{"error":"Payment required"}'))

    client = fetch._ProxiedFirecrawlClient(
        api_key="sk-backup", base_url=_MAIN_URL, http_proxy=_PROXY_HTTP,
    )
    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        client.search(query="q")

    assert excinfo.value.response.status_code == 402
    # 代理凭证不得出现在异常消息里
    assert "s3cret" not in str(excinfo.value)


def test_proxy_client_200_with_error_field_raises(monkeypatch):
    """HTTP 200 但 body 带 error: 抛普通 Exception (与 SDK 一致)。"""
    _capture_post(monkeypatch, _FakeResponse(200, {"success": False, "error": "Rate limit exceeded"}))

    client = fetch._ProxiedFirecrawlClient(
        api_key="sk-backup", base_url=_MAIN_URL, http_proxy=_PROXY_HTTP,
    )
    with pytest.raises(Exception, match="Rate limit exceeded"):
        client.search(query="q")


def test_mask_proxy_url():
    """代理 URL 脱敏: 保留主机端口, 抹掉账号密码。"""
    assert fetch._mask_proxy_url("http://user:s3cret@10.0.0.9:8080") == "http://user:***@10.0.0.9:8080"
    assert fetch._mask_proxy_url("http://10.0.0.9:8080") == "http://10.0.0.9:8080"
    assert fetch._mask_proxy_url("") == ""


def test_redact_secrets_scrubs_inline_credentials():
    """诊断信息里任何 user:pass@ 形状的凭证都要抹掉。"""
    text = "ProxyError for http://user:s3cret@10.0.0.9:8080 (key sk-live-xyz)"
    out = fetch._redact_secrets(text, "sk-live-xyz")

    assert "s3cret" not in out
    assert "sk-live-xyz" not in out
    assert "10.0.0.9:8080" in out  # 主机端口保留, 便于排查


def test_http_proxy_channel_used_end_to_end(monkeypatch, no_db):
    """配了 HTTP 代理: 闩锁置位 -> 经适配器走代理 + 备用 key 抓完流程。"""
    _setup_proxy(monkeypatch, http_proxy=_PROXY_HTTP)
    calls = _capture_post(monkeypatch, _FakeResponse(200, {"success": True, "data": []}))
    svc.mark_quota_exhausted("firecrawl", "exhausted earlier")

    result = fetch.scrape_match_analysis(
        home_team="Dinamo Zagreb",
        away_team="Viking",
        competition="Champions League",
        year=2026,
        api_key="sk-main",
        firecrawl_base_url=_MAIN_URL,
    )

    # 所有 search 都经代理通道发出, 带备用 key
    assert calls, "代理通道未发出任何请求"
    assert all(c["proxies"]["http"] == _PROXY_HTTP for c in calls)
    assert all(c["headers"]["Authorization"] == "Bearer sk-proxy" for c in calls)
    assert result == _NO_RESULT
def test_search_requests_omit_scrape_options(monkeypatch, no_db):
    """search 请求体不得带 scrapeOptions。

    带了之后 Firecrawl 会把每条结果都刮一遍并按 1 credit/页收费, 而这里只用
    URL/标题挑候选, 正文还要用 scrape_url 单独付一次 —— 等于同一批页面买两遍。
    """
    _setup_proxy(monkeypatch, http_proxy=_PROXY_HTTP)
    calls = _capture_post(monkeypatch, _FakeResponse(200, {"success": True, "data": []}))

    fetch.scrape_match_analysis(
        home_team="Villarreal",
        away_team="Espanyol",
        competition="La Liga",
        year=2026,
        api_key="sk-main",
        firecrawl_base_url=_MAIN_URL,
    )

    searches = [c for c in calls if c["url"].endswith("/v1/search")]
    assert searches, "未发出任何搜索请求"
    for call in searches:
        assert "scrapeOptions" not in (call.get("json") or {}), call["json"]
        # 结果条数仍需限制: 搜索按 2 credits/10 条计费, 条数越多越贵
        assert isinstance((call.get("json") or {}).get("limit"), int)


# ── 代理 URL 凭证编码 (# + & ( 等特殊字符) ─────────────────────

def test_normalize_proxy_url_encodes_special_characters():
    """真实代理凭证含 # + ( & ~ ! : 必须编码, 否则 netloc 被 # 截断成 'nud'。"""
    from urllib.parse import unquote

    from urllib3.util.url import parse_url

    raw = "http://nuD#Z+0-a(&8Y0X:wTs8ZV~cP!1&53#@103.114.160.158:46034"
    encoded = fetch._normalize_proxy_url(raw)

    parsed = parse_url(encoded)
    assert parsed.host == "103.114.160.158"
    assert parsed.port == 46034
    user, _, password = parsed.auth.partition(":")
    assert unquote(user) == "nuD#Z+0-a(&8Y0X"
    assert unquote(password) == "wTs8ZV~cP!1&53#"
    # 未编码时 requests 会把主机解析成 nud (日志里的 NameResolutionError)
    assert str(parse_url(raw).host).lower() == "nud"


def test_normalize_proxy_url_is_idempotent():
    """已经写了编码凭证的配置不能二次编码 (%23 -> %2523)。"""
    assert fetch._normalize_proxy_url("http://u%23ser:pw@10.0.0.9:8080") == (
        "http://u%23ser:pw@10.0.0.9:8080"
    )
    # 密码里的 @ 和 : 不参与切分: @ 取最后一个, : 取第一个
    assert fetch._normalize_proxy_url("http://u:p@ss:w0rd@10.0.0.9:8080") == (
        "http://u:p%40ss%3Aw0rd@10.0.0.9:8080"
    )


def test_normalize_proxy_url_plain_passthrough():
    """无凭证 / 空值原样返回。"""
    assert fetch._normalize_proxy_url("http://10.0.0.9:8080") == "http://10.0.0.9:8080"
    assert fetch._normalize_proxy_url("") == ""
    assert fetch._normalize_proxy_url("socks5://10.0.0.9:1080") == "socks5://10.0.0.9:1080"


def test_proxy_client_normalizes_credentials(monkeypatch):
    """适配器构造时就把凭证编码好, proxies 里不能出现裸 # 。"""
    client = fetch._ProxiedFirecrawlClient(
        api_key="sk-backup",
        base_url=_MAIN_URL,
        http_proxy="http://nuD#Z+0-a(&8Y0X:pw@103.114.160.158:46034",
    )
    assert client._proxies["https"] == (
        "http://nuD%23Z%2B0-a%28%268Y0X:pw@103.114.160.158:46034"
    )
# ── 代理通道独立记账 (主备两行, 各自的重置日) ───────────────────

def test_proxy_channel_exhaustion_marks_proxy_provider(monkeypatch, no_db):
    """代理通道撞 402: 写 firecrawl-proxy 行, 不覆盖主账号那一行。"""
    _setup_proxy(monkeypatch)
    _BEHAVIOR[_PROXY_URL] = {"search_exc": _quota_http_error()}
    svc.mark_quota_exhausted("firecrawl", "main account exhausted")

    result = fetch.scrape_match_analysis(
        home_team="Dinamo Zagreb",
        away_team="Viking",
        competition="Champions League",
        year=2026,
        api_key="sk-main",
        firecrawl_base_url=_MAIN_URL,
    )

    assert svc.is_quota_exhausted("firecrawl-proxy") is True
    # 主账号那行仍然是被标记状态 (不是被代理的诊断覆盖掉语义)
    assert svc.is_quota_exhausted("firecrawl") is True
    assert "获取失败，忽略该数据源" in result


def test_both_latched_skips_everything(monkeypatch, no_db):
    """主备都耗尽: 一个客户端都不建, 直接失败 — 不再每场打一次 402 写库。"""
    constructed = _setup_proxy(monkeypatch)
    svc.mark_quota_exhausted("firecrawl", "main exhausted")
    svc.mark_quota_exhausted("firecrawl-proxy", "proxy exhausted")

    result = fetch.scrape_match_analysis(
        home_team="Dinamo Zagreb",
        away_team="Viking",
        competition="Champions League",
        year=2026,
        api_key="sk-main",
        firecrawl_base_url=_MAIN_URL,
    )

    assert constructed == []
    assert "获取失败，忽略该数据源" in result


def test_proxy_provider_has_its_own_reset_day(monkeypatch):
    """主/备用账号重置日分开配 (主 22 号、备用 9 号)。"""
    from datetime import date

    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "firecrawl_quota_reset_day", 22)
    monkeypatch.setattr(app_settings, "firecrawl_proxy_quota_reset_day", 9)
    assert svc._reset_day_for("firecrawl") == 22
    assert svc._reset_day_for("firecrawl-proxy") == 9

    # 10-05 标记耗尽, 今天 10-20: 备用账号 10-09 已重置过 -> 放行;
    # 主账号要等到 10-22 -> 仍闩锁。同一个日期、两个账号结论不同。
    today = date(2026, 10, 20)
    assert svc._quota_reset_since("firecrawl", date(2026, 10, 5), today) is False
    assert svc._quota_reset_since("firecrawl-proxy", date(2026, 10, 5), today) is True
