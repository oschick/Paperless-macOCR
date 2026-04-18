"""Application configuration via environment variables."""

from pydantic import HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Paperless-NGX
    paperless_url: HttpUrl
    paperless_token: str

    # macOCR HTTP server
    macocr_url: HttpUrl
    macocr_auth: str = ""

    # Webhook server
    host: str = "0.0.0.0"
    port: int = 9000

    # Security
    webhook_secret: str = ""

    # Logging
    log_level: str = "INFO"

    # OCR settings
    ocr_dpi: int = 300
    skip_if_text_present: bool = True


def get_settings() -> Settings:
    """Create and return application settings."""
    return Settings()
