"""Repo-root conftest.py — pytest-wide test fixtures and env setup.

Sets SAEBOOKS_WEB_SITE_ORIGIN to ``http://test`` so the OriginRefererMiddleware
treats requests from the AsyncClient ``base_url="http://test"`` as same-origin.
This lets the existing ~270 write-path tests continue to send POST/PUT/PATCH/
DELETE without explicitly forging an Origin header on every call.

In production the env var defaults to https://books-dev.sauer.com.au, so this
override only affects the test process.

CSRF Layer 3 — test-suite cooperation
-------------------------------------
Layer 3 (per-form CSRF token) enforces a token in every state-changing form
body that matches ``request.session['csrf_token']``.  None of the existing
~270 write-path tests set such a token, and patching every ``data={...}``
dict by hand would be a 200-file edit of mostly-mechanical changes.

Instead, this conftest does TWO things at module scope:

1. Patch ``itsdangerous`` cookie creation so any session cookie built via
   the per-test ``_make_session_cookie`` helpers (a copy-paste pattern
   across test files) automatically carries a fixed ``csrf_token``.
   We achieve this without touching the helpers by intercepting at the
   layer below — we wrap ``TimestampSigner.sign`` to detect saebooks-web
   session payloads and inject the token before signing.

2. Patch ``httpx.AsyncClient.send`` to detect urlencoded POST/PUT/PATCH/
   DELETE bodies destined for the ASGI app and add the fixed
   ``csrf_token`` form field if it's not already present.

Together these make every legacy test pass Layer 3 without code changes.
The fixed token is a constant in this file so any new test that wants to
exercise mismatch behaviour can submit a different value or override the
session key directly.

This patching is test-process-only (it relies on env vars set here) and
the CSRF middleware in saebooks_web.security has no awareness of pytest;
production semantics are unchanged.
"""
from __future__ import annotations

import json
import os
from base64 import b64decode, b64encode
from urllib.parse import parse_qsl, urlencode

# Set BEFORE any saebooks_web modules are imported (this conftest runs first).
os.environ.setdefault("SAEBOOKS_WEB_SITE_ORIGIN", "http://test")


# ---------------------------------------------------------------------------
# Fixed CSRF token used by all tests.
#
# Real tokens are 32-byte URL-safe random strings (~43 chars); this fixed one
# is the same shape but a constant.  The CSRF middleware only checks string
# equality (via secrets.compare_digest), so a constant is fine here.
# ---------------------------------------------------------------------------
TEST_CSRF_TOKEN = "test-csrf-token-fixed-43chars-1234567890abc"


# ---------------------------------------------------------------------------
# Patch 1 — TimestampSigner.sign injects csrf_token into the session payload.
#
# Test files build session cookies like this::
#
#     def _make_session_cookie(data: dict) -> str:
#         signer = TimestampSigner(settings.secret_key)
#         payload = b64encode(json.dumps(data).encode("utf-8"))
#         return signer.sign(payload).decode("utf-8")
#
# When ``signer.sign(payload)`` is called with a base64-encoded JSON payload
# that looks like a saebooks-web session (i.e. has ``api_token``), we decode,
# add ``csrf_token``, and re-encode.  Other uses of TimestampSigner are
# untouched — the JSON-detection guards us against false positives.
# ---------------------------------------------------------------------------


def _maybe_inject_csrf(payload: bytes) -> bytes:
    """Return payload with csrf_token added if it looks like a session cookie."""
    try:
        decoded = b64decode(payload)
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return payload
    if not isinstance(data, dict):
        return payload
    # Only inject for saebooks-web session payloads (have api_token).
    if "api_token" not in data:
        return payload
    if "csrf_token" in data:
        return payload  # caller already set one — don't override
    data["csrf_token"] = TEST_CSRF_TOKEN
    return b64encode(json.dumps(data).encode("utf-8"))


def _patch_itsdangerous() -> None:
    from itsdangerous import TimestampSigner

    if getattr(TimestampSigner.sign, "_saebooks_csrf_test_patched", False):
        return

    _orig_sign = TimestampSigner.sign

    def _patched_sign(self, value):  # type: ignore[no-untyped-def]
        if isinstance(value, bytes):
            value = _maybe_inject_csrf(value)
        elif isinstance(value, str):
            try:
                value = _maybe_inject_csrf(value.encode("ascii"))
            except Exception:
                pass
        return _orig_sign(self, value)

    _patched_sign._saebooks_csrf_test_patched = True  # type: ignore[attr-defined]
    TimestampSigner.sign = _patched_sign  # type: ignore[method-assign]


_patch_itsdangerous()


# ---------------------------------------------------------------------------
# Patch 2 — httpx AsyncClient.send injects csrf_token into urlencoded bodies
# for state-changing methods, when the body doesn't already contain one.
#
# Triggered for every request the tests send via AsyncClient.  We don't
# scope to ASGITransport because all our tests use it and a generic patch
# is simpler; non-ASGI requests with urlencoded bodies are extremely rare
# in this test suite.
# ---------------------------------------------------------------------------


def _patch_httpx() -> None:
    import httpx

    if getattr(httpx.AsyncClient.send, "_saebooks_csrf_test_patched", False):
        return

    _STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
    _orig_send = httpx.AsyncClient.send
    _orig_build_request = httpx.AsyncClient.build_request

    # Hook 1 — build_request injects csrf_token into multipart bodies.
    #
    # When tests call ``client.post(url, files={...})`` without ``data=``,
    # httpx serialises a multipart/form-data body with only the file part.
    # The CSRFMiddleware skips multipart (handler calls verify_csrf_form),
    # but verify_csrf_form needs ``csrf_token`` in the parsed form.  We
    # inject it here so multipart calls behave like urlencoded ones below.
    def _patched_build_request(self, method, url, *, content=None, data=None,
                                files=None, json=None, params=None,
                                headers=None, cookies=None, timeout=...,
                                extensions=None):  # type: ignore[no-untyped-def]
        if (
            method.upper() in _STATE_CHANGING
            and files is not None  # multipart path
            and (data is None or "csrf_token" not in data)
        ):
            data = dict(data) if data else {}
            data["csrf_token"] = TEST_CSRF_TOKEN
        kwargs = {}
        if timeout is not ...:
            kwargs["timeout"] = timeout
        return _orig_build_request(
            self, method, url, content=content, data=data, files=files,
            json=json, params=params, headers=headers, cookies=cookies,
            extensions=extensions, **kwargs,
        )

    _patched_build_request._saebooks_csrf_test_patched = True  # type: ignore[attr-defined]
    httpx.AsyncClient.build_request = _patched_build_request  # type: ignore[method-assign]

    # Hook 2 — send injects csrf_token into urlencoded bodies post-build.
    #
    # Triggered for every request the tests send via AsyncClient.  We don't
    # scope to ASGITransport because all our tests use it and a generic patch
    # is simpler; non-ASGI requests with urlencoded bodies are extremely rare
    # in this test suite.
    async def _patched_send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        try:
            method = request.method.upper()
            if method in _STATE_CHANGING:
                content_type = request.headers.get("content-type", "").lower()
                if content_type.startswith("application/x-www-form-urlencoded"):
                    body = request.content or b""
                    decoded = body.decode("utf-8", errors="replace")
                    pairs = parse_qsl(decoded, keep_blank_values=True)
                    keys = {k for k, _ in pairs}
                    if "csrf_token" not in keys:
                        pairs.append(("csrf_token", TEST_CSRF_TOKEN))
                        new_body = urlencode(pairs).encode("utf-8")
                        # httpx Request body lives in two places: the bytes
                        # buffer (request._content) and the AsyncByteStream
                        # the ASGI transport iterates on (request.stream).
                        # Both must be updated, plus Content-Length, or the
                        # ASGI app will receive the stale body.
                        from httpx import ByteStream
                        request._content = new_body
                        request.stream = ByteStream(new_body)
                        request.headers["content-length"] = str(len(new_body))
        except Exception:
            # Any failure here must not break the test — fall through to
            # the original send.  Tests that exercise CSRF rejection will
            # still work because they bypass this helper.
            pass
        return await _orig_send(self, request, **kwargs)

    _patched_send._saebooks_csrf_test_patched = True  # type: ignore[attr-defined]
    httpx.AsyncClient.send = _patched_send  # type: ignore[method-assign]


_patch_httpx()
