from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    integration_service_token: str | None = None
    dev_auth_token: str | None = None
    legacy_public_reads_enabled: bool = True
    auto_create_schema: bool = True
    upload_dir: Path = Field(default=Path("./uploads"))
    payment_vision_model: str = "gpt-4.1-mini"
    openai_api_key: str | None = None

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
    return Settings()
