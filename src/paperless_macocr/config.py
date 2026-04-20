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
    replace_pdf: bool = False

    # Web UI
    web_ui_enabled: bool = True

    # Web UI authentication
    # "none" = no auth, "basic" = HTTP basic, "oidc" = OpenID Connect
    web_ui_auth: str = "none"

    # Basic auth credentials (when web_ui_auth = "basic")
    web_ui_username: str = "admin"
    web_ui_password: str = ""

    # OIDC / OAuth2 (when web_ui_auth = "oidc")
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_discovery_url: str = ""
    oidc_redirect_uri: str = ""

    # Session secret for cookie signing
    session_secret: str = "change-me-in-production"  # noqa: S105

    # Tag IDs to exclude from the web UI document list (comma-separated)
    web_ui_exclude_tags: str = ""

    def get_exclude_tag_ids(self) -> list[int]:
        """Parse the comma-separated exclude tag IDs into a list."""
        if not self.web_ui_exclude_tags:
            return []
        return [int(t.strip()) for t in self.web_ui_exclude_tags.split(",") if t.strip().isdigit()]


def get_settings() -> Settings:
    """Create and return application settings."""
    return Settings()
