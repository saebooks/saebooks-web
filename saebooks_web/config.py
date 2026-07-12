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
    session_https_only: bool = False
    """Set True when deployed behind a TLS-terminating reverse proxy (prod).

    Controls the Secure flag on the session cookie.  Default False for local
    dev (plain HTTP).  Override with SAEBOOKS_WEB_SESSION_HTTPS_ONLY=true.
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

    # -----------------------------------------------------------------
    # Outbound email / customer comms (owned by the web app as of engine
    # #32).  The accounting engine's outbound-email POLICY (two-key kill
    # switch + Outlook draft mode) and its two transports (SMTP send,
    # Microsoft Graph draft) are ported into ``saebooks_web.comms``.
    #
    # Env var names are deliberately UNPREFIXED (bare, via ``alias``) so
    # they match the accounting engine's own email settings and the shared
    # compose environment — the engine used the same names before the
    # extraction.  Everything defaults CLOSED: no token (dev-open like
    # render), send disabled, draft mode off, no transport configured.
    # -----------------------------------------------------------------
    comms_token: str = Field(default="", alias="COMMS_TOKEN")
    """Shared secret for the /internal/comms/send endpoint.  When non-empty
    the engine must present it as ``X-Comms-Token`` (constant-time compared).
    Empty → dev mode, endpoint is open (rely on network isolation)."""

    # --- SMTP transport (the "sent" path) — ported from mailer.py ---
    smtp_host: str = Field(default="", alias="SMTP_HOST")
    """SMTP relay host.  Empty → the SMTP transport falls back to writing
    an .eml into ``mail_outbox_dir`` (dev), and the send policy treats an
    empty host as "not configured" → blocked (never a false "sent")."""
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="", alias="SMTP_FROM")
    """Default envelope From when the caller does not supply meta.from."""
    smtp_tls: bool = Field(default=True, alias="SMTP_TLS")
    mail_outbox_dir: str = Field(
        default="/tmp/saebooks-comms-outbox", alias="SAEBOOKS_MAIL_OUTBOX_DIR"
    )
    """Dev outbox dir used by the SMTP transport when smtp_host is empty."""

    # --- Resend API transport (the customer_doc "sent" path) ---
    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")
    """Resend API key.  Empty → customer_doc sends are BLOCKED (exactly like
    the engine's original customer_email: no key, no send)."""
    resend_api_url: str = Field(
        default="https://api.resend.com", alias="RESEND_API_URL"
    )
    """Resend API base URL (no trailing slash)."""

    # --- Microsoft Graph draft transport (the "drafted" path) ---
    graph_tenant_id: str = Field(default="", alias="GRAPH_TENANT_ID")
    graph_client_id: str = Field(default="", alias="GRAPH_CLIENT_ID")
    graph_client_secret: str = Field(default="", alias="GRAPH_CLIENT_SECRET")
    graph_draft_mailbox: str = Field(default="", alias="GRAPH_DRAFT_MAILBOX")

    # --- Two-key kill switch (the POLICY) ---
    customer_email_send_enabled: bool = Field(
        default=False, alias="SAEBOOKS_EMAIL_SEND_ENABLED"
    )
    """Key 1 — a real SMTP send happens ONLY when this is explicitly true
    AND draft mode is off.  Default false (fail-closed)."""
    customer_email_draft_mode: bool = Field(
        default=False, alias="SAEBOOKS_EMAIL_DRAFT_MODE"
    )
    """Key 2 — when true, EVERY message is parked as a Microsoft Graph draft
    for human review instead of being sent.  Overrides the send key: while
    draft mode is on, nothing is ever sent.  Default false."""


settings = Settings()
