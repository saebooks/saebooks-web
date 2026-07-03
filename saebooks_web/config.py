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

from pydantic import Field
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
    # LaTeX / PDF rendering (owned by the web app as of engine #31/#32).
    #
    # The web app renders the six battle-tested LaTeX/Jinja templates
    # (templates/latex/*.tex.j2) and POSTs the .tex to the latex-api
    # microservice.  Env var names are deliberately UNPREFIXED (bare
    # LATEX_API_URL / LATEX_LOGO_PATH / RENDER_TOKEN) via ``alias`` so they
    # match the accounting engine's own latex settings and the shared
    # compose environment — the engine used the same names before the
    # extraction.
    # -----------------------------------------------------------------
    latex_api_url: str = Field(default="http://latex-api:8000", alias="LATEX_API_URL")
    """Base URL of the latex-api compile service (no trailing slash)."""

    latex_logo_path: str = Field(default="", alias="LATEX_LOGO_PATH")
    """Absolute path (as seen INSIDE the latex-api container) to the
    letterhead logo PNG.  Empty → templates fall back to the text
    letterhead.  Injected into every render ctx as ``logo_path``."""

    render_token: str = Field(default="", alias="RENDER_TOKEN")
    """Shared secret for the /internal/render endpoint.  When non-empty the
    engine must present it as the ``X-Render-Token`` header (constant-time
    compared).  Empty → dev mode, endpoint is open (rely on network
    isolation)."""


settings = Settings()
