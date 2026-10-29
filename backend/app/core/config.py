from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 数据库
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "football"

    # API-Football
    api_football_key: str = ""
    api_football_base_url: str = ""
    # API-Football key 的滚动窗口限额；免费套餐通常为 10 次/分钟。
    api_football_rate_limit: int = 10
    api_football_rate_window: float = 60.0

    # 天气数据（OpenWeatherMap）
    openweathermap_api_key: str = ""

    # 外部比赛情报（Firecrawl）
    firecrawl_api_key: str = ""
    firecrawl_base_url: str = "http://127.0.0.1:3002"
    # Firecrawl 托管套餐按月配额（当前账号每月 1000 次调用），不是按天。
    # 额度在每月该日（1-28）重置；闩锁必须撑到重置日才自动放行，
    # 否则会每天放行一次、反复重试到再次耗尽。
    # 默认 22：2026-10-28 当天从 credit-usage 接口两次读到的账期分别是
    # 10-09~11-09（9 号重置，remaining=-989）和 10-22~11-22（22 号重置，
    # remaining=0）—— 账号在当天重置过周期（疑似购买额度），22 号 20:45 UTC
    # 换算 UTC+8 正是 23 号凌晨。这个日子会随续费/购买变动，拿不准就跑
    # tools/check_firecrawl_quota.py 看真实账期，再改 backend/.env。
    firecrawl_quota_reset_day: int = 22
    # Firecrawl 代理/备用通道: 主额度 (月度配额) 耗尽时, 经这里 + 备用 key
    # 抓情报, 不影响主 key 的状态记账。三个都留空 = 不启用, 保持原来的快速
    # 失败行为; 任一非空即启用代理通道, 未填的项沿用主配置。
    #   firecrawl_proxy        HTTP 代理, 形如 http://username:password@ip:port
    #                          (SDK 的 requests 调用不接受 proxies 参数, 代理
    #                           路径改走自带适配器直连 REST 端点)
    #   firecrawl_proxy_base_url  代理要访问的 firecrawl 端点 (留空=沿用主)
    #   firecrawl_proxy_api_key   备用 API key (留空=沿用主 key)
    firecrawl_proxy: str = ""
    firecrawl_proxy_base_url: str = ""
    firecrawl_proxy_api_key: str = ""
    # 备用账号的重置日 (与主账号分开配: 实测主账号 11-22、备用账号 11-09,
    # 两个账期不同, 共用一个日子会让备用通道白等或早放行)。
    firecrawl_proxy_quota_reset_day: int = 9

    # ── 情报整理 / 主预测 / 备用预测：经 codex CLI 调用的第三方大模型 ──
    # 不再直接 HTTP 调 /chat/completions；这三个字段的用途变为：
    #   *_model    -> codex exec 的 -m 模型名（须为 codex/provider 支持的模型）
    #   *_base_url -> 非空时用 -c 覆盖 ~/.codex/config.toml 的 model_providers；
    #                 为空则沿用服务器 codex 配置里的 provider
    #   *_api_key  -> 只通过环境变量注入 codex 子进程（变量名见 codex_api_key_env），
    #                 绝不进命令行
    intelligence_llm_api_key: str = ""
    intelligence_llm_base_url: str = ""
    intelligence_llm_model: str = ""

    prediction_llm_api_key: str = ""
    prediction_llm_base_url: str = ""
    prediction_llm_model: str = ""
    # 为 true 时，主预测模型直接使用 codex 自身配置（~/.codex/config.toml），
    # 忽略下面的 prediction_llm_* 与 fallback 预测模型配置
    prediction_use_codex_default: bool = False

    # 备用预测模型（独立 url/key/model；可选）
    prediction_fallback_llm_model: str = ""
    prediction_fallback_llm_base_url: str = ""
    prediction_fallback_llm_api_key: str = ""

    # ── codex CLI 执行环境 ──
    codex_bin: str = "codex"          # 可执行文件名或绝对路径
    codex_home: str = ""              # CODEX_HOME；空=继承环境（默认 ~/.codex）
    # 沙箱模式：预测要跑 football-bayes skill 的脚本（复制到临时工作目录执行），
    # read-only 会让 skill 无法工作
    codex_sandbox: str = "workspace-write"
    codex_timeout_predict: int = 1200        # 单场预测（含重试）总墙钟秒数
    codex_timeout_intelligence: int = 1200  # 情报审核/清洗（含重试）总墙钟秒数
    codex_retries: int = 2                  # 失败仅对 codex 自身重试次数
    codex_skill_name: str = "football-bayes"  # prompt 中以 $<name> 触发的 skill
    codex_provider_id: str = "codex_env"      # base_url 非空时 -c 覆盖用的 provider id
    codex_api_key_env: str = "CODEX_API_KEY"  # 注入 api_key 的环境变量名（对齐 config.toml 的 env_key）
    codex_network: bool = True                # 允许联网（skill 的 S7 外部交叉验证需要）

    # 缓存
    cache_ttl: int = 3600

    # API 认证：生产环境必须通过环境变量配置随机长密钥
    read_api_key: str = ""
    admin_api_key: str = ""

    @property
    def db_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
