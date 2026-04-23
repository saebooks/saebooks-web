# saebooks-web

Thin server-side-rendered web frontend for [saebooks-api](../saebooks/).
Built on FastAPI + Jinja2 + HTMX — no Node.js, no SPA framework, no build step.

## What it is

`saebooks-web` is a **dumb proxy with templates**. It:

1. Holds the user's session (signed cookie via `itsdangerous`)
2. Proxies all data requests to `saebooks-api` via `httpx`, injecting the bearer token
3. Renders Jinja2 templates server-side, with HTMX fragments for interactivity

The web server never talks to the database directly. All business logic lives in
`saebooks-api`.

## Requirements

- Python 3.12+
- A running `saebooks-api` instance (default: `http://localhost:8042`)
- The API token set via `SAEBOOKS_DEV_API_TOKEN` on the API server

## Running in development

```bash
# Install dependencies
uv sync --extra dev

# Run (hot-reload)
uv run uvicorn saebooks_web.main:app --reload --port 8043
```

Then open `http://localhost:8043`. The login page asks for the API token
(`SAEBOOKS_DEV_API_TOKEN` from the API server's environment, or its startup log).

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SAEBOOKS_WEB_API_URL` | `http://localhost:8042` | Base URL of saebooks-api |
| `SAEBOOKS_WEB_SECRET_KEY` | `dev-insecure-change-me-before-prod` | Session signing key — **must** be changed in production |
| `SAEBOOKS_WEB_PORT` | `8043` | Port to bind to |
| `SAEBOOKS_WEB_HOST` | `0.0.0.0` | Bind address |
| `SAEBOOKS_WEB_DEBUG` | `false` | Enable debug mode |
| `SAEBOOKS_WEB_LOG_LEVEL` | `INFO` | Log level |

Generate a production secret key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Running tests

```bash
uv run pytest
```

## Project layout

```
saebooks_web/
  main.py          FastAPI app + middleware wiring
  config.py        Pydantic-settings (env vars)
  auth.py          /login, /logout routes
  api_client.py    httpx wrapper (injects bearer token from session)
  routes/
    contacts.py    GET /contacts — first HTMX view
templates/
  base.html        Layout + HTMX CDN + Tailwind CDN
  auth/login.html  Login form
  contacts/list.html  Contacts table
tests/
  test_smoke.py    /healthz + contacts render (respx-mocked API)
```

## Architecture note

Auth model (Phase 0): the login form accepts a raw API bearer token.
This will be replaced with email+password → portal JWT exchange once
Lane A portal auth lands (TODO in `auth.py`).

See `~/.claude/plans/saebooks-api-rebuild.md` — Phase 2 / Lane D for full context.
