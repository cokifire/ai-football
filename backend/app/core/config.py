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

    # 备用预测模型（仅作为第二组 -m，仍走 codex；可选）
    prediction_fallback_llm_model: str = ""

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
