"""集中管理環境設定。

本範本採 12-factor style：程式碼只讀設定，不在 code 裡硬寫 production secret。

本機：
    .env

Kubernetes：
    ConfigMap + Secret -> environment variables

Pydantic Settings 會把環境變數名稱自動對應到欄位，例如：

    DATABASE_URL -> database_url
    A2A_PUBLIC_URL -> a2a_public_url
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Agent Runtime 所有可配置參數。

    注意哪些設定屬於哪一層：

    API layer
        api_host / api_port

    persistence
        database_url

    task queue
        redis_url

    Agent SDK
        claude_model / claude_max_turns / claude_project_key

    A2A discovery
        a2a_public_url
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "dev"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # 同一 PostgreSQL instance 目前同時承載：
    #   - application tables
    #   - A2A DatabaseTaskStore
    #   - LangGraph checkpoint/store
    #   - Claude Agent SDK SessionStore
    # Production 可依容量/治理需求拆成不同 database/schema/service。
    database_url: str = "postgresql://agent:agent@localhost:5432/agent"

    # Redis 只拿來做 Celery broker/result backend，不是 durable workflow source of truth。
    redis_url: str = "redis://localhost:6379/0"

    # Claude SDK 會從環境取得 API key；請用 Kubernetes Secret 管理。
    anthropic_api_key: str | None = None
    claude_model: str = "claude-sonnet-4-5"
    claude_max_turns: int = 8

    # 用來區隔 Claude Agent SDK native sessions 的 project namespace。
    claude_project_key: str = "agent-runtime"

    # 這個 URL 會被寫進 Agent Card，其他 Agent 會真的照這個位址呼叫。
    # K8s deployment 時必須換成 ingress/gateway 對外可達的 URL，不能留 localhost。
    a2a_public_url: str = "http://localhost:8000/a2a"


@lru_cache
def get_settings() -> Settings:
    """每個 process 快取一份 Settings。

    API process 與 Celery worker 是不同 process，因此各自會有自己的 cache instance。
    """
    return Settings()
