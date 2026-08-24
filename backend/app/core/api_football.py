"""统一的 API-Football 客户端。

集中处理:
1. 主/备 API Key 自动切换: 当主 key 触发额度耗尽(429 / 403 / 响应体含
   "requests limit" / "quota" 等)时, 自动切换到备用 key 重试。
2. 统一的超时与瞬时故障重试(连接/TLS 超时、5xx)。
3. 出站请求日志: 记录每个请求的方法/端点/耗时/状态码/使用的 key,
   便于排查"平台有记录、本地查不到"类问题。

所有原本散落在各 service / tool 中的 `httpx.get(...)` 都应改为调用本模块,
切勿再新增裸 httpx 调用。
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from loguru import logger

from app.core.config import settings

# ── 超时与重试配置 ─────────────────────────────────────────────
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
TRANSPORT_ERROR_RETRIES = 3          # 连接/TLS/网络瞬时故障重试
TRANSPORT_ERROR_BASE_DELAY = 1.5    # 退避基数(秒)
SLOW_LOG_THRESHOLD = 5.0            # 慢请求日志阈值(秒)


class ApiFootballError(Exception):
    """API-Football 请求失败(非额度问题)。"""


class ApiFootballQuotaExceeded(Exception):
    """所有可用 key 的当日请求额度均耗尽。"""


def _available_keys() -> list[str]:
    """返回非空 key 列表, 主 key 在前, 备用 key 在后。"""
    keys: list[str] = []
    primary = (settings.api_football_key or "").strip()
    backup = (settings.api_football_key_backup or "").strip()
    if primary:
        keys.append(primary)
    if backup and backup != primary:
        keys.append(backup)
    return keys


def _is_quota_exhausted(status_code: int, body: str) -> bool:
    """判断响应是否表示额度耗尽。

    API-Football 在额度耗尽时常见表现:
      - HTTP 429 (Too Many Requests)
      - HTTP 403 + 响应体含 "requests limit" / "quota" 等文案
      - 200 但 errors 字段含限额提示(免费版常见)
    """
    if status_code == 429:
        return True
    lowered = body.lower()
    markers = ("requests limit", "quota", "daily limit", "limit exceeded", "free plan")
    if status_code == 403 and any(m in lowered for m in markers):
        return True
    # 200 但业务层报额度
    if any(m in lowered for m in ("requests limit", "you have reached")):
        return True
    return False


def api_football_get_sync(
    endpoint: str,
    params: dict | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    """同步 GET, 返回 httpx.Response (调用方自行 .json() / 读 headers)。

    主 key 额度耗尽时自动切换备用 key。所有 key 都耗尽则抛出
    ApiFootballQuotaExceeded; 其它错误抛 ApiFootballError。
    """
    params = params or {}
    keys = _available_keys()
    if not keys:
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
    for ki, key in enumerate(keys):
        key_label = "primary" if ki == 0 else f"backup#{ki}"
        for attempt in range(TRANSPORT_ERROR_RETRIES):
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
                if _is_quota_exhausted(r.status_code, body):
                    logger.warning(
                        f"[API-Football][{key_label}] key 额度耗尽 {endpoint} "
                        f"({r.status_code})，尝试下一个 key"
                    )
                    last_err = ApiFootballQuotaExceeded(
                        f"key[{key_label}] 额度耗尽: {endpoint}"
                    )
                    break  # 切换 key, 不再对该 key 重试

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
        # 该 key 已用完重试或明确额度耗尽 -> 进入下一个 key
        else:
            # for 正常结束(没有 break)意味着传输重试全失败
            continue
        # 因 quota break 跳出内层 -> 继续外层下一个 key

    # 所有 key 都失败
    if isinstance(last_err, ApiFootballQuotaExceeded):
        raise last_err
    raise ApiFootballError(f"API-Football 请求失败 {endpoint}: {last_err}")


async def api_football_get_async(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict | None = None,
) -> dict:
    """异步 GET (配合共享 AsyncClient 使用), 返回解析后的 JSON dict。

    异步场景的 key 切换: 复用 client 的默认 headers 中注入的 key 集合,
    失败时由调用方决定是否带新 key 重建 client。这里提供单次请求封装,
    并在额度耗尽时抛出 ApiFootballQuotaExceeded。
    """
    params = params or {}
    t0 = time.monotonic()
    r = await client.get(endpoint, params=params)
    elapsed = time.monotonic() - t0
    if elapsed >= SLOW_LOG_THRESHOLD:
        logger.warning(f"[API-Football][async] 慢请求 {endpoint} {elapsed:.1f}s -> {r.status_code}")

    body = r.text or ""
    if _is_quota_exhausted(r.status_code, body):
        raise ApiFootballQuotaExceeded(f"额度耗尽: {endpoint}")

    logger.debug(f"[API-Football][async] {endpoint} -> {r.status_code} ({elapsed:.2f}s)")
    r.raise_for_status()
    return r.json()


def get_async_client() -> httpx.AsyncClient:
    """构建带主 key 的共享异步 client (供 async_sync_subdata 等使用)。"""
    keys = _available_keys()
    key = keys[0] if keys else ""
    base_url = settings.api_football_base_url.rstrip("/")
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"x-apisports-key": key},
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT),
    )
