"""查询 Firecrawl 当前额度是否用完, 并把 api_status 表同步成真实状态。

用法 (在 backend/ 目录下运行):
    python tools/check_firecrawl_quota.py

做的事: 调 Firecrawl 托管套餐的 credit-usage 接口拿剩余点数, 然后
    remaining <= 0 -> api_status 对应行标记 quota_exhausted=1
                      (之后情报抓取快速失败, 不再拿 402 试探)
    remaining > 0  -> 复位该行 (额度恢复/套餐升级后不必等每月重置日)
    接口不可用      -> 不动数据库 (自托管实例没有该端点), 被动识别继续兜底

主账号与备用(代理)账号分开查、分开记账 —— 两行 provider:
    firecrawl        -> 主 key
    firecrawl-proxy  -> 备用 key (经代理通道), 未配置代理则跳过
两个账号额度与重置日互不相干, 分两行才不会互相顶掉。

退出码: 0=至少一个账号额度可用, 2=所有已配置账号都耗尽, 1=查询失败(库未更新)
"""
import sys
from pathlib import Path

# 让脚本在 backend/ 下以 python tools/xxx.py 运行时也能 import app 包
# (直接运行脚本时 sys.path[0] 是 tools/ 目录, 而非 backend/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from tools.fetch_intelligence import (
    PROVIDER_FIRECRAWL,
    PROVIDER_FIRECRAWL_PROXY,
    _mask_proxy_url,
    refresh_firecrawl_quota_status,
)


def _probe(label, *, api_key, base_url, http_proxy, provider):
    """查一个账号; 返回剩余点数 (查不动返回 None)。"""
    result = refresh_firecrawl_quota_status(
        api_key=api_key,
        firecrawl_base_url=base_url,
        http_proxy=http_proxy,
        provider=provider,
    )
    remaining = result.get("remaining_credits")
    plan = result.get("plan_credits")
    period = f"{result.get('billing_period_start')} ~ {result.get('billing_period_end')}"

    if result.get("status") == "exhausted":
        print(f"[{label}] 额度已用完: remaining={remaining}/{plan} (账期 {period})")
        print(f"  api_status provider={provider} 已标记 quota_exhausted=1")
        return remaining
    if result.get("status") == "available":
        print(f"[{label}] 额度可用: remaining={remaining}/{plan} (账期 {period})")
        print(f"  api_status provider={provider} 已复位为 quota_exhausted=0")
        return remaining
    print(f"[{label}] 查询失败, 数据库状态未更新: {result.get('error') or '未知错误'}")
    return None


def main() -> int:
    proxy_key = (settings.firecrawl_proxy_api_key or "").strip()
    proxy_http = (settings.firecrawl_proxy or "").strip()
    proxy_base = (settings.firecrawl_proxy_base_url or "").strip()

    main_left = _probe(
        "主账号",
        api_key=settings.firecrawl_api_key,
        base_url=settings.firecrawl_base_url,
        http_proxy="",
        provider=PROVIDER_FIRECRAWL,
    )

    proxy_left = None
    if proxy_key or proxy_http:
        print(
            f"  备用通道: proxy={_mask_proxy_url(proxy_http) or '(不走代理)'} "
            f"base_url={proxy_base or '(沿用主端点)'}"
        )
        proxy_left = _probe(
            "备用账号",
            api_key=proxy_key or settings.firecrawl_api_key,
            base_url=proxy_base or settings.firecrawl_base_url,
            http_proxy=proxy_http,
            provider=PROVIDER_FIRECRAWL_PROXY,
        )

    known = [v for v in (main_left, proxy_left) if v is not None]
    if not known:
        print("所有账号都没查到额度, 数据库状态未更新")
        return 1
    if any(v > 0 for v in known):
        print("结论: 有账号额度可用, 情报抓取可继续")
        return 0
    print("结论: 已配置账号的额度都已用完, 情报抓取将快速失败 (不消耗额度)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
