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

    # 情报整理 LLM（OpenAI-compatible）
    intelligence_llm_api_key: str = ""
    intelligence_llm_base_url: str = "https://apihub.agnes-ai.com/v1"
    intelligence_llm_model: str = "agnes-2.5-flash"

    # 主预测 LLM（OpenAI-compatible）
    prediction_llm_api_key: str = ""
    prediction_llm_base_url: str = "https://api.deepseek.com/v1"
    prediction_llm_model: str = "deepseek-chat"

    # 备用预测 LLM（主模型请求失败或输出不合规时自动切换；可选）
    prediction_fallback_llm_api_key: str = ""
    prediction_fallback_llm_base_url: str = ""
    prediction_fallback_llm_model: str = ""

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
