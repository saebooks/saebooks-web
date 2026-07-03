"""Internal LaTeX/PDF rendering service — the web app owns document presentation.

Two-repo extraction (Gitea saebooks/saebooks #31/#32): the accounting engine
is the *accountant* (it owns facts — journals, balances, tax) and the web app
is the *bookkeeper* (it owns presentation — the rendered PDF a human reads).
This module is the app side of that split: the Jinja2 LaTeX environment, the
``latex_escape`` filter, and the latex-api HTTP client are ported VERBATIM
(behaviourally) from the engine's ``saebooks/services/latex_pdf.py`` so the
six battle-tested templates render byte-for-byte the same as before.

Public surface
--------------
``POST /internal/render/{template}`` — server-to-server endpoint the engine's
new client calls.  Contract (do not deviate — the engine depends on it):

* ``{template}`` ∈ the six known template names, else 400.
* If ``RENDER_TOKEN`` is set, header ``X-Render-Token`` must match it
  (constant-time), else 401.  Empty token → dev mode, open.
* Body: a JSON object = the render ctx, passed straight to the template.
* 200 → the compiled PDF bytes (``application/pdf``).
* 422 → ``{"detail": "latex compile failed", "log_tail": <tail>}``.
* 503 → ``{"detail": "latex service unavailable"}``.

latex_escape ordering
---------------------
The ten LaTeX special characters are escaped in a single regex pass so each
source character is replaced exactly once — a sequential str.replace() chain
would re-process the backslashes it just introduced (e.g. ``&`` → ``\\&`` then
the backslash pass would mangle it).  Braces map to ``\\{`` / ``\\}`` and the
three command-style conversions (``~`` → ``\\textasciitilde{}``, ``^`` →
``\\textasciicircum{}``, ``\\`` → ``\\textbackslash{}``) carry their own braces
which are emitted by the substitution, never re-escaped, because the pass is
single-shot.
"""
from __future__ import annotations

import hmac
import logging
import re
from pathlib import Path
from typing import Any

import httpx
import jinja2
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from saebooks_web.config import settings

logger = logging.getLogger("saebooks_web.render")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LatexCompileError(Exception):
    """Raised when latex-api returns HTTP 422 (xelatex failed).

    ``log_tail`` is the last few lines of the xelatex log returned in the
    422 ``detail`` field — surfaced to the caller for diagnosis.
    """

    def __init__(self, log_tail: str) -> None:
        super().__init__(f"LaTeX compile error:\n{log_tail}")
        self.log_tail = log_tail


class LatexServiceError(Exception):
    """Raised for connection errors or unexpected responses from latex-api."""


# ---------------------------------------------------------------------------
# latex_escape filter  (ported verbatim from saebooks/services/latex_pdf.py)
# ---------------------------------------------------------------------------

# Single-pass regex for LaTeX special-character escaping.  Using sequential
# str.replace() calls is dangerous because replacements introduced in earlier
# passes (e.g. \& from &) would be re-processed by the backslash pass.  A
# single re.sub call with a function replacement processes each character
# exactly once.
_LATEX_ESCAPE_RE = re.compile(r"([&%$#_{}~^\\])")

_LATEX_CHAR_MAP: dict[str, str] = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def latex_escape(value: Any) -> str:
    """Escape a Python value for safe interpolation into LaTeX source.

    Uses a single-pass regex substitution so each source character is
    replaced exactly once; backslashes introduced by earlier replacements
    are never re-processed.
    """
    text = str(value) if not isinstance(value, str) else value
    return _LATEX_ESCAPE_RE.sub(lambda m: _LATEX_CHAR_MAP[m.group(1)], text)


# ---------------------------------------------------------------------------
# Jinja2 environment
# ---------------------------------------------------------------------------

# render.py lives in saebooks_web/; templates/latex/ is at the repo root.
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "latex"

# The six known template names.  Membership doubles as the anti-path-traversal
# gate: only these bare names reach the loader.
TEMPLATE_NAMES: frozenset[str] = frozenset(
    {
        "_preamble",
        "document",
        "quote",
        "purchase_order",
        "contact_statement",
        "statement_pack",
    }
)

_env: jinja2.Environment | None = None


def get_env() -> jinja2.Environment:
    """Return the process-wide LaTeX Jinja2 environment (lazy singleton)."""
    global _env
    if _env is None:
        _env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=False,  # LaTeX, not HTML — escaping is manual via latex_escape
            keep_trailing_newline=True,
        )
        _env.filters["latex_escape"] = latex_escape
    return _env


# ---------------------------------------------------------------------------
# Core render function  (ported from the engine's async render_latex)
# ---------------------------------------------------------------------------


async def render_latex(template: str, ctx: dict) -> bytes:
    """Render ``<template>.tex.j2`` with ``ctx`` and return PDF bytes.

    Raises
    ------
    LatexCompileError
        latex-api returned HTTP 422, carrying the xelatex log tail.
    LatexServiceError
        Connection failure, unexpected HTTP status, or missing ``pdf_url``.
    jinja2.TemplateNotFound
        The named template does not exist (should not happen — the route
        whitelists names before calling here).
    """
    latex_api_url = settings.latex_api_url

    # Inject the optional letterhead logo path (templates render the image
    # when present, the text letterhead otherwise).  A logo_path already
    # present in ctx wins over the setting.
    ctx = dict(ctx)
    ctx.setdefault("logo_path", settings.latex_logo_path or None)

    env = get_env()
    tmpl = env.get_template(f"{template}.tex.j2")
    latex_src = tmpl.render(**ctx)

    # POST the LaTeX source to latex-api /compile.
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            compile_resp = await client.post(
                f"{latex_api_url}/compile",
                json={"latex": latex_src},
            )
    except httpx.ConnectError as exc:
        raise LatexServiceError(
            f"Cannot connect to latex-api at {latex_api_url}: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise LatexServiceError(
            f"Timeout waiting for latex-api at {latex_api_url}: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise LatexServiceError(
            f"HTTP error communicating with latex-api: {exc}"
        ) from exc

    if compile_resp.status_code == 422:
        detail = compile_resp.json().get("detail", "<no log>")
        raise LatexCompileError(str(detail))

    if compile_resp.status_code != 200:
        raise LatexServiceError(
            f"latex-api /compile returned HTTP {compile_resp.status_code}: "
            f"{compile_resp.text[:500]}"
        )

    compile_body = compile_resp.json()
    pdf_url = compile_body.get("pdf_url")
    if not pdf_url:
        raise LatexServiceError(
            f"latex-api /compile response missing 'pdf_url': {compile_body}"
        )

    # GET the compiled PDF bytes.
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            pdf_resp = await client.get(f"{latex_api_url}{pdf_url}")
    except httpx.RequestError as exc:
        raise LatexServiceError(
            f"HTTP error fetching PDF from latex-api: {exc}"
        ) from exc

    if pdf_resp.status_code != 200:
        raise LatexServiceError(
            f"latex-api GET {pdf_url} returned HTTP {pdf_resp.status_code}"
        )

    return pdf_resp.content


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

router = APIRouter()


def _token_ok(request: Request) -> bool:
    """True when RENDER_TOKEN is unset (dev) or the header matches it.

    Constant-time comparison via ``hmac.compare_digest`` to avoid leaking the
    token through timing.
    """
    expected = settings.render_token
    if not expected:
        return True  # dev mode — endpoint open, rely on network isolation
    provided = request.headers.get("x-render-token", "")
    return hmac.compare_digest(provided, expected)


@router.post("/internal/render/{template}", include_in_schema=False)
async def render_document(template: str, request: Request) -> Response:
    """Render one of the six known templates to a PDF (server-to-server).

    See the module docstring for the exact contract the engine depends on.
    """
    # Auth first — never reveal template validity to an unauthenticated caller.
    if not _token_ok(request):
        return JSONResponse(
            {"detail": "invalid or missing render token"}, status_code=401
        )

    # Whitelist membership is also the anti-path-traversal gate.
    if template not in TEMPLATE_NAMES:
        return JSONResponse({"detail": "unknown template"}, status_code=400)

    try:
        ctx = await request.json()
    except Exception:
        return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
    if not isinstance(ctx, dict):
        return JSONResponse(
            {"detail": "render ctx must be a JSON object"}, status_code=400
        )

    try:
        pdf_bytes = await render_latex(template, ctx)
    except LatexCompileError as exc:
        logger.warning("render %s: latex compile failed", template)
        return JSONResponse(
            {"detail": "latex compile failed", "log_tail": exc.log_tail},
            status_code=422,
        )
    except LatexServiceError as exc:
        logger.warning("render %s: latex service unavailable (%s)", template, exc)
        return JSONResponse(
            {"detail": "latex service unavailable"}, status_code=503
        )

    return Response(content=pdf_bytes, media_type="application/pdf")
