"""Application settings loaded from environment variables.

All variables are prefixed ``SAEBOOKS_WEB_``.

Required in production
----------------------
SESSION_SECRET_KEY
    32+ random bytes, base64 or hex.  Generate with:
    ``python -c "import secrets; print(secrets.token_hex(32))"``

Optional (have sensible defaults for local dev)
-----------------------------------------------
SAEBOOKS_API_URL
    Base URL of the saebooks-api process.  Default: http://localhost:8042
SAEBOOKS_WEB_PORT
    Port the web process listens on.  Default: 8043
SAEBOOKS_WEB_HOST
    Bind address.  Default: 0.0.0.0
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SAEBOOKS_WEB_",
        env_file=".env",
        env_file_encoding="utf-8",
        # Allow SAEBOOKS_API_URL without the prefix (explicit override).
        extra="ignore",
    )

    # -----------------------------------------------------------------
    # API back-end
    # -----------------------------------------------------------------
    api_url: str = "http://localhost:8042"
    """Base URL for the saebooks-api (no trailing slash)."""

    # -----------------------------------------------------------------
    # Session
    # -----------------------------------------------------------------
    secret_key: str = "dev-insecure-change-me-before-prod"
    """Secret key for itsdangerous signed session cookies.

    Must be changed in production via SAEBOOKS_WEB_SECRET_KEY env var.
    """

    session_cookie_name: str = "saebooks_web_session"
    session_max_age: int = 60 * 60 * 8  # 8 hours
    session_https_only: bool = True
    """Controls the Secure flag on the session cookie.

    Default True (Secure flag set) for any TLS-terminating deployment.
    Override with SAEBOOKS_WEB_SESSION_HTTPS_ONLY=false for local HTTP dev only.
    """

    # -----------------------------------------------------------------
    # Server
    # -----------------------------------------------------------------
    port: int = 8043
    host: str = "0.0.0.0"

    # -----------------------------------------------------------------
    # Misc
    # -----------------------------------------------------------------
    debug: bool = False
    log_level: str = "INFO"

    # -----------------------------------------------------------------
    # Launch promo — must match the API setting.
    # When true the signup page shows the "first 1000 get Pro free"
    # banner. The banner hides itself when SAEBOOKS_WEB_LAUNCH_PROMO_ENABLED
    # is false so we can ship the template before activating the promo.
    # -----------------------------------------------------------------
    launch_promo_enabled: bool = False


    # -----------------------------------------------------------------
    # Startup guard
    # -----------------------------------------------------------------
    def model_post_init(self, __context: object) -> None:
        _INSECURE_PLACEHOLDER = "dev-insecure-change-me-before-prod"
        if self.secret_key == _INSECURE_PLACEHOLDER:
            raise ValueError(
                "SAEBOOKS_WEB_SECRET_KEY is still the insecure default placeholder. "
                "Set a strong random key (>= 32 bytes) via the env var before starting. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if len(self.secret_key) < 32:
            raise ValueError(
                f"SAEBOOKS_WEB_SECRET_KEY is too short ({len(self.secret_key)} chars). "
                "Minimum 32 characters required."
            )


settings = Settings()
