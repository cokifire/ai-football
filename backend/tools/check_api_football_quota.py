"""查询 API-Football 两个通道的当日额度, 并把 api_status 表同步成真实状态。

用法 (在 backend/ 目录下运行):
    python tools/check_api_football_quota.py

做的事: 调 API-Football 的 /status 接口拿当日已用/上限 (response.requests.
current/limit_day, UTC 自然日重置), 然后
    current >= limit_day -> api_status 对应行标记 quota_exhausted=1
                            (之后请求快速失败/自动切备用通道, 不再拿 429 试探)
    current <  limit_day -> 复位该行 (额度恢复/套餐升级后不必等 UTC 跨天)
    接口不可用           -> 不动数据库 (被动识别继续兜底)

主账号与备用(代理)账号分开查、分开记账 — 两行 provider:
    api-football        -> 主 key (直连)
    api-football-proxy  -> 备用 key (经代理通道), 未同时配齐代理+备用 key 则跳过
两个账号的当日额度互不相干, 分两行才不会互相顶掉。

注意: /status 本身消耗 1 次当日额度, 所以这是个手动运维工具, 不要放进定时任务。

退出码: 0=至少一个通道额度可用, 2=所有已配置通道都耗尽, 1=查询失败(库未更新)
"""
import sys
from pathlib import Path

# 让脚本在 backend/ 下以 python tools/xxx.py 运行时也能 import app 包
# (直接运行脚本时 sys.path[0] 是 tools/ 目录, 而非 backend/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.api_football import refresh_api_football_quota_status
from app.core.config import settings
from app.core.proxy_utils import mask_proxy_url
from app.models.api_status import PROVIDER_API_FOOTBALL, PROVIDER_API_FOOTBALL_PROXY


def _probe(label, *, api_key, base_url, http_proxy, provider):
    """查一个通道; 返回剩余次数 (查不动返回 None)。"""
    result = refresh_api_football_quota_status(
        api_key=api_key,
        base_url=base_url,
        http_proxy=http_proxy,
        provider=provider,
    )
    status = result.get("status")
    current = result.get("current")
    limit_day = result.get("limit_day")

    if status == "exhausted":
        print(f"[{label}] 当日额度已用完: {current}/{limit_day}")
        print(f"  api_status provider={provider} 已标记 quota_exhausted=1")
        return 0
    if status == "available":
        print(f"[{label}] 当日额度可用: {current}/{limit_day} (剩余 {limit_day - current})")
        print(f"  api_status provider={provider} 已复位为 quota_exhausted=0")
        return (limit_day or 0) - (current or 0)
    print(f"[{label}] 查询失败, 数据库状态未更新: {result.get('error') or '未知错误'}")
    return None


def main() -> int:
    main_left = _probe(
        "主账号",
        api_key=settings.api_football_key,
        base_url=settings.api_football_base_url,
        http_proxy="",
        provider=PROVIDER_API_FOOTBALL,
    )

    proxy_left = None
    proxy_url = (settings.api_football_proxy or "").strip()
    proxy_key = (settings.api_football_proxy_api_key or "").strip()
    if proxy_url and proxy_key:
        print(
            f"  备用通道: proxy={mask_proxy_url(proxy_url)} "
            f"base_url={(settings.api_football_proxy_base_url or '').strip() or '(沿用主端点)'}"
        )
        proxy_left = _probe(
            "备用账号",
            api_key=proxy_key,
            base_url=settings.api_football_proxy_base_url,
            http_proxy=proxy_url,
            provider=PROVIDER_API_FOOTBALL_PROXY,
        )
    elif proxy_url or proxy_key:
        print("  备用通道未同时配置代理与备用 key (API_FOOTBALL_PROXY / "
              "API_FOOTBALL_PROXY_API_KEY), 跳过")

    known = [v for v in (main_left, proxy_left) if v is not None]
    if not known:
        print("所有通道都没查到额度, 数据库状态未更新")
        return 1
    if any(v > 0 for v in known):
        print("结论: 有通道额度可用, 请求可继续 (主额度耗尽会自动切备用通道)")
        return 0
    print("结论: 已配置通道的当日额度都已用完, 请求将快速失败 (不消耗额度)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
