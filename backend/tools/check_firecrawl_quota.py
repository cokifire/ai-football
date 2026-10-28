"""查询 Firecrawl 当前额度是否用完, 并把 api_status 表同步成真实状态。

用法 (在 backend/ 目录下运行):
    python tools/check_firecrawl_quota.py

做的事: 调 Firecrawl 托管套餐的 credit-usage 接口拿剩余点数, 然后
    remaining <= 0 -> api_status.provider=firecrawl 标记 quota_exhausted=1
                      (之后情报抓取直接快速失败, 不再拿 402 试探)
    remaining > 0  -> 复位该行 (额度恢复/套餐升级后不必等每月重置日)
    接口不可用      -> 不动数据库 (自托管实例没有该端点), 被动识别继续兜底

退出码: 0=额度可用, 2=额度已用完, 1=查询失败(数据库状态未更新)
"""
import sys
from pathlib import Path

# 让脚本在 backend/ 下以 python tools/xxx.py 运行时也能 import app 包
# (直接运行脚本时 sys.path[0] 是 tools/ 目录, 而非 backend/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from tools.fetch_intelligence import refresh_firecrawl_quota_status


def main() -> int:
    result = refresh_firecrawl_quota_status(
        api_key=settings.firecrawl_api_key,
        firecrawl_base_url=settings.firecrawl_base_url,
    )

    status = result.get("status")
    remaining = result.get("remaining_credits")
    plan = result.get("plan_credits")
    period = f"{result.get('billing_period_start')} ~ {result.get('billing_period_end')}"

    if status == "exhausted":
        print(f"Firecrawl 额度已用完: remaining={remaining}/{plan} (账期 {period})")
        print("api_status 表 provider=firecrawl 已标记 quota_exhausted=1, 情报抓取将快速失败")
        return 2
    if status == "available":
        print(f"Firecrawl 额度可用: remaining={remaining}/{plan} (账期 {period})")
        print("api_status 表 provider=firecrawl 已复位为 quota_exhausted=0")
        return 0

    print(f"Firecrawl 额度查询失败, 数据库状态未更新: {result.get('error') or '未知错误'}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
