"""API-Football 主备通道（备用 key + 代理出口 + 伪装指纹）行为测试。

覆盖的不变量:
1. 通道解析: 主通道恒在; 代理通道仅当「代理 + 备用 key」同时配齐才出现;
   半配置告警并按未配置处理。
2. 通道选择: 主 key 优先; 主 key 当日额度耗尽 -> 自动选备用通道;
   全部不可用 -> ApiFootballQuotaExceeded; 一个 key 都没配 -> ApiFootballError。
3. 自动切换 (sync/async): 主通道响应日额度耗尽 -> 按当前通道 provider 记账
   (api-football), 同一次调用内改用备用 key + 代理 client 重发并成功;
   备用通道也耗尽 -> 记 api-football-proxy 并抛异常。
4. 代理异常: 代理出口连接失败 (重试耗尽) -> ApiFootballProxyError, 且异常
   消息里的代理凭证已脱敏。
5. 指纹请求头: 代理通道带一整套浏览器头 (UA/sec-ch-ua/sec-fetch-*), 不伪造
   Accept-Encoding; 主通道不带指纹。
6. 主动探测: refresh_api_football_quota_status 的 exhausted/available/error 三态。

所有用例都不碰真实网络与真实 DB: 请求走 httpx.MockTransport, DB 用
_BrokenSession 让 api_status_service 降级到进程内缓存。模块级 autouse fixture
把节流间隔缩到毫秒级 — 否则进程级限流器会让每个用例之间等 6.7s。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core import api_football as af
from app.core.proxy_utils import mask_proxy_url, normalize_proxy_url
from app.services import api_status_service as svc

BASE_URL = "https://api-sports.io/v1"
PROXY_URL = "http://user:s3cret@10.0.0.9:8080"
DAILY_LIMIT_BODY = (
    '{"errors": {"rateLimit": "The daily request limit for the API has been reached."}, '
    '"response": []}'
)


class _BrokenSession:
    """任何方法都抛异常, 模拟 DB 不可用 (与 test_api_status_service 同一招)。"""

    def __getattr__(self, name):
        def _raise(*a, **kw):
            raise RuntimeError(f"DB unavailable in test ({name})")

        return _raise


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch):
    """把节流间隔与退避基数缩到毫秒级, 让重试用例在秒级跑完。"""
    monkeypatch.setattr(af, "MIN_REQUEST_INTERVAL", 0.01)
    monkeypatch.setattr(af, "TRANSPORT_ERROR_BASE_DELAY", 0.01)
    monkeypatch.setattr(af, "RATE_LIMIT_MAX_BACKOFF", 0.02)


@pytest.fixture()
def no_db(monkeypatch):
    """api_status_service 的 DB 全部降级; 闩锁只走进程内缓存。"""
    monkeypatch.setattr(svc, "SessionLocal", lambda: _BrokenSession())
    svc.clear_cache_for_tests()
    yield
    svc.clear_cache_for_tests()


def _configure(monkeypatch, *, main_key="main-key", proxy_url="", proxy_base="", proxy_key=""):
    """一次性写好通道相关 settings (snapshot 变则 resolve_channels 自动重建)。"""
    monkeypatch.setattr(af.settings, "api_football_key", main_key)
    monkeypatch.setattr(af.settings, "api_football_base_url", BASE_URL)
    monkeypatch.setattr(af.settings, "api_football_proxy", proxy_url)
    monkeypatch.setattr(af.settings, "api_football_proxy_base_url", proxy_base)
    monkeypatch.setattr(af.settings, "api_football_proxy_api_key", proxy_key)


def _seed_client(channel: af._Channel, handler, *, sync: bool) -> list:
    """给某通道预置 MockTransport client (带 base_url, 与 _build_client 一致)。"""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client_cls = httpx.Client if sync else httpx.AsyncClient
    client = client_cls(transport=httpx.MockTransport(_handler), base_url=BASE_URL)
    cache = af._sync_clients if sync else af._async_clients
    cache[(channel.proxy_url, channel.base_url)] = client
    return seen


def _channels_by_name() -> dict:
    return {c.name: c for c in af.resolve_channels()}


def _daily_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=DAILY_LIMIT_BODY)


# ── 通道解析 ──────────────────────────────────────────────────

def test_resolve_channels_main_only(monkeypatch):
    _configure(monkeypatch)
    channels = af.resolve_channels()
    assert len(channels) == 1
    main = channels[0]
    assert main.name == "main"
    assert main.provider == "api-football"
    assert main.proxy_url == ""
    assert main.fingerprint is False


def test_resolve_channels_proxy_requires_both(monkeypatch):
    """半配置 (只配代理 / 只配备用 key) 一律按未配置处理。"""
    _configure(monkeypatch, proxy_url=PROXY_URL)
    assert len(af.resolve_channels()) == 1

    _configure(monkeypatch, proxy_key="backup-key")
    assert len(af.resolve_channels()) == 1


def test_resolve_channels_proxy_when_both_configured(monkeypatch):
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_base="", proxy_key="backup-key")
    channels = _channels_by_name()
    assert set(channels) == {"main", "proxy"}
    proxy = channels["proxy"]
    assert proxy.provider == "api-football-proxy"
    assert proxy.api_key == "backup-key"
    assert proxy.fingerprint is True
    # base_url 留空 = 沿用主端点
    assert proxy.base_url == BASE_URL


def test_resolve_channels_proxy_base_url_override(monkeypatch):
    _configure(
        monkeypatch, proxy_url=PROXY_URL,
        proxy_base="https://backup.example.com/v1/", proxy_key="backup-key",
    )
    proxy = _channels_by_name()["proxy"]
    assert proxy.base_url == "https://backup.example.com/v1"
    # 代理 URL 的凭证已被规范化 (可安全交给 httpx)
    assert proxy.proxy_url == "http://user:s3cret@10.0.0.9:8080"


# ── 代理 URL 工具 ─────────────────────────────────────────────

def test_normalize_proxy_url_encodes_special_chars():
    """密码里的 # 必须被百分号编码, 否则 netloc 会被截断成 fragment。"""
    raw = "http://nuD#Z+0-a(&8Y0X:pw@10.0.0.9:8080"
    normalized = normalize_proxy_url(raw)
    assert "#" not in normalized.split("@")[0]
    assert normalized.endswith("@10.0.0.9:8080")
    # 幂等: 已编码的凭证不二次编码
    assert normalize_proxy_url(normalized) == normalized


def test_mask_proxy_url_hides_credentials():
    masked = mask_proxy_url(PROXY_URL)
    assert "s3cret" not in masked
    assert masked == "http://user:***@10.0.0.9:8080"
    assert mask_proxy_url("http://10.0.0.9:8080") == "http://10.0.0.9:8080"


# ── 指纹请求头 ────────────────────────────────────────────────

def test_fingerprint_headers_shape(monkeypatch):
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    proxy = _channels_by_name()["proxy"]
    headers = af._headers_for(proxy)

    assert headers["x-apisports-key"] == "backup-key"
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert "Chrome/131" in headers["User-Agent"]
    assert headers["sec-ch-ua-platform"] == '"Windows"'
    assert headers["Sec-Fetch-Mode"] == "cors"
    assert headers["Sec-Fetch-Dest"] == "empty"
    assert headers["Referer"] == "https://www.api-sports.io/"
    # Accept-Encoding 交给 httpx 自行管理, 伪造 br 会导致解码失败
    assert "Accept-Encoding" not in headers


def test_main_channel_has_no_fingerprint(monkeypatch):
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    main = _channels_by_name()["main"]
    headers = af._headers_for(main)
    assert headers == {"x-apisports-key": "main-key"}


# ── 通道选择 ──────────────────────────────────────────────────

def test_select_channel_prefers_main(monkeypatch, no_db):
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    assert af._select_channel().name == "main"


def test_select_channel_switches_when_main_latched(monkeypatch, no_db):
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    svc.mark_quota_exhausted("api-football", "test")
    assert af._select_channel().name == "proxy"


def test_select_channel_raises_when_all_latched(monkeypatch, no_db):
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    svc.mark_quota_exhausted("api-football", "test")
    svc.mark_quota_exhausted("api-football-proxy", "test")
    with pytest.raises(af.ApiFootballQuotaExceeded):
        af._select_channel()


def test_select_channel_raises_when_proxy_not_configured(monkeypatch, no_db):
    _configure(monkeypatch)
    svc.mark_quota_exhausted("api-football", "test")
    with pytest.raises(af.ApiFootballQuotaExceeded):
        af._select_channel()


def test_select_channel_raises_without_any_key(monkeypatch, no_db):
    _configure(monkeypatch, main_key="")
    with pytest.raises(af.ApiFootballError):
        af._select_channel()


def test_has_available_channel_matrix(monkeypatch, no_db):
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    assert af.has_available_channel() is True

    svc.mark_quota_exhausted("api-football", "test")
    # 主 key 耗尽但备用通道可用 — odds_fetcher 不应因此直接降级
    assert af.has_available_channel() is True

    svc.mark_quota_exhausted("api-football-proxy", "test")
    assert af.has_available_channel() is False


# ── 自动切换 (sync) ───────────────────────────────────────────

def test_sync_auto_switches_to_proxy_on_daily_quota(monkeypatch, no_db):
    """主通道响应日额度耗尽 -> 记账 api-football, 同一次调用内切备用通道成功。"""
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    main_seen = _seed_client(_channels_by_name()["main"], _daily_handler, sync=True)
    proxy_seen = _seed_client(
        _channels_by_name()["proxy"],
        lambda req: httpx.Response(200, json={"response": [{"team": {"id": 1}}]}),
        sync=True,
    )

    r = af.api_football_get_sync("teams", params={"league": 39})

    assert r.status_code == 200
    assert r.json() == {"response": [{"team": {"id": 1}}]}
    # 主通道记了账, 备用通道没有
    assert svc.is_quota_exhausted("api-football") is True
    assert svc.is_quota_exhausted("api-football-proxy") is False
    # 请求确实走了两条通道, 且代理通道带了备用 key + 指纹头
    assert len(main_seen) == 1
    assert len(proxy_seen) == 1
    assert proxy_seen[0].headers["x-apisports-key"] == "backup-key"
    assert "Chrome/131" in proxy_seen[0].headers["User-Agent"]
    assert proxy_seen[0].headers["Sec-Fetch-Mode"] == "cors"


def test_sync_both_channels_exhausted_raises(monkeypatch, no_db):
    """主备都耗尽: 两行都记账, 抛 ApiFootballQuotaExceeded, 不再重试。"""
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    for channel in af.resolve_channels():
        _seed_client(channel, _daily_handler, sync=True)

    with pytest.raises(af.ApiFootballQuotaExceeded):
        af.api_football_get_sync("teams")

    assert svc.is_quota_exhausted("api-football") is True
    assert svc.is_quota_exhausted("api-football-proxy") is True


def test_sync_proxy_transport_failure_raises_proxy_error(monkeypatch, no_db):
    """主 key 已闩锁 + 代理出口连不上 -> ApiFootballProxyError, 凭证脱敏。"""
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    svc.mark_quota_exhausted("api-football", "test")

    proxy = _channels_by_name()["proxy"]
    calls = {"n": 0}

    def _broken(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError(f"[ProxyError] Cannot connect to proxy {PROXY_URL}")

    af._sync_clients[(proxy.proxy_url, proxy.base_url)] = httpx.Client(
        transport=httpx.MockTransport(_broken), base_url=BASE_URL
    )

    with pytest.raises(af.ApiFootballProxyError) as exc_info:
        af.api_football_get_sync("teams")

    # 重试耗尽 (3 次) 才放弃
    assert calls["n"] == af.TRANSPORT_ERROR_RETRIES
    # 异常消息里的代理凭证必须脱敏
    assert "s3cret" not in str(exc_info.value)
    assert mask_proxy_url(PROXY_URL) in str(exc_info.value)


def test_sync_main_transport_failure_keeps_plain_error(monkeypatch, no_db):
    """主通道连不上时保持历史行为: 普通 ApiFootballError, 不冒充实战类异常。"""
    _configure(monkeypatch)
    main = _channels_by_name()["main"]

    def _broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    af._sync_clients[(main.proxy_url, main.base_url)] = httpx.Client(
        transport=httpx.MockTransport(_broken), base_url=BASE_URL
    )

    with pytest.raises(af.ApiFootballError) as exc_info:
        af.api_football_get_sync("teams")
    assert not isinstance(exc_info.value, af.ApiFootballProxyError)


# ── 自动切换 (async) ──────────────────────────────────────────

def test_async_auto_switches_to_proxy_on_daily_quota(monkeypatch, no_db):
    """异步路径同样自动切换, 并使用模块级缓存的代理 client。"""
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    main_seen = _seed_client(_channels_by_name()["main"], _daily_handler, sync=False)
    proxy_req = _seed_client(
        _channels_by_name()["proxy"],
        lambda req: httpx.Response(200, json={"response": [{"ok": 1}]}),
        sync=False,
    )

    data = asyncio.run(af.api_football_get_async(None, "/fixtures", {"fixture": 1}))

    assert data == {"response": [{"ok": 1}]}
    assert svc.is_quota_exhausted("api-football") is True
    assert svc.is_quota_exhausted("api-football-proxy") is False
    assert len(main_seen) == 1
    assert len(proxy_req) == 1
    assert proxy_req[0].headers["x-apisports-key"] == "backup-key"
    assert "Chrome/131" in proxy_req[0].headers["User-Agent"]


# ── client 构造 ───────────────────────────────────────────────

def test_build_client_constructs_with_proxy_and_full_timeout(monkeypatch):
    """真实构造 (非 MockTransport) 不能抛错: httpx.Timeout 必须四项齐全,
    proxy= 参数必须被两个通道都接受。"""
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    for channel in af.resolve_channels():
        sync_client = af._build_client(channel, sync=True)
        assert isinstance(sync_client, httpx.Client)
        assert sync_client.timeout.connect == af.CONNECT_TIMEOUT
        assert sync_client.timeout.read == af.READ_TIMEOUT
        assert sync_client.timeout.write == af.CONNECT_TIMEOUT
        assert sync_client.timeout.pool == af.CONNECT_TIMEOUT
        async_client = af._build_client(channel, sync=False)
        assert isinstance(async_client, httpx.AsyncClient)


# ── 闩锁复位 ──────────────────────────────────────────────────

def test_reset_quota_latch_resets_both_rows_when_proxy_configured(monkeypatch):
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")
    reset: list[str] = []
    monkeypatch.setattr(af, "_service_reset_provider", lambda p: reset.append(p))
    af.reset_quota_latch()
    assert reset == ["api-football", "api-football-proxy"]


def test_reset_quota_latch_only_main_without_proxy(monkeypatch):
    _configure(monkeypatch)
    reset: list[str] = []
    monkeypatch.setattr(af, "_service_reset_provider", lambda p: reset.append(p))
    af.reset_quota_latch()
    assert reset == ["api-football"]


# ── 主动探测 (/status) ────────────────────────────────────────

def _seed_probe_client(handler, *, http_proxy: str = "") -> None:
    """给探测路径的 client 缓存里塞 MockTransport (键与 refresh_* 内部构造一致)。"""
    af._sync_clients[(normalize_proxy_url(http_proxy), BASE_URL)] = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    )


def test_refresh_quota_status_exhausted(monkeypatch, no_db):
    _configure(monkeypatch)
    _seed_probe_client(
        lambda req: httpx.Response(
            200, json={"response": {"requests": {"current": 100, "limit_day": 100}}}
        ),
    )
    result = af.refresh_api_football_quota_status(
        api_key="main-key", base_url=BASE_URL, provider="api-football",
    )
    assert result["status"] == "exhausted"
    assert svc.is_quota_exhausted("api-football") is True


def test_refresh_quota_status_available_resets(monkeypatch, no_db):
    _configure(monkeypatch)
    _seed_probe_client(
        lambda req: httpx.Response(
            200, json={"response": {"requests": {"current": 7, "limit_day": 100}}}
        ),
    )
    # 先闩锁再探测: available 必须把闩锁复位 (不必等 UTC 跨天)
    svc.mark_quota_exhausted("api-football", "test")
    assert svc.is_quota_exhausted("api-football") is True

    result = af.refresh_api_football_quota_status(
        api_key="main-key", base_url=BASE_URL, provider="api-football",
    )
    assert result["status"] == "available"
    assert result["current"] == 7
    assert result["limit_day"] == 100
    assert svc.is_quota_exhausted("api-football") is False


def test_refresh_quota_status_error_keeps_db(monkeypatch, no_db):
    _configure(monkeypatch)
    _seed_probe_client(lambda req: httpx.Response(502, text="Bad Gateway"))
    result = af.refresh_api_football_quota_status(
        api_key="main-key", base_url=BASE_URL, provider="api-football",
    )
    assert result["status"] == "error"
    # 查询失败不动数据库
    assert svc.is_quota_exhausted("api-football") is False


def test_refresh_quota_status_masks_proxy_in_error(monkeypatch, no_db):
    _configure(monkeypatch, proxy_url=PROXY_URL, proxy_key="backup-key")

    def _broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"proxy {PROXY_URL} unreachable")

    _seed_probe_client(_broken, http_proxy=PROXY_URL)
    result = af.refresh_api_football_quota_status(
        api_key="backup-key", base_url=BASE_URL,
        http_proxy=PROXY_URL, provider="api-football-proxy",
    )
    assert result["status"] == "error"
    assert "s3cret" not in result["error"]
