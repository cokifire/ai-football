"""统一的 API-Football 客户端（主备 key 多通道）。

集中处理:
1. 统一的超时与瞬时故障重试(连接/TLS 超时、5xx、短时 429)。
2. 主备 key 自动切换: 主 key 当日额度 (UTC 自然日) 耗尽时, 自动改走「备用 key
   + HTTP 代理出口」通道, 代理通道附带一套固定的浏览器伪装请求头。两个通道的
   额度在 ``api_status`` 表分两行记账 (``api-football`` / ``api-football-proxy``),
   互不顶掉 (对标 Firecrawl 的 firecrawl / firecrawl-proxy 双行方案)。
3. 出站请求日志: 记录每个请求的通道/端点/耗时/状态码,
   便于排查"平台有记录、本地查不到"类问题。

所有原本散落在各 service / tool 中的 `httpx.get(...)` 都应改为调用本模块,
切勿再新增裸 httpx 调用。
"""
from __future__ import annotations

import time
import asyncio
import json
from collections import deque
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from threading import Lock
import httpx
from loguru import logger

from app.core.config import settings
from app.core.proxy_utils import mask_proxy_url, normalize_proxy_url
from app.models.api_status import PROVIDER_API_FOOTBALL, PROVIDER_API_FOOTBALL_PROXY

# ── 超时与重试配置 ─────────────────────────────────────────────
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
TRANSPORT_ERROR_RETRIES = 3          # 连接/TLS/网络瞬时故障重试
TRANSPORT_ERROR_BASE_DELAY = 1.5    # 退避基数(秒)
SLOW_LOG_THRESHOLD = 5.0            # 慢请求日志阈值(秒)
RATE_LIMIT_RETRIES = 3              # 非日额度的 429 重试次数
MAX_RATE_LIMIT_WAIT = 60.0          # 单次限流等待上限，避免任务无限阻塞
API_RATE_LIMIT = max(settings.api_football_rate_limit, 1)
API_RATE_WINDOW = max(settings.api_football_rate_window, 1.0)
# 预留的配额余量: 服务端窗口与本地窗口并不同步(服务端按自己的首次请求/自然分钟
# 计时), 本地按 窗口/额度 精确放行等于贴着上限跑, 边界错开就会出现"第 11 次"。
# 因此节流时间隔按 额度-1 计算: 10 次/分钟 -> 60/9 ≈ 6.7s, 留一次余量。
RATE_LIMIT_HEADROOM = 1
EFFECTIVE_RATE_LIMIT = max(API_RATE_LIMIT - RATE_LIMIT_HEADROOM, 1)
# 均匀节流间隔：用于本地节流与无 Retry-After 时的 429 退避。
MIN_REQUEST_INTERVAL = API_RATE_WINDOW / EFFECTIVE_RATE_LIMIT
# 429 无 Retry-After 时的退避上限（保证单次等待落在 6~10s 区间内）。
RATE_LIMIT_MAX_BACKOFF = 10.0

# 免费套餐硬上限。配置值高于它只会让服务端持续 429，启动即告警。
KNOWN_PLAN_CEILING = 10
if API_RATE_LIMIT > KNOWN_PLAN_CEILING:
    logger.warning(
        f"[API-Football] API_FOOTBALL_RATE_LIMIT={API_RATE_LIMIT} 高于免费套餐上限 "
        f"{KNOWN_PLAN_CEILING}，服务端会持续 429；请改为 {KNOWN_PLAN_CEILING} 或确认套餐已升级"
    )

DAILY_QUOTA_MARKERS = (
    "request limit for the day",
    "requests limit for the day",
    "request limit of the day",
    "requests limit of the day",
    "daily request limit",
    "daily rate limit",
    "rate limit for the day",
    "daily limit",
    "quota for the day",
    "daily quota",
    "you have reached the request limit for the day",
)
RATE_LIMIT_MARKERS = (
    "rate limit",
    "ratelimit",
    "too many requests",
)


class _ApiRateLimiter:
    """进程内共享限流器，避免多个同步/异步任务共同冲垮同一个 API key。

    采用「均匀节流」而非「攒满窗口再干等」: 相邻请求至少间隔
    ``API_RATE_WINDOW / (API_RATE_LIMIT - 1)`` 秒(默认约 6.7s), 使请求平摊在
    整个窗口内, 单次等待时长因此落在 6~10s 而不是接近整个窗口的 48s+。
    滑动窗口仍作为兜底, 防止外部并发调用一次性打满额度。

    主备两个通道共用这一个限流器: 两个账号的分钟窗口各自独立计, 但同一出口
    (代理) 的分钟节奏仍受此约束; 切换通道时不另立节奏, 避免两个通道叠加超发。
    """

    def __init__(self):
        self._timestamps: deque[float] = deque()
        self._next_slot = 0.0  # monotonic 时间戳: 下一个允许发出请求的时刻
        self._lock = Lock()

    def penalize(self, seconds: float) -> None:
        """收到服务端 429 后把本地节流窗口整体后移, 重新对齐服务端窗口。

        服务端一旦判定超限, 它的窗口起点与我们本地记录的并不一致; 只在本次请求
        上 sleep 而不移动本地节奏, 后续请求仍会踩在同一个边界上反复 429。
        """
        with self._lock:
            self._next_slot = max(self._next_slot, time.monotonic() + max(seconds, 0.0))

    def wait_for_drain(self, keep: int = 0, max_wait: float = 90.0) -> float:
        """等待直到滑动窗口里剩余请求数 <= ``keep``, 让本进程配额占满的 60s
        在服务端那边完整滚出去, 再开始下一阶段任务。

        ``sync_fixtures`` 等"会一气发 10 个请求"的任务结束后, 调用此方法再
        跑 ``sync_completed_sub_data``, 避免两段任务 1 分钟内合计 11+ 个请求
        把服务端撞 429。``max_wait`` 是单次最长等待秒数, 防止对账时间漂移时
        无限阻塞。
        """
        deadline = time.monotonic() + max_wait
        target = max(keep, 0)
        slept = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= API_RATE_WINDOW:
                    self._timestamps.popleft()
                if len(self._timestamps) <= target or now >= deadline:
                    return slept
                # 等到最早一次请求滑出窗口
                wait = API_RATE_WINDOW - (now - self._timestamps[0]) + 0.1
                actual = min(wait, max(deadline - now, 0.1))
            time.sleep(actual)
            slept += actual

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= API_RATE_WINDOW:
                    self._timestamps.popleft()

                wait = max(self._next_slot - now, 0.0)
                window_full = False
                if wait <= 0.0 and len(self._timestamps) >= API_RATE_LIMIT:
                    # 兜底: 窗口被外部调用打满, 等到最早一次请求滑出窗口
                    wait = max(API_RATE_WINDOW - (now - self._timestamps[0]) + 0.1, 0.1)
                    window_full = True

                if wait <= 0.0:
                    self._timestamps.append(now)
                    self._next_slot = now + MIN_REQUEST_INTERVAL
                    return

                # 提前占位, 避免并发调用者醒来后挤占同一时间点
                slot = now + wait
                self._timestamps.append(slot)
                self._next_slot = slot + MIN_REQUEST_INTERVAL

            if window_full:
                logger.info(
                    f"[API-Football] 滑动窗口已满 "
                    f"({API_RATE_LIMIT}/{int(API_RATE_WINDOW)}s), 等待 {wait:.1f}s"
                )
            else:
                # 常规均匀节流属于设计内行为, 不是服务端报错; 降为 DEBUG,
                # 避免被误读成"遇到限流"。
                logger.debug(f"[API-Football] 均匀节流: 等待 {wait:.1f}s")
            time.sleep(wait)
            return


_api_rate_limiter = _ApiRateLimiter()


class ApiFootballError(Exception):
    """API-Football 请求失败(非额度问题)。"""


class ApiFootballQuotaExceeded(Exception):
    """当前 API key 的当日请求额度已耗尽。"""


class ApiFootballRateLimited(ApiFootballError):
    """收到短时限流，重试后仍未恢复。"""


class ApiFootballProxyError(ApiFootballError):
    """代理通道自身不可用(连接失败/代理拒连), 重试后仍未恢复。

    与额度/限流区分开: 调用方看到它应该知道"换了出口也打不通", 消息里的
    代理 URL 已脱敏。
    """


# ── 当日额度耗尽闩锁 ─────────────────────────────────────────
# 闩锁状态由 ``app.services.api_status_service`` 持久化到 ``api_status`` 表
# （原 ``_daily_quota_exhausted`` 是进程内 bool，调度进程与手动工具看不到对方状态）。
# 主备两个 key 分两行记: ``api-football``(主) 与 ``api-football-proxy``(备用),
# 两个账号的当日额度互不相干, 分两行才不会互相顶掉。
# 这里只保留几个薄包装函数，把调用全部转发到 service，使所有调用方的 import
# 路径不变 (``from app.core.api_football import is_quota_exhausted, ...``)。
from app.services.api_status_service import (
    is_quota_exhausted as _service_is_quota_exhausted,
    mark_quota_exhausted as _service_mark_quota_exhausted,
    reset_provider as _service_reset_provider,
)


def mark_quota_exhausted(
    reason: str = "", provider: str = PROVIDER_API_FOOTBALL,
) -> None:
    """标记当日额度已耗尽：写 ``api_status`` 表 + 进程缓存，后续请求快速失败。

    ``provider`` 默认主通道; 备用通道传 ``PROVIDER_API_FOOTBALL_PROXY`` —
    两行分开记, 主 key 耗尽不会把备用 key 一起闩掉。
    """
    _service_mark_quota_exhausted(provider, reason)


def is_quota_exhausted(provider: str = PROVIDER_API_FOOTBALL) -> bool:
    """当日额度是否已耗尽（供调用方提前放弃长任务）。跨进程共享，跨天自动复位。"""
    return _service_is_quota_exhausted(provider)


def reset_channel_quota(provider: str) -> None:
    """复位指定通道的额度闩锁（清缓存 + DB 置 0）。"""
    _service_reset_provider(provider)


def _proxy_channel_configured() -> bool:
    """代理 + 备用 key 是否同时配齐（半配置不算，见 resolve_channels）。"""
    _, _, proxy_url, _, proxy_key = _settings_snapshot()
    return bool(proxy_url and proxy_key)


def reset_quota_latch() -> None:
    """每个调度 tick 开始时复位闩锁（清缓存 + DB 置 0），新的一天额度会恢复。

    主备两个通道都复位(备用通道仅在已配置时): 新的一天两个账号的额度都会恢复,
    手动换 key / 升级套餐后同样需要一次重新探测的机会。复位只是"给本次运行一次
    探测机会": 若额度真的耗尽, 本轮首个请求会再次识别并标记, 同一轮内后续调用
    仍然快速失败。
    """
    _service_reset_provider(PROVIDER_API_FOOTBALL)
    if _proxy_channel_configured():
        _service_reset_provider(PROVIDER_API_FOOTBALL_PROXY)


def _is_daily_quota_exhausted(status_code: int, body: str) -> bool:
    """判断响应是否明确表示 *当日* 请求额度耗尽。

    HTTP 429 本身只代表请求过多，可能是按 IP/代理节点的短时限流；不能
    仅凭状态码把 key 判为日额度用完。HTTP 200 只检查 API 返回的 ``errors``
    字段，避免正常响应 body 中的普通文本触发误判。
    """
    if status_code not in (200, 403, 429):
        return False
    error_text = _response_error_text(status_code, body).lower()
    return any(marker in error_text for marker in DAILY_QUOTA_MARKERS)


def _response_error_text(status_code: int, body: str) -> str:
    """提取 API 错误文本，避免扫描正常响应的全部 JSON。

    API-Football 有时会用 HTTP 200 携带 ``errors`` 对象返回业务错误。
    对这种响应只信任 ``errors``；HTTP 200 的 ``response`` 数据和其它元数据
    不参与额度判定。非 200 的非 JSON 响应则保留正文，兼容网关错误页。
    """
    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return body if status_code != 200 else ""

    if not isinstance(payload, dict) or "errors" not in payload:
        return body if status_code != 200 else ""

    errors = payload.get("errors")
    if not errors:
        return ""
    # 不把字段名（例如 ``rateLimit``）混入分类文本，否则每日额度错误
    # 会被字段名本身误判成普通 rate-limit 错误。
    def _values_only(value) -> str:
        if isinstance(value, dict):
            return " ".join(_values_only(item) for item in value.values())
        if isinstance(value, list):
            return " ".join(_values_only(item) for item in value)
        return str(value)

    return _values_only(errors)


def _is_rate_limited_response(status_code: int, body: str) -> bool:
    """识别 HTTP 429 或 HTTP 200 携带的短时限流业务错误。"""
    if status_code == 429:
        return True
    error_text = _response_error_text(status_code, body).lower()
    return any(marker in error_text for marker in RATE_LIMIT_MARKERS)


def _quota_exhausted_reason(endpoint: str, response: httpx.Response, body: str) -> str:
    """生成写入 api_status 的脱敏诊断信息，不包含 API key。"""
    return (
        f"{endpoint} status={response.status_code}; "
        f"{_limit_diagnostics(response, body)}"
    )


def _rate_limit_wait_seconds(response: httpx.Response | None = None, attempt: int = 0) -> float:
    """读取 Retry-After；缺失或无效时按均匀节流下限退避(6.7s/10s/10s)。

    API-Football 的限流以分钟窗口计, 因此退避基数取 ``窗口/额度``(默认 6.7s),
    而不是传输层故障用的 1.5s, 避免"等 1.5s 再撞一次限流"。基数无需对齐
    12~15s 的常规节流区间: 重试仍要经过 acquire() 等下一个节流槽位,
    退避更短只是让重试更早开始排队, 不会提前发出请求。
    """
    retry_after = ((response.headers.get("retry-after") if response else None) or "").strip()
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), MAX_RATE_LIMIT_WAIT)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is not None:
                    return min(max(retry_at.timestamp() - time.time(), 0.0), MAX_RATE_LIMIT_WAIT)
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
    return min(MIN_REQUEST_INTERVAL * (2 ** attempt), RATE_LIMIT_MAX_BACKOFF)


def _limit_diagnostics(response: httpx.Response, body: str) -> str:
    """返回不含 key 的限流诊断信息，方便区分日额度与短时限流。"""
    headers = response.headers
    body_preview = " ".join(body.split())[:500]
    return (
        "retry_after=" + repr(headers.get("retry-after"))
        + f", remaining={headers.get('x-ratelimit-requests-remaining')!r}"
        + f", limit={headers.get('x-ratelimit-requests-limit')!r}"
        + f", body={body_preview!r}"
    )


# ── 主备通道抽象 ───────────────────────────────────────────────
@dataclass(frozen=True)
class _Channel:
    """一条 API-Football 访问通道。

    * ``main``  — 主 key 直连 (默认路径, 不带指纹)。
    * ``proxy`` — 备用 key 经 HTTP 代理出口, 附带固定浏览器伪装请求头。
    """

    name: str            # "main" | "proxy"
    api_key: str
    base_url: str
    proxy_url: str       # 空串 = 直连
    provider: str        # api_status 行主键 (额度记账用)
    fingerprint: bool    # 是否附带浏览器伪装请求头


# 代理通道专用: 一套固定的浏览器伪装请求头。
# 为什么需要: 代理出口多为机房 IP, 裸 httpx 的默认头 (python-httpx/x) 在网关侧
# 非常显眼; 带上一套与 Chrome 131 (Windows) 一致的请求头, 让请求在网关侧看起来
# 像普通浏览器发出的 XHR。按需求只保留这一套, 不做多画像轮换。
# 刻意不含 Accept-Encoding: httpx 按实际可解码能力自行设置 (gzip/deflate/...),
# 手写 br 而环境没装 brotli 时会导致响应解码失败。
_BROWSER_FINGERPRINT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "Referer": "https://www.api-sports.io/",
}


def _settings_snapshot() -> tuple:
    """读取与通道相关的全部 settings, 作为通道解析缓存的指纹。

    settings 是进程内单例; 测试里 monkeypatch 后 snapshot 变化,
    ``resolve_channels`` 自动重建 — 不需要额外的缓存失效接口。
    """
    return (
        (settings.api_football_key or "").strip(),
        (settings.api_football_base_url or "").strip().rstrip("/"),
        (settings.api_football_proxy or "").strip(),
        (settings.api_football_proxy_base_url or "").strip().rstrip("/"),
        (settings.api_football_proxy_api_key or "").strip(),
    )


# resolve_channels 的结果缓存: (snapshot, channels)。snapshot 变化才重建。
_channels_cache: tuple[tuple, tuple[_Channel, ...]] = ((), ())
# 半配置告警去重: 同一组残缺配置只告警一次, 避免热路径刷屏。
_warned_partial_configs: set[tuple] = set()
_warn_lock = Lock()


def resolve_channels() -> tuple[_Channel, ...]:
    """解析当前可用通道: 主通道恒在; 备用通道仅当代理 + 备用 key 同时配齐才返回。

    半配置 (只配代理或只配备用 key) 视为未启用 — 账号级日额度必须换 key 才恢复,
    IP 级封锁必须换出口, 只换一半是无效切换; 告警一次后按未配置处理, 保持原来的
    快速失败行为, 绝不静默启用半个通道。
    """
    global _channels_cache
    snapshot = _settings_snapshot()
    cached = _channels_cache
    if cached[0] == snapshot:
        return cached[1]

    main_key, main_base, proxy_url, proxy_base, proxy_key = snapshot
    channels = [
        _Channel(
            name="main",
            api_key=main_key,
            base_url=main_base,
            proxy_url="",
            provider=PROVIDER_API_FOOTBALL,
            fingerprint=False,
        )
    ]
    if proxy_url and proxy_key:
        channels.append(
            _Channel(
                name="proxy",
                api_key=proxy_key,
                base_url=proxy_base or main_base,
                proxy_url=normalize_proxy_url(proxy_url),
                provider=PROVIDER_API_FOOTBALL_PROXY,
                fingerprint=True,
            )
        )
    elif proxy_url or proxy_key:
        with _warn_lock:
            if snapshot not in _warned_partial_configs:
                _warned_partial_configs.add(snapshot)
                logger.warning(
                    "[API-Football] 代理通道半配置 (只配了 "
                    f"{'代理' if proxy_url else '备用 key'}), 按未配置处理 — "
                    "API_FOOTBALL_PROXY 与 API_FOOTBALL_PROXY_API_KEY 必须同时设置"
                )

    _channels_cache = (snapshot, tuple(channels))
    return _channels_cache[1]


def _channel_available(channel: _Channel) -> bool:
    """通道此刻能否发请求: 配了 key, 且当日额度未耗尽。"""
    return bool(channel.api_key) and not _service_is_quota_exhausted(channel.provider)


def _select_channel() -> _Channel:
    """按「主 key 优先, 主额度耗尽走备用」选一条可发请求的通道。

    没有可用通道时抛 ``ApiFootballQuotaExceeded``; 一个 key 都没配时抛
    ``ApiFootballError`` (与历史行为一致)。
    """
    channels = resolve_channels()
    for channel in channels:
        if _channel_available(channel):
            return channel
    if not any(c.api_key for c in channels):
        raise ApiFootballError("API-Football key 未配置 (api_football_key)")
    raise ApiFootballQuotaExceeded(
        "API-Football key 当日额度已耗尽: "
        + ", ".join(c.name for c in channels)
    )


def _fallback_channel(current: _Channel) -> _Channel | None:
    """当前通道日额度耗尽后, 找一条还能用的其它通道 (没有则 None)。

    只从主通道往备用通道切: 反向没有意义 (同一次调用里来回横跳), 且能走到
    备用通道时主通道一定处于 latched 状态。
    """
    if current.name != "main":
        return None
    for channel in resolve_channels():
        if channel.name == "main":
            continue
        if _channel_available(channel):
            return channel
    return None


def has_available_channel() -> bool:
    """是否还有可用通道 (供调用方提前判断要不要走备用数据源)。

    ``odds_fetcher`` 等入口用: 主 key 额度耗尽但备用通道可用时, API 路径仍然
    值得一试, 不该直接降级到 Flashscore。
    """
    return any(_channel_available(c) for c in resolve_channels())


def _headers_for(channel: _Channel) -> dict[str, str]:
    """构造该通道的请求头: key 必带; 代理通道叠加固定浏览器指纹。"""
    headers = {"x-apisports-key": channel.api_key}
    if channel.fingerprint:
        headers.update(_BROWSER_FINGERPRINT_HEADERS)
    return headers


# per-channel httpx 客户端缓存: (proxy_url, base_url) -> client。
# 代理出口与端点不变时复用连接池; key 走逐请求 header, 不进缓存键 — 换 key
# 只影响 header, 连接池仍然可以复用。
_sync_clients: dict[tuple[str, str], httpx.Client] = {}
_async_clients: dict[tuple[str, str], httpx.AsyncClient] = {}
_clients_lock = Lock()


def _http_timeout(read: float | None = None) -> httpx.Timeout:
    """统一的超时配置。httpx 要求四项齐全 (或给 default), 只设 connect/read 会直接抛错。"""
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=read or READ_TIMEOUT,
        write=CONNECT_TIMEOUT,
        pool=CONNECT_TIMEOUT,
    )


def _build_client(channel: _Channel, *, sync: bool):
    """按通道构造 httpx client (代理通道带 proxy 出口)。"""
    kwargs: dict = {"timeout": _http_timeout()}
    if channel.base_url:
        kwargs["base_url"] = channel.base_url
    if channel.proxy_url:
        kwargs["proxy"] = channel.proxy_url
    if sync:
        return httpx.Client(**kwargs)
    return httpx.AsyncClient(**kwargs)


def _client_for(channel: _Channel, *, sync: bool):
    """取 (或惰性建) 该通道的 httpx client, 按 (proxy_url, base_url) 缓存。"""
    cache = _sync_clients if sync else _async_clients
    key = (channel.proxy_url, channel.base_url)
    client = cache.get(key)
    if client is not None:
        return client
    with _clients_lock:
        client = cache.get(key)
        if client is None:
            client = _build_client(channel, sync=sync)
            cache[key] = client
    return client


def _main_channel() -> _Channel:
    for channel in resolve_channels():
        if channel.name == "main":
            return channel
    raise ApiFootballError("API-Football key 未配置 (api_football_key)")


def api_football_get_sync(
    endpoint: str,
    params: dict | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    """同步 GET, 返回 httpx.Response (调用方自行 .json() / 读 headers)。

    多通道语义:
    * 主 key 优先; 主 key 当日额度耗尽时自动切换「备用 key + 代理出口」通道,
      在同一次调用内用备用 key 重发 (``switched`` 保证最多切一次, 不会横跳)。
    * 所有通道都不可用 (未配代理 / 备用也耗尽) 时抛 ``ApiFootballQuotaExceeded``;
      当日额度已确认耗尽时直接快速失败, 不再消耗额度重试。
    * 代理通道自身连接失败 (重试耗尽) 抛 ``ApiFootballProxyError`` (脱敏代理 URL)。
    """
    params = params or {}
    channel = _select_channel()
    switched = False

    timeout_cfg = _http_timeout(timeout)

    last_err: Exception | None = None
    for attempt in range(max(TRANSPORT_ERROR_RETRIES, RATE_LIMIT_RETRIES)):
        try:
            client = _client_for(channel, sync=True)
            url = f"{channel.base_url}/{endpoint.lstrip('/')}"
            # 每次真正发出的请求（包括重试）都占用一个额度。
            _api_rate_limiter.acquire()
            # 计时从通过限流器之后开始，避免把节流等待算成"慢请求"。
            t0 = time.monotonic()
            r = client.get(
                url,
                headers=_headers_for(channel),
                params=params,
                timeout=timeout_cfg,
            )
            elapsed = time.monotonic() - t0
            if elapsed >= SLOW_LOG_THRESHOLD:
                logger.warning(
                    f"[API-Football][{channel.name}] 慢请求 {endpoint} "
                    f"{elapsed:.1f}s -> {r.status_code}"
                )

            body = r.text or ""
            if _is_daily_quota_exhausted(r.status_code, body):
                # 置位当前通道的闩锁：本进程后续请求快速失败，避免空转一小时
                reason = _quota_exhausted_reason(endpoint, r, body)
                mark_quota_exhausted(reason, channel.provider)
                logger.debug(
                    f"[API-Football][{channel.name}] 额度耗尽诊断 {reason}"
                )
                fallback = None if switched else _fallback_channel(channel)
                if fallback is not None:
                    logger.warning(
                        f"[API-Football][{channel.name}] 当日额度已耗尽, 切换至备用 key "
                        f"+ 代理通道 (proxy={mask_proxy_url(fallback.proxy_url)}) 重试: {endpoint}"
                    )
                    channel = fallback
                    switched = True
                    continue
                raise ApiFootballQuotaExceeded(
                    f"API-Football key 当日额度已耗尽: {endpoint} (channel={channel.name})"
                )

            if _is_rate_limited_response(r.status_code, body):
                # 短时限流（包括 HTTP 200 携带 errors.rateLimit）不写日额度闩锁。
                # 代理出口的 429 多是 IP 级短时限流, 同样只退避重试, 不切通道。
                if attempt < RATE_LIMIT_RETRIES - 1:
                    wait = _rate_limit_wait_seconds(r, attempt)
                    logger.warning(
                        f"[API-Football][{channel.name}] 短时限流 {endpoint} ({r.status_code})，"
                        f"{wait:.1f}s 后重试 #{attempt + 1}；{_limit_diagnostics(r, body)}"
                    )
                    # 同时把本地节流窗口后移，否则后续请求仍踩在同一个边界上。
                    _api_rate_limiter.penalize(wait)
                    time.sleep(wait)
                    continue
                logger.warning(
                    f"[API-Football][{channel.name}] 短时限流重试耗尽 {endpoint} ({r.status_code})；"
                    f"{_limit_diagnostics(r, body)}"
                )
                raise ApiFootballRateLimited(f"key[{channel.name}] 短时限流: {endpoint}")

            if r.status_code in (500, 502, 503, 504):
                wait = TRANSPORT_ERROR_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"[API-Football][{channel.name}] {r.status_code} 瞬时错误 "
                    f"{endpoint}, {wait:.1f}s 后重试 #{attempt+1}"
                )
                time.sleep(wait)
                continue

            logger.debug(
                f"[API-Football][{channel.name}] {endpoint} -> {r.status_code} "
                f"({elapsed:.2f}s)"
            )
            return r

        except (httpx.TimeoutException, httpx.TransportError) as e:
            wait = TRANSPORT_ERROR_BASE_DELAY * (2 ** attempt)
            last_err = e
            if attempt >= TRANSPORT_ERROR_RETRIES - 1 and channel.name == "proxy":
                # 代理出口本身连不上 (重试已耗尽): 与额度/限流区分开。
                # httpx 的异常文本可能带代理 URL, 必须脱敏后再进消息。
                raise ApiFootballProxyError(
                    f"代理通道不可用 {endpoint}: {mask_proxy_url(str(e))} "
                    f"(proxy={mask_proxy_url(channel.proxy_url)})"
                ) from e
            logger.warning(
                f"[API-Football][{channel.name}] 传输错误 {endpoint}: {mask_proxy_url(str(e)) if channel.name == 'proxy' else e}, "
                f"{wait:.1f}s 后重试 #{attempt+1}"
            )
            time.sleep(wait)
    if isinstance(last_err, ApiFootballQuotaExceeded):
        raise last_err
    raise ApiFootballError(f"API-Football 请求失败 {endpoint}: {last_err}")


async def api_football_get_async(
    client: httpx.AsyncClient | None,
    endpoint: str,
    params: dict | None = None,
) -> dict:
    """异步 GET, 返回解析后的 JSON dict。

    多通道语义与 ``api_football_get_sync`` 一致: 主 key 优先, 主额度耗尽自动
    切「备用 key + 代理出口」通道 (同一次调用内重发, 最多切一次)。
    主通道复用调用方传入的共享 client (连接池); 代理通道改用模块级缓存的代理
    client — 调用方的 client 没有代理出口, 承接不了备用通道。
    当日额度已确认耗尽时直接快速失败。
    """
    params = params or {}
    channel = _select_channel()
    switched = False
    for attempt in range(RATE_LIMIT_RETRIES):
        http = (
            client
            if (channel.name == "main" and client is not None)
            else _client_for(channel, sync=False)
        )
        await asyncio.to_thread(_api_rate_limiter.acquire)
        t0 = time.monotonic()
        try:
            r = await http.get(endpoint, params=params, headers=_headers_for(channel))
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt >= RATE_LIMIT_RETRIES - 1:
                if channel.name == "proxy":
                    raise ApiFootballProxyError(
                        f"代理通道不可用 {endpoint} (proxy={mask_proxy_url(channel.proxy_url)})"
                    ) from None
                raise
            await asyncio.sleep(TRANSPORT_ERROR_BASE_DELAY * (2 ** attempt))
            continue

        elapsed = time.monotonic() - t0
        if elapsed >= SLOW_LOG_THRESHOLD:
            logger.warning(
                f"[API-Football][{channel.name}] 慢请求 {endpoint} {elapsed:.1f}s -> {r.status_code}"
            )

        body = r.text or ""
        if _is_daily_quota_exhausted(r.status_code, body):
            reason = _quota_exhausted_reason(endpoint, r, body)
            mark_quota_exhausted(reason, channel.provider)
            fallback = None if switched else _fallback_channel(channel)
            if fallback is not None:
                logger.warning(
                    f"[API-Football][{channel.name}] 当日额度已耗尽, 切换至备用 key "
                    f"+ 代理通道 (proxy={mask_proxy_url(fallback.proxy_url)}) 重试: {endpoint}"
                )
                channel = fallback
                switched = True
                continue
            raise ApiFootballQuotaExceeded(
                f"额度耗尽: {endpoint} (channel={channel.name})"
            )

        if _is_rate_limited_response(r.status_code, body):
            if attempt >= RATE_LIMIT_RETRIES - 1:
                raise ApiFootballRateLimited(
                    f"key[{channel.name}] 短时限流: {endpoint} ({r.status_code})"
                )
            wait = _rate_limit_wait_seconds(r, attempt)
            _api_rate_limiter.penalize(wait)
            await asyncio.sleep(wait)
            continue

        logger.debug(f"[API-Football][{channel.name}] {endpoint} -> {r.status_code} ({elapsed:.2f}s)")
        r.raise_for_status()
        data = r.json()
        errors = data.get("errors") or {}
        error_text = str(errors).lower()
        if any(term in error_text for term in ("rate limit", "ratelimit", "too many requests")):
            if attempt >= RATE_LIMIT_RETRIES - 1:
                raise ApiFootballRateLimited(f"响应体限流: {endpoint}")
            # HTTP 200 但响应体限流：同样要把本地窗口后移，否则后续请求继续
            # 落在服务端窗口的同一条边界上。
            wait = _rate_limit_wait_seconds(r, attempt)
            _api_rate_limiter.penalize(wait)
            await asyncio.sleep(wait)
            continue
        return data

    raise ApiFootballError(f"API-Football 异步请求失败: {endpoint}")


def get_async_client() -> httpx.AsyncClient:
    """构建带主 key 的共享异步 client (主通道直连)。

    请求头以逐请求覆盖为准 (``api_football_get_async`` 每次都按当前通道传 key),
    这里的默认 header 只是兼容旧调用方直接使用该 client 的场景。
    """
    main = _main_channel()
    client = _build_client(main, sync=False)
    if main.api_key:
        client.headers["x-apisports-key"] = main.api_key
    return client


def refresh_api_football_quota_status(
    *,
    api_key: str,
    base_url: str = "",
    http_proxy: str = "",
    provider: str = PROVIDER_API_FOOTBALL,
) -> dict:
    """主动查询一个 key 的当日额度 (``GET /status``), 并把 api_status 同步成真实状态。

    API-Football 的 ``/status`` 返回 ``response.requests.{current, limit_day}``
    (免费套餐 limit_day=100, 按 UTC 自然日重置)。判定:
        current >= limit_day -> ``mark_quota_exhausted(provider)``
        current <  limit_day -> ``reset_channel_quota(provider)`` (额度恢复/升级后
                               不必等 UTC 跨天)
        接口不可用/解析失败  -> 不动数据库, 被动识别继续兜底

    注意: ``/status`` 本身消耗 1 次当日额度, 因此只放在手动工具
    (``tools/check_api_football_quota.py``) 里用, 不进请求热路径; 探测同样过
    限流器, 不绕过 10 次/分钟的节奏。查询不受闩锁影响 — 闩锁置位时也要能查。
    """
    key = (api_key or "").strip()
    if not key:
        return {"status": "error", "error": "api key 未配置"}
    base = (base_url or settings.api_football_base_url or "").strip().rstrip("/")
    channel = _Channel(
        name="probe",
        api_key=key,
        base_url=base,
        proxy_url=normalize_proxy_url(http_proxy),
        provider=provider,
        fingerprint=bool(http_proxy),
    )
    url = f"{base}/status" if base else "/status"
    try:
        _api_rate_limiter.acquire()
        r = _client_for(channel, sync=True).get(
            url,
            headers=_headers_for(channel),
            timeout=_http_timeout(),
        )
        body = r.text or ""
        if r.status_code != 200:
            return {
                "status": "error",
                "error": f"status={r.status_code} body={' '.join(body.split())[:200]!r}",
            }
        data = r.json()
        requests_info = (data.get("response") or {}).get("requests") or {}
        current = requests_info.get("current")
        limit_day = requests_info.get("limit_day")
        if current is None or limit_day is None:
            return {"status": "error", "error": "响应缺少 response.requests.current/limit_day"}
        current, limit_day = int(current), int(limit_day)
        if current >= limit_day:
            mark_quota_exhausted(f"/status current={current} limit_day={limit_day}", provider)
            return {"status": "exhausted", "current": current, "limit_day": limit_day}
        reset_channel_quota(provider)
        return {"status": "available", "current": current, "limit_day": limit_day}
    except Exception as exc:  # noqa: BLE001 - 探测失败只返回错误, 不动数据库
        # httpx 的异常文本可能带代理 URL (含凭证), 必须脱敏。
        return {"status": "error", "error": mask_proxy_url(str(exc))}
