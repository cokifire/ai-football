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
from email.utils import parsedate_to_datetime
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


class ApiFootballError(Exception):
    """API-Football 请求失败(非额度问题)。"""


class ApiFootballQuotaExceeded(Exception):
    """当前 API key 的当日请求额度已耗尽。"""


class ApiFootballRateLimited(ApiFootballError):
    """收到短时限流，重试后仍未恢复。"""


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


def _rate_limit_wait_seconds(response: httpx.Response, attempt: int) -> float:
    """读取 Retry-After；缺失或无效时使用指数退避。"""
    retry_after = (response.headers.get("retry-after") or "").strip()
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
    return TRANSPORT_ERROR_BASE_DELAY * (2 ** attempt)


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
    ApiFootballError。
    """
    params = params or {}
    key = (settings.api_football_key or "").strip()
    if not key:
        raise ApiFootballError("API-Football key 未配置 (api_football_key)")

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
        t0 = time.monotonic()
        try:
            r = httpx.get(
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
                logger.warning(
                    f"[API-Football][{key_label}] 明确的当日额度耗尽 {endpoint} "
                    f"({r.status_code})；{_limit_diagnostics(r, body)}"
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
    ApiFootballQuotaExceeded。
    """
    params = params or {}
    t0 = time.monotonic()
    r = await client.get(endpoint, params=params)
    elapsed = time.monotonic() - t0
    if elapsed >= SLOW_LOG_THRESHOLD:
        logger.warning(f"[API-Football][async] 慢请求 {endpoint} {elapsed:.1f}s -> {r.status_code}")

    body = r.text or ""
    if _is_daily_quota_exhausted(r.status_code, body):
        raise ApiFootballQuotaExceeded(f"额度耗尽: {endpoint}")

    logger.debug(f"[API-Football][async] {endpoint} -> {r.status_code} ({elapsed:.2f}s)")
    r.raise_for_status()
    return r.json()


def get_async_client() -> httpx.AsyncClient:
    """构建带 API-Football key 的共享异步 client。"""
    key = (settings.api_football_key or "").strip()
    base_url = settings.api_football_base_url.rstrip("/")
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"x-apisports-key": key},
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT),
    )
