# SAE Books Web — multi-stage production Dockerfile
#
# Multi-arch: linux/amd64 and linux/arm64 are Tier-1 published binaries.
# linux/riscv64 is Tier-2 (best-effort, no SLA); known-buildable via QEMU
# on the saebooks buildx builder but takes 3-5× longer per build due to QEMU.
# To include riscv64 add it to --platform on the buildx call.
#
# This image runs the Jinja2 + HTMX thin web frontend. It does NOT talk to
# the database directly — all data access goes through saebooks-api (REST).
#
# Required environment variable:
#   SAEBOOKS_API_URL — base URL of the saebooks-api service, e.g.
#                      http://api:8000 (in compose) or https://books.example.com
#   SAEBOOKS_WEB_SECRET_KEY — 32+ byte random hex for session cookies

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1: builder — install all deps into a venv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# gcc is needed by some optional C extensions (e.g. multidict inside httpx
# extras). The web frontend has no Postgres dep so libpq-dev is not needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "${VIRTUAL_ENV}"

WORKDIR /build

# Dependency manifest first for layer-cache efficiency.
COPY pyproject.toml README.md ./

RUN pip install --upgrade pip setuptools wheel \
    && pip install .

# Source copy after deps.
COPY saebooks_web/ ./saebooks_web/
COPY templates/ ./templates/

RUN pip install --no-deps .

# ---------------------------------------------------------------------------
# Stage 2: runtime — no compiler toolchain
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# curl for HEALTHCHECK only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user.
RUN groupadd --system saebooks \
    && useradd --system --gid saebooks --no-create-home saebooks

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY --chown=saebooks:saebooks saebooks_web/ ./saebooks_web/
# Top-level templates/ directory — Jinja2 ChoiceLoader looks here first,
# allowing theme overrides outside the package tree.
COPY --chown=saebooks:saebooks templates/ ./templates/

USER saebooks

# Web frontend listens on 8080 in the SAP compose bundle (API is on 8000).
# The app's internal default is 8043 (dev); SAP overrides via env:
#   SAEBOOKS_WEB_PORT=8080 (set in compose)
EXPOSE 8080

# Healthcheck hits /healthz — unauthenticated liveness probe defined in main.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["uvicorn", "saebooks_web.main:app", "--host", "0.0.0.0", "--port", "8080"]
