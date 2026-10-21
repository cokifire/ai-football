"""统一的 API-Football 客户端。

集中处理:
1. 统一的超时与瞬时故障重试(连接/TLS 超时、5xx、短时 429)。
2. 出站请求日志: 记录每个请求的方法/端点/耗时/状态码,
   便于排查"平台有记录、本地查不到"类问题。

所有原本散落在各 service / tool 中的 `httpx.get(...)` 都应改为调用本模块,
切勿再新增裸 httpx 调用。
"""
from __future__ import annotations

import time
import asyncio
from collections import deque
from email.utils import parsedate_to_datetime
from threading import Lock
import httpx
from loguru import logger

from app.core.config import settings

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


class _ApiRateLimiter:
    """进程内共享限流器，避免多个同步/异步任务共同冲垮同一个 API key。

    采用「均匀节流」而非「攒满窗口再干等」: 相邻请求至少间隔
    ``API_RATE_WINDOW / (API_RATE_LIMIT - 1)`` 秒(默认约 6.7s), 使请求平摊在
    整个窗口内, 单次等待时长因此落在 6~10s 而不是接近整个窗口的 48s+。
    滑动窗口仍作为兜底, 防止外部并发调用一次性打满额度。
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
_sync_http_client = httpx.Client()


class ApiFootballError(Exception):
    """API-Football 请求失败(非额度问题)。"""


class ApiFootballQuotaExceeded(Exception):
    """当前 API key 的当日请求额度已耗尽。"""


class ApiFootballRateLimited(ApiFootballError):
    """收到短时限流，重试后仍未恢复。"""


# ── 当日额度耗尽闩锁 ─────────────────────────────────────────
# 闩锁状态现在由 ``app.services.api_status_service`` 持久化到 ``api_status`` 表
# （原 ``_daily_quota_exhausted`` 是进程内 bool，调度进程与手动工具看不到对方状态）。
# 这里只保留 3 个薄包装函数，把调用全部转发到 service，使所有调用方的 import
# 路径不变 (``from app.core.api_football import is_quota_exhausted, ...``)。
from app.services.api_status_service import (
    is_quota_exhausted as _service_is_quota_exhausted,
    mark_quota_exhausted as _service_mark_quota_exhausted,
    reset_provider as _service_reset_provider,
)


def mark_quota_exhausted(reason: str = "") -> None:
    """标记当日额度已耗尽：写 ``api_status`` 表 + 进程缓存，后续请求快速失败。"""
    _service_mark_quota_exhausted("api-football", reason)


def is_quota_exhausted() -> bool:
    """当日额度是否已耗尽（供调用方提前放弃长任务）。跨进程共享，跨天自动复位。"""
    return _service_is_quota_exhausted("api-football")


def reset_quota_latch() -> None:
    """每个调度 tick 开始时复位闩锁（清缓存 + DB 置 0），新的一天额度会恢复。"""
    _service_reset_provider("api-football")


def _is_daily_quota_exhausted(status_code: int, body: str) -> bool:
    """判断响应是否明确表示 *当日* 请求额度耗尽。

    HTTP 429 本身只代表请求过多，可能是按 IP/代理节点的短时限流；不能
    仅凭状态码把 key 判为日额度用完。只有响应体明确提到 day/daily 时才判定为额度耗尽。
    """
    lowered = body.lower()
    if status_code not in (200, 403, 429):
        return False
    daily_markers = (
        "request limit for the day",
        "requests limit for the day",
        "request limit of the day",
        "requests limit of the day",
        "daily request limit",
        "daily limit",
        "quota for the day",
        "daily quota",
        "you have reached the request limit for the day",
    )
    return any(marker in lowered for marker in daily_markers)


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


def api_football_get_sync(
    endpoint: str,
    params: dict | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    """同步 GET, 返回 httpx.Response (调用方自行 .json() / 读 headers)。

    单一 API key 额度耗尽时抛出 ApiFootballQuotaExceeded；其它错误抛
    ApiFootballError。当日额度已确认耗尽时直接快速失败，不再消耗额度重试。
    """
    params = params or {}
    key = (settings.api_football_key or "").strip()
    if not key:
        raise ApiFootballError("API-Football key 未配置 (api_football_key)")
    if is_quota_exhausted():
        raise ApiFootballQuotaExceeded(f"API-Football key 当日额度已耗尽: {endpoint}")

    timeout_cfg = httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=timeout or READ_TIMEOUT,
        write=CONNECT_TIMEOUT,
        pool=CONNECT_TIMEOUT,
    )
    base_url = settings.api_football_base_url.rstrip("/")
    url = f"{base_url}/{endpoint.lstrip('/')}"

    last_err: Exception | None = None
    key_label = "single"
    for attempt in range(max(TRANSPORT_ERROR_RETRIES, RATE_LIMIT_RETRIES)):
        try:
            # 每次真正发出的请求（包括重试）都占用一个额度。
            _api_rate_limiter.acquire()
            # 计时从通过限流器之后开始，避免把节流等待算成"慢请求"。
            t0 = time.monotonic()
            r = _sync_http_client.get(
                url,
                headers={"x-apisports-key": key},
                params=params,
                timeout=timeout_cfg,
            )
            elapsed = time.monotonic() - t0
            if elapsed >= SLOW_LOG_THRESHOLD:
                logger.warning(
                    f"[API-Football][{key_label}] 慢请求 {endpoint} "
                    f"{elapsed:.1f}s -> {r.status_code}"
                )

            body = r.text or ""
            if _is_daily_quota_exhausted(r.status_code, body):
                # 置位闩锁：本次任务剩余的请求全部快速失败，避免空转一小时
                mark_quota_exhausted(f"{endpoint} status={r.status_code}")
                logger.debug(
                    f"[API-Football][{key_label}] 额度耗尽诊断 {endpoint}；"
                    f"{_limit_diagnostics(r, body)}"
                )
                raise ApiFootballQuotaExceeded(f"API-Football key 额度耗尽: {endpoint}")

            if r.status_code == 429:
                # 429 不等于日额度耗尽，先在当前 key 上按退避策略重试。
                if attempt < RATE_LIMIT_RETRIES - 1:
                    wait = _rate_limit_wait_seconds(r, attempt)
                    logger.warning(
                        f"[API-Football][{key_label}] 短时限流 {endpoint} (429)，"
                        f"{wait:.1f}s 后重试 #{attempt + 1}；{_limit_diagnostics(r, body)}"
                    )
                    # 同时把本地节流窗口后移，否则后续请求仍踩在同一个边界上。
                    _api_rate_limiter.penalize(wait)
                    time.sleep(wait)
                    continue
                logger.warning(
                    f"[API-Football][{key_label}] 短时限流重试耗尽 {endpoint} (429)；"
                    f"{_limit_diagnostics(r, body)}"
                )
                raise ApiFootballRateLimited(f"key[{key_label}] 短时限流: {endpoint}")

            if r.status_code in (500, 502, 503, 504):
                wait = TRANSPORT_ERROR_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"[API-Football][{key_label}] {r.status_code} 瞬时错误 "
                    f"{endpoint}, {wait:.1f}s 后重试 #{attempt+1}"
                )
                time.sleep(wait)
                continue

            logger.debug(
                f"[API-Football][{key_label}] {endpoint} -> {r.status_code} "
                f"({elapsed:.2f}s)"
            )
            return r

        except (httpx.TimeoutException, httpx.TransportError) as e:
            wait = TRANSPORT_ERROR_BASE_DELAY * (2 ** attempt)
            logger.warning(
                f"[API-Football][{key_label}] 传输错误 {endpoint}: {e}, "
                f"{wait:.1f}s 后重试 #{attempt+1}"
            )
            last_err = e
            time.sleep(wait)
    if isinstance(last_err, ApiFootballQuotaExceeded):
        raise last_err
    raise ApiFootballError(f"API-Football 请求失败 {endpoint}: {last_err}")


async def api_football_get_async(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict | None = None,
) -> dict:
    """异步 GET (配合共享 AsyncClient 使用), 返回解析后的 JSON dict。

    使用共享 client 默认 headers 中的单一 key，并在额度耗尽时抛出
    ApiFootballQuotaExceeded。当日额度已确认耗尽时直接快速失败。
    """
    params = params or {}
    if is_quota_exhausted():
        raise ApiFootballQuotaExceeded(f"API-Football key 当日额度已耗尽: {endpoint}")
    for attempt in range(RATE_LIMIT_RETRIES):
        await asyncio.to_thread(_api_rate_limiter.acquire)
        t0 = time.monotonic()
        try:
            r = await client.get(endpoint, params=params)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt >= RATE_LIMIT_RETRIES - 1:
                raise
            await asyncio.sleep(TRANSPORT_ERROR_BASE_DELAY * (2 ** attempt))
            continue

        elapsed = time.monotonic() - t0
        if elapsed >= SLOW_LOG_THRESHOLD:
            logger.warning(f"[API-Football][async] 慢请求 {endpoint} {elapsed:.1f}s -> {r.status_code}")

        body = r.text or ""
        if _is_daily_quota_exhausted(r.status_code, body):
            mark_quota_exhausted(f"{endpoint} status={r.status_code}")
            raise ApiFootballQuotaExceeded(f"额度耗尽: {endpoint}")
        if r.status_code == 429:
            if attempt >= RATE_LIMIT_RETRIES - 1:
                raise ApiFootballRateLimited(f"短时限流: {endpoint}")
            wait = _rate_limit_wait_seconds(r, attempt)
            _api_rate_limiter.penalize(wait)
            await asyncio.sleep(wait)
            continue

        logger.debug(f"[API-Football][async] {endpoint} -> {r.status_code} ({elapsed:.2f}s)")
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
    """构建带 API-Football key 的共享异步 client。"""
    key = (settings.api_football_key or "").strip()
    base_url = settings.api_football_base_url.rstrip("/")
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"x-apisports-key": key},
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT),
    )
