from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: str = Field(default="change-me")
    api_bind_host: str = Field(default="0.0.0.0")
    api_bind_port: int = Field(default=8000)

    database_url: str = Field(
        default="postgresql+psycopg://pliny:pliny@localhost:5432/pliny",
    )

    neo4j_uri: str = Field(default="bolt://localhost:7687")
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: str = Field(default="")

    rustfs_endpoint: str = Field(default="http://localhost:9000")
    rustfs_access_key: str = Field(default="")
    rustfs_secret_key: str = Field(default="")
    rustfs_bucket: str = Field(default="pliny")

    blob_root: str = Field(default="./.blob-dev")

    openai_api_key: str = Field(default="")
    openai_rpm: int = Field(default=60)
    openai_tpm: int = Field(default=90000)
    openai_daily_usd_cap: float = Field(default=20.0)

    current_embedding_model: str = Field(default="text-embedding-3-small")
    embedding_model_version: str = Field(default="1")

    telegram_bot_token: str = Field(default="")
    telegram_allowed_user_ids: str = Field(default="")
    pliny_api_base_url: str = Field(default="http://localhost:8000")

    fast_worker_concurrency: int = Field(default=4)
    slow_worker_concurrency: int = Field(default=2)
    stage_timeout_seconds: int = Field(default=900)

    cors_allowed_origins: str = Field(default="")


@lru_cache
def get_settings() -> Settings:
    return Settings()
