"""Single source of truth for configuration. All env access funnels through here."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # LLM
    anthropic_api_key: str = ""
    mock_llm: bool = True
    llm_model_primary: str = "claude-sonnet-4-6"
    llm_model_fast: str = "claude-haiku-4-5-20251001"

    # Backend
    backend: str = "sqlite"
    sqlite_path: str = "./data/trip.db"

    # Approval keys (Ed25519, base64url)
    approval_signing_key: str = ""
    approval_verify_key: str = ""
    approval_kid: str = "v1"

    # Budgets
    request_max_dollars: float = 5.00
    request_max_seconds: int = 300

    # Real-API (M2)
    amadeus_client_id: str = ""
    amadeus_client_secret: str = ""
    duffel_access_token: str = ""    # Test token (duffel_test_...) from app.duffel.com
    duffel_api_version: str = "v2"
    duffel_stays_enabled: bool = False    # Stays requires Duffel sales contact to enable
    liteapi_key: str = ""    # Free tier at liteapi.travel — self-serve signup
    liteapi_use_sandbox: bool = True    # sandbox API base; flip to false for production
    openweather_api_key: str = ""
    voyage_api_key: str = ""

    # Payments (M5)
    stripe_test_key: str = ""
    booking_provider: str = "mock_always"

    # Demo
    demo_user_id: str = "demo-user-001"
    demo_user_phone: str = "+15555550100"

    # Deployment
    cors_origins_raw: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def ensure_data_dir(self) -> None:
        Path(self.sqlite_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_data_dir()
