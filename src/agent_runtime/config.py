from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "dev"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    database_url: str = "postgresql://agent:agent@localhost:5432/agent"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: str | None = None
    claude_model: str = "claude-sonnet-4-5"
    claude_max_turns: int = 8
    claude_project_key: str = "agent-runtime"

    a2a_public_url: str = "http://localhost:8000/a2a"


@lru_cache
def get_settings() -> Settings:
    return Settings()
