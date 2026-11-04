from functools import lru_cache
from pathlib import Path
import os

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentUsageOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    conversation_tokens: int | None = Field(default=None, ge=1, le=10**12)
    sender_tokens: int | None = Field(default=None, ge=1, le=10**12)
    business_tokens: int | None = Field(default=None, ge=1, le=10**12)
    alert_percent: int | None = Field(default=None, ge=1, le=100)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Escalar AI POS API"
    environment: str = "development"
    database_url: str = "sqlite+pysqlite:///./impulsa_pos.db"
    cors_origins: str = (
        "http://localhost:5173,http://localhost:5174,"
        "https://panelempresa.vercel.app,https://panelclientes-k8wt.vercel.app"
    )
    supabase_url: str | None = None
    supabase_jwt_secret: str | None = None
    supabase_jwks_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_owner_provision_function: str = "provision-pos-owner"
    invite_redirect_url: str = "http://localhost:5173/invitacion"
    public_api_base_url: str | None = None
    pos_public_base_url: str | None = None
    device_auth_secret: str | None = None
    auth_admin_secret: str | None = None
    integration_service_token: str | None = None
    dev_auth_token: str | None = None
    legacy_public_reads_enabled: bool = False
    auto_create_schema: bool = True
    upload_dir: Path = Field(default=Path("./uploads"))
    payment_vision_model: str = "gpt-4.1-mini"
    openai_api_key: str | None = None
    agent_messages_per_minute: int = Field(default=30, ge=3, le=300)
    agent_messages_per_hour: int = Field(default=120, ge=3, le=3000)
    agent_media_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=8 * 1024 * 1024)
    agent_media_max_pixels: int = Field(default=25_000_000, ge=1, le=25_000_000)
    agent_media_conversation_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=10**12)
    agent_media_conversation_files: int = Field(default=64, ge=1, le=10000)
    agent_media_branch_bytes: int = Field(default=256 * 1024 * 1024, ge=1024, le=10**12)
    agent_media_branch_files: int = Field(default=1000, ge=1, le=100000)
    agent_media_cleanup_batch: int = Field(default=4, ge=1, le=32)
    agent_usage_conversation_tokens: int = Field(default=1_000_000, ge=1, le=10**12)
    agent_usage_sender_tokens: int = Field(default=2_000_000, ge=1, le=10**12)
    agent_usage_business_tokens: int = Field(default=50_000_000, ge=1, le=10**12)
    agent_usage_alert_percent: int = Field(default=80, ge=1, le=100)
    agent_usage_business_overrides: dict[str, AgentUsageOverride] = Field(default_factory=dict)

    @field_validator("agent_usage_conversation_tokens", "agent_usage_sender_tokens", "agent_usage_business_tokens", "agent_usage_alert_percent", mode="before")
    @classmethod
    def strict_usage_integer(cls, value):
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError("Usage limits must be positive integer values")
        return value

    @field_validator("agent_usage_business_overrides")
    @classmethod
    def usage_business_keys(cls, value):
        if any(not key.isascii() or not key.isdigit() or key.startswith("0") for key in value):
            raise ValueError("Usage override keys must be canonical positive business IDs")
        return value

    @property
    def is_development(self) -> bool:
        return self.environment.lower() in {"development", "dev", "test"}

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def normalized_database_url(self) -> str:
        value = self.database_url
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg2://", 1)
        if value.startswith("postgresql://") and "+" not in value.split(":", 1)[0]:
            return value.replace("postgresql://", "postgresql+psycopg2://", 1)
        return value

    @property
    def jwks_url(self) -> str | None:
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        if self.supabase_url:
            return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        return None


@lru_cache
def get_settings() -> Settings:
    # Isolated rehearsals must not inherit private .env files from the checkout.
    return Settings(_env_file=None) if os.environ.get("ESCALAR_ENV_FILE_DISABLED") == "true" else Settings()
