# saebooks-web

Server-side-rendered web frontend for
[saebooks](https://github.com/saebooks/saebooks). FastAPI + Jinja2 +
HTMX — no Node.js, no SPA framework. CSS is compiled at image build time
using the standalone [Tailwind CSS binary](https://tailwindcss.com/blog/standalone-cli)
(no npm/Node required).

> **Status:** v0.1 — public alpha. AGPL-3.0.

## What it is

`saebooks-web` is a templated thin proxy. It:

1. Holds the user's session (signed cookie via `itsdangerous`).
2. Proxies all data requests to `saebooks` (the API) via `httpx`,
   injecting the user's bearer token.
3. Renders Jinja2 templates server-side, with HTMX fragments for
   interactivity.

The web server never talks to the database directly. All business
logic lives in the API.

## Quickstart (Docker)

The recommended way to run it is alongside the API via the top-level
[`saebooks`](https://github.com/saebooks/saebooks) Docker Compose
bundle — see that repo's README. The web container is published to
Docker Hub as `saebooks/saebooks-web`.

## Running standalone (development)

```bash
uv sync --extra dev
```

Install the standalone Tailwind binary (one-off, no Node required):

```bash
curl -fsSL https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-linux-x64 \
  -o ~/.local/bin/tailwindcss && chmod +x ~/.local/bin/tailwindcss
```

In one terminal, watch for CSS changes:

```bash
./scripts/build_css.sh --watch
```

In another terminal, start the app:

```bash
uv run uvicorn saebooks_web.main:app --reload --port 8080
```

Then open <http://localhost:8080>. You will need a running API
instance to point at (default: `http://localhost:8042`).

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SAEBOOKS_API_URL` | `http://localhost:8042` | Base URL of the API |
| `SAEBOOKS_WEB_SECRET_KEY` | *(none — required)* | Session signing key. Generate with `openssl rand -base64 32`. |
| `SAEBOOKS_WEB_PORT` | `8080` | Port to bind to |
| `SAEBOOKS_WEB_HOST` | `0.0.0.0` | Bind address |
| `SAEBOOKS_WEB_DEBUG` | `false` | Enable debug mode |
| `SAEBOOKS_WEB_LOG_LEVEL` | `INFO` | Log level |
| `SAEBOOKS_WEB_SITE_ORIGINS` | `http://localhost:8080` | Comma-separated list of trusted origins for CSRF |

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
  security/        CSRF + session helpers
  routes/          per-domain route modules
templates/         Jinja2 templates
tests/             pytest + respx
```

## Licence

AGPL-3.0. See <https://github.com/saebooks/saebooks> for the
top-level project, charter, and commercial licensing options.
