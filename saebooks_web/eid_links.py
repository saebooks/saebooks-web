"""eID account links — maps an authenticated isikukood to a web user.

Why web-local storage
---------------------
saebooks-web is a thin client: users live in the engine's ``users`` table
and external identities in its ``oauth_provider_links`` table. The engine
does not yet expose endpoints for "link an eID identity to me" or "look up
a user by eID identity" (named as engine-lane work in the eID report), so
this wave keeps the link table web-side, in a small JSON file owned by the
web container. When the engine grows first-class eID identities this store
migrates behind the same API in ``eid_sso``.

Protection discipline (mirrors the engine's Fernet-encrypted isikukood
handling from the EE TSD work, but stronger for this use case)
----------------------------------------------------------------------
The personal code is **never stored at all** — not even encrypted:

* lookups key on ``HMAC-SHA256(k, personal_code)`` where ``k`` is derived
  from the web session secret via HKDF-style domain separation. A keyed
  MAC (not a bare hash) because the isikukood space is tiny (~date x
  serial — trivially enumerable offline); without the key an attacker who
  steals the file cannot reverse or brute-force the codes.
* the UI shows a masked form (first 3 + last 2 digits) stored alongside.
* the login flow re-derives the HMAC from the certificate-validated code
  at authentication time, so the plaintext exists only in memory during
  the flow.

An auditor should check: the HMAC key derivation below, that no call site
logs ``personal_code``, and file permissions on the store path (created
0600).

Concurrency: writes are rare (settings-page link/unlink only) and guarded
by a process-local lock + atomic ``os.replace``. A multi-replica web tier
would need the engine-side store — another reason it is named engine work.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from saebooks_web.config import settings

_DEFAULT_STORE = "/opt/data/saebooks-web/eid_links.json"

_lock = threading.Lock()


def _store_path() -> Path:
    """Store location — ``SAEBOOKS_EID_LINK_STORE`` env, else the standard
    ``/opt/data/<svc>`` data path (bind-mounted into the container)."""
    return Path(os.environ.get("SAEBOOKS_EID_LINK_STORE", _DEFAULT_STORE))


def _hmac_key() -> bytes:
    """Domain-separated MAC key derived from the web session secret.

    The session secret is already required, high-entropy production config
    (``SAEBOOKS_WEB_SECRET_KEY``); deriving from it avoids provisioning a
    second secret. Domain separation ensures a session-cookie forgery
    primitive and this MAC key are not interchangeable.
    """
    return hashlib.sha256(
        b"saebooks-web:eid-personal-code-hmac:v1:" + settings.secret_key.encode()
    ).digest()


def code_fingerprint(personal_code: str) -> str:
    """Deterministic keyed fingerprint of a personal code (hex)."""
    return hmac.new(_hmac_key(), personal_code.encode(), hashlib.sha256).hexdigest()


def mask_personal_code(personal_code: str) -> str:
    """``40504040001`` -> ``405••••••01`` — safe for display and storage."""
    if len(personal_code) < 6:
        return "•" * len(personal_code)
    return personal_code[:3] + "•" * (len(personal_code) - 5) + personal_code[-2:]


def _load() -> dict:
    path = _store_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1, "links": {}}
    except (OSError, ValueError):
        # Corrupt store: fail closed (no logins resolve) rather than crash
        # the login page. The settings page will show "not linked".
        return {"version": 1, "links": {}}
    if not isinstance(data, dict) or not isinstance(data.get("links"), dict):
        return {"version": 1, "links": {}}
    return data


def _save(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def find_link(personal_code: str) -> dict | None:
    """Return the link record for a (certificate-validated) personal code."""
    fp = code_fingerprint(personal_code)
    return _load()["links"].get(fp)


def find_link_for_email(email: str) -> dict | None:
    """Return the link record for a user's email (settings display)."""
    email = email.strip().lower()
    for rec in _load()["links"].values():
        if rec.get("email") == email:
            return rec
    return None


def link(email: str, personal_code: str, provider: str) -> dict:
    """Create/replace the link for ``personal_code`` -> ``email``.

    One personal code maps to exactly one user; linking a code that is
    already linked to another user replaces nothing — it raises, the
    settings UI surfaces the conflict.
    """
    email = email.strip().lower()
    fp = code_fingerprint(personal_code)
    record = {
        "email": email,
        "masked": mask_personal_code(personal_code),
        "provider": provider,
        "linked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    with _lock:
        data = _load()
        existing = data["links"].get(fp)
        if existing and existing.get("email") != email:
            raise ValueError("personal code already linked to another account")
        # One link per user: drop any other code previously linked to this
        # email so a re-link replaces cleanly.
        data["links"] = {
            k: v for k, v in data["links"].items() if v.get("email") != email
        }
        data["links"][fp] = record
        _save(data)
    return record


def unlink(email: str) -> bool:
    """Remove any link owned by ``email``. Returns True when one existed."""
    email = email.strip().lower()
    with _lock:
        data = _load()
        before = len(data["links"])
        data["links"] = {
            k: v for k, v in data["links"].items() if v.get("email") != email
        }
        if len(data["links"]) == before:
            return False
        _save(data)
    return True
