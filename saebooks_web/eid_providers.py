"""Estonian eID providers — Smart-ID and Mobiil-ID against the SK ID
Solutions REST APIs.

This module is the transport + cryptography layer for eID login. The web
routes live in ``saebooks_web.eid_sso``; account linking in
``saebooks_web.eid_links``. Nothing here touches the session or templates.

Providers
---------
* ``SmartIdProvider`` — SK Smart-ID **RP API v3**, notification-based
  authentication flow (``ACSP_V2`` signature protocol). v3 is used rather
  than the older v2 because SK's demo environment no longer auto-responds
  to v2 sessions (verified live 2026-07-12: a v2 session against a
  documented test account sat RUNNING until TIMEOUT) and SK's current
  documentation only publishes v3.
* ``MobileIdProvider`` — SK Mobile-ID (MID) REST API v1
  (``tsp.demo.sk.ee/mid-api`` / ``mid.sk.ee/mid-api``).

Both flows follow the same shape:

1. ``start_authentication`` — generate a cryptographically random
   challenge, POST it to SK, get a session id, compute the 4-digit
   verification code the user must see on their device.
2. ``check_session`` — poll SK's session endpoint. ``None`` while the user
   has not yet confirmed; an :class:`EidAssertion` once SK returns
   ``OK`` **and every cryptographic check passes**; a subclass of
   :class:`EidError` on refusal/timeout/validation failure.

Security model (the part an auditor should read)
------------------------------------------------
The user's identity is taken **exclusively from the returned X.509
certificate**, never from what the browser typed. Before an assertion is
produced:

* the certificate must be inside its validity window;
* its KeyUsage must include ``digitalSignature``;
* it must be **directly issued by a pinned SK issuing CA** from the
  environment's trust bundle (``saebooks_web/data/eid/sk_demo_ca.pem`` /
  ``sk_prod_ca.pem``) — issuer-DN match **and** issuer-signature
  verification via ``Certificate.verify_directly_issued_by`` — and the
  issuing CA itself must be valid, ``CA=TRUE`` and ``keyCertSign``.
  SK end-user certificates are always issued directly by these issuing
  CAs (no floating intermediates), so pinned-direct-issuer verification
  is a complete chain check for this PKI while avoiding a hand-rolled
  arbitrary-depth path builder;
* the signature returned by SK must verify against the certificate's
  public key over the exact challenge this process generated
  (ACSP_V2 payload for Smart-ID; the submitted digest for Mobile-ID);
* the personal code inside the certificate must equal the personal code
  the flow was started for (defence against a session-swap).

Demo vs production
------------------
``SAEBOOKS_EID_ENV=demo`` (default) targets SK's public demo environment
with SK's published demo relying-party credentials — free, no contract.
``SAEBOOKS_EID_ENV=production`` requires a signed SK contract; the
relying-party UUID/name **must** then be supplied via env and
:class:`EidLiveCredentialsMissing` is raised at provider construction —
before any socket is opened — when they are absent. Production Smart-ID
authentications are billed per transaction by SK, which is why the whole
eID surface is edition-gated to paid tiers (see ``eid_sso.eid_enabled``).

Privacy: personal codes, session tokens and certificates are never logged
by this module. Log lines carry provider name + machine error codes only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

logger = logging.getLogger("saebooks_web.eid")

_DATA_DIR = Path(__file__).resolve().parent / "data" / "eid"

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EidError(Exception):
    """Base class for eID authentication failures.

    ``code`` is a stable machine-readable identifier; templates map it to a
    translated user message. Never embed personal codes in these messages.
    """

    code = "eid_error"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.code)
        self.detail = detail


class EidUserRefused(EidError):
    """The user actively declined the authentication on their device."""

    code = "user_refused"


class EidTimeout(EidError):
    """The user did not respond before the SK session expired."""

    code = "timeout"


class EidWrongVerificationCode(EidError):
    """The user picked the wrong verification code on the device."""

    code = "wrong_vc"


class EidNoSuitableAccount(EidError):
    """No usable eID account for the given personal code / phone number."""

    code = "no_account"


class EidUnavailable(EidError):
    """Transport-level failure or SK-side outage — retry later."""

    code = "unavailable"


class EidValidationError(EidError):
    """A cryptographic check failed — treat as an attack, never retry-soft.

    Raised for: certificate outside validity, untrusted issuer, bad
    signature, certificate/flow personal-code mismatch, wrong key usage.
    """

    code = "validation_failed"


class EidLiveCredentialsMissing(EidError):
    """``SAEBOOKS_EID_ENV=production`` selected without SK contract creds.

    Raised at provider construction, before any socket is opened towards a
    production SK host. Production access requires a signed contract with
    SK ID Solutions and per-transaction billing — this must never be
    reachable by accident.
    """

    code = "live_credentials_missing"


# ---------------------------------------------------------------------------
# Config (env read at call time — same convention as the sibling providers)
# ---------------------------------------------------------------------------

_DEMO_SMARTID_URL = "https://sid.demo.sk.ee/smart-id-rp/v3"
_PROD_SMARTID_URL = "https://rp-api.smart-id.com/v3"
_DEMO_MID_URL = "https://tsp.demo.sk.ee/mid-api"
_PROD_MID_URL = "https://mid.sk.ee/mid-api"

# SK's published public demo relying-party credentials (docs: "Environment
# technical parameters"). These work only against the demo hosts.
_DEMO_SMARTID_RP_UUID = "00000000-0000-4000-8000-000000000000"
_DEMO_MID_RP_UUID = "00000000-0000-0000-0000-000000000000"
_DEMO_RP_NAME = "DEMO"


def eid_environment() -> str:
    """``demo`` (default) or ``production``."""
    env = os.environ.get("SAEBOOKS_EID_ENV", "demo").strip().lower() or "demo"
    return env if env in ("demo", "production") else "demo"


def _rp_credentials(provider: str) -> tuple[str, str]:
    """Resolve (relyingPartyUUID, relyingPartyName) for ``provider``.

    Per-provider env keys win over the shared ones (an SK contract issues
    separate credentials for Smart-ID and Mobile-ID):

    * ``SAEBOOKS_EID_SMARTID_RP_UUID`` / ``SAEBOOKS_EID_SMARTID_RP_NAME``
    * ``SAEBOOKS_EID_MID_RP_UUID`` / ``SAEBOOKS_EID_MID_RP_NAME``
    * shared fallback: ``SAEBOOKS_EID_RP_UUID`` / ``SAEBOOKS_EID_RP_NAME``

    In demo, SK's published demo credentials are the final fallback. In
    production there is no fallback — missing creds raise
    :class:`EidLiveCredentialsMissing`.
    """
    prefix = {"smart-id": "SMARTID", "mobile-id": "MID"}[provider]
    uuid = (
        os.environ.get(f"SAEBOOKS_EID_{prefix}_RP_UUID")
        or os.environ.get("SAEBOOKS_EID_RP_UUID")
        or ""
    ).strip()
    name = (
        os.environ.get(f"SAEBOOKS_EID_{prefix}_RP_NAME")
        or os.environ.get("SAEBOOKS_EID_RP_NAME")
        or ""
    ).strip()
    if eid_environment() == "production":
        if not uuid or not name:
            raise EidLiveCredentialsMissing(
                "SAEBOOKS_EID_ENV=production requires SAEBOOKS_EID_*_RP_UUID and "
                "SAEBOOKS_EID_*_RP_NAME from a signed SK ID Solutions contract"
            )
        return uuid, name
    if not uuid:
        uuid = _DEMO_SMARTID_RP_UUID if provider == "smart-id" else _DEMO_MID_RP_UUID
    return uuid, (name or _DEMO_RP_NAME)


def _smartid_base_url() -> str:
    override = os.environ.get("SAEBOOKS_EID_SMARTID_URL", "").strip()
    if override:
        return override.rstrip("/")
    return (_PROD_SMARTID_URL if eid_environment() == "production" else _DEMO_SMARTID_URL)


def _mid_base_url() -> str:
    override = os.environ.get("SAEBOOKS_EID_MID_URL", "").strip()
    if override:
        return override.rstrip("/")
    return _PROD_MID_URL if eid_environment() == "production" else _DEMO_MID_URL


def _smartid_scheme_name() -> str:
    """ACSP_V2 schemeName: ``smart-id`` in production, ``smart-id-demo`` in
    demo (verified live against sid.demo.sk.ee 2026-07-12 — the demo
    signature only verifies with ``smart-id-demo``)."""
    override = os.environ.get("SAEBOOKS_EID_SMARTID_SCHEME", "").strip()
    if override:
        return override
    return "smart-id" if eid_environment() == "production" else "smart-id-demo"


def _display_text() -> str:
    """Consent-dialog text shown on the user's device (60 chars max)."""
    text = os.environ.get("SAEBOOKS_EID_DISPLAY_TEXT", "").strip()
    if not text:
        from saebooks_web.brand import current_brand

        text = current_brand().name
    return text[:60]


# ---------------------------------------------------------------------------
# Personal-code / identity helpers
# ---------------------------------------------------------------------------

_PERSONAL_CODE_RE = re.compile(r"^[1-6]\d{10}$")

_OID_SERIALNUMBER = x509.NameOID.SERIAL_NUMBER
_OID_GIVEN_NAME = x509.NameOID.GIVEN_NAME
_OID_SURNAME = x509.NameOID.SURNAME


def normalize_personal_code(raw: str) -> str | None:
    """Return the bare 11-digit Estonian personal code, or None if invalid.

    Accepts optional ``PNOEE-`` prefix and surrounding whitespace. The
    checksum digit is validated per the isikukood modulo-11 algorithm.
    """
    code = raw.strip().upper()
    code = code.removeprefix("PNOEE-")
    if not _PERSONAL_CODE_RE.match(code):
        return None
    digits = [int(c) for c in code]
    for weights in (
        (1, 2, 3, 4, 5, 6, 7, 8, 9, 1),
        (3, 4, 5, 6, 7, 8, 9, 1, 2, 3),
    ):
        check = sum(d * w for d, w in zip(digits[:10], weights, strict=True)) % 11
        if check < 10:
            return code if digits[10] == check else None
    return code if digits[10] == 0 else None


@dataclass(frozen=True)
class EidAssertion:
    """A cryptographically validated eID identity.

    ``personal_code`` is the bare 11-digit isikukood (no ``PNOEE-``
    prefix). Treat it like a password when storing — see ``eid_links``.
    """

    personal_code: str
    country: str
    given_name: str
    surname: str
    document_number: str
    provider: str

    @property
    def display_name(self) -> str:
        return f"{self.given_name} {self.surname}".title().strip()


@dataclass(frozen=True)
class EidStart:
    """Result of starting an authentication: what the UI needs to render."""

    provider: str
    verification_code: str
    state: dict
    """JSON-serializable poll state — stored in the signed session cookie."""


# ---------------------------------------------------------------------------
# Certificate validation
# ---------------------------------------------------------------------------

_bundle_cache: dict[str, list[x509.Certificate]] = {}


def _trust_bundle() -> list[x509.Certificate]:
    """Pinned SK issuing-CA certificates for the active environment.

    ``SAEBOOKS_EID_CA_BUNDLE`` overrides the bundled file (used by tests to
    substitute a fixture CA; could also be used to pin a subset in prod).
    """
    override = os.environ.get("SAEBOOKS_EID_CA_BUNDLE", "").strip()
    path = Path(override) if override else _DATA_DIR / (
        "sk_prod_ca.pem" if eid_environment() == "production" else "sk_demo_ca.pem"
    )
    key = str(path)
    cached = _bundle_cache.get(key)
    if cached is not None:
        return cached
    certs = x509.load_pem_x509_certificates(path.read_bytes())
    if not certs:
        raise EidValidationError("empty CA trust bundle")
    _bundle_cache[key] = certs
    return certs


def validate_certificate(cert_der: bytes, *, now: datetime | None = None) -> x509.Certificate:
    """Validate an end-user auth certificate against the pinned SK CAs.

    Raises :class:`EidValidationError` unless ALL of the following hold:
    validity window, KeyUsage.digitalSignature, and direct issuance
    (issuer-DN match + signature verification) by a pinned issuing CA that
    is itself valid, ``CA=TRUE`` and ``keyCertSign``.
    """
    now = now or datetime.now(UTC)
    try:
        cert = x509.load_der_x509_certificate(cert_der)
    except Exception as exc:
        raise EidValidationError("certificate unparseable") from exc

    if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
        raise EidValidationError("certificate outside validity window")

    try:
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as exc:
        raise EidValidationError("certificate has no KeyUsage extension") from exc
    if not ku.digital_signature:
        raise EidValidationError("certificate not valid for digital signature")

    for ca in _trust_bundle():
        if ca.subject != cert.issuer:
            continue
        # Issuing CA must itself be currently valid and a real CA.
        if not (ca.not_valid_before_utc <= now <= ca.not_valid_after_utc):
            continue
        try:
            bc = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
            ca_ku = ca.extensions.get_extension_for_class(x509.KeyUsage).value
        except x509.ExtensionNotFound:
            continue
        if not bc.ca or not ca_ku.key_cert_sign:
            continue
        try:
            cert.verify_directly_issued_by(ca)
            return cert
        except (InvalidSignature, ValueError, TypeError):
            continue
    raise EidValidationError("certificate not issued by a pinned SK CA")


def extract_identity(cert: x509.Certificate, provider: str) -> EidAssertion:
    """Pull (personal code, names) out of a validated certificate subject.

    SK certificates carry ``serialNumber=PNOEE-<isikukood>`` (ETSI EN
    319 412-1 natural-person semantics identifier). Only Estonian (``EE``)
    identities are accepted for now.
    """

    def _attr(oid) -> str:
        vals = cert.subject.get_attributes_for_oid(oid)
        return vals[0].value if vals else ""

    serial = _attr(_OID_SERIALNUMBER)
    m = re.match(r"^PNO([A-Z]{2})-(\d{11})$", serial)
    if not m:
        raise EidValidationError("certificate has no PNO semantics identifier")
    country, code = m.group(1), m.group(2)
    if country != "EE":
        raise EidValidationError("only Estonian (PNOEE) identities are accepted")
    return EidAssertion(
        personal_code=code,
        country=country,
        given_name=_attr(_OID_GIVEN_NAME),
        surname=_attr(_OID_SURNAME),
        document_number="",
        provider=provider,
    )


# ---------------------------------------------------------------------------
# Signature verification helpers
# ---------------------------------------------------------------------------

_HASHES = {
    "SHA256": hashes.SHA256,
    "SHA-256": hashes.SHA256,
    "SHA384": hashes.SHA384,
    "SHA-384": hashes.SHA384,
    "SHA512": hashes.SHA512,
    "SHA-512": hashes.SHA512,
}


def _hash_for(name: str) -> hashes.HashAlgorithm:
    try:
        return _HASHES[name.upper()]()
    except KeyError as exc:
        raise EidValidationError("unsupported hash algorithm") from exc


def _verify_rsa(
    pub: rsa.RSAPublicKey,
    signature: bytes,
    data: bytes,
    algorithm: str,
    algorithm_params: dict | None,
) -> None:
    """Verify an RSA signature over ``data`` (full message, not prehashed)."""
    if algorithm == "rsassa-pss":
        params = algorithm_params or {}
        halg = _hash_for(params.get("hashAlgorithm", "SHA-512"))
        salt = params.get("saltLength", halg.digest_size)
        try:
            pub.verify(
                signature, data, padding.PSS(mgf=padding.MGF1(halg), salt_length=salt), halg
            )
            return
        except InvalidSignature as exc:
            raise EidValidationError("signature verification failed") from exc
    m = re.match(r"^sha(256|384|512)WithRSAEncryption$", algorithm)
    if m:
        halg = _hash_for(f"SHA{m.group(1)}")
        try:
            pub.verify(signature, data, padding.PKCS1v15(), halg)
            return
        except InvalidSignature as exc:
            raise EidValidationError("signature verification failed") from exc
    raise EidValidationError("unsupported signature algorithm")


def _verify_prehashed(pub, signature: bytes, digest: bytes, hash_name: str) -> None:
    """Verify a signature made directly over a digest we computed (MID).

    EC signatures from SK come as raw ``R||S``; DER is tried as fallback.
    RSA uses PKCS#1 v1.5.
    """
    halg = _hash_for(hash_name)
    if len(digest) != halg.digest_size:
        raise EidValidationError("digest length mismatch")
    if isinstance(pub, ec.EllipticCurvePublicKey):
        candidates = []
        if len(signature) % 2 == 0:
            half = len(signature) // 2
            candidates.append(
                encode_dss_signature(
                    int.from_bytes(signature[:half], "big"),
                    int.from_bytes(signature[half:], "big"),
                )
            )
        candidates.append(signature)  # already-DER fallback
        for cand in candidates:
            try:
                pub.verify(cand, digest, ec.ECDSA(utils.Prehashed(halg)))
                return
            except (InvalidSignature, ValueError):
                continue
        raise EidValidationError("signature verification failed")
    if isinstance(pub, rsa.RSAPublicKey):
        try:
            pub.verify(signature, digest, padding.PKCS1v15(), utils.Prehashed(halg))
            return
        except InvalidSignature as exc:
            raise EidValidationError("signature verification failed") from exc
    raise EidValidationError("unsupported public key type")


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class EidProvider(ABC):
    """One eID authentication method (Smart-ID, Mobiil-ID, …)."""

    key: str
    requires_phone_number = False

    @abstractmethod
    async def start_authentication(
        self,
        personal_code: str,
        *,
        phone_number: str | None = None,
        language: str = "et",
    ) -> EidStart:
        """Kick off an authentication; returns the verification code +
        JSON-safe poll state. Raises an :class:`EidError` subclass on
        failure."""

    @abstractmethod
    async def check_session(self, state: dict) -> EidAssertion | None:
        """Poll SK once. ``None`` while pending; a validated assertion on
        success; raises an :class:`EidError` subclass on any terminal
        failure."""


async def _http_json(
    method: str, url: str, *, json_body: dict | None = None, timeout: float = 20.0
) -> dict:
    """One JSON request to SK with uniform transport-error mapping."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json_body)
    except httpx.RequestError as exc:
        logger.warning("eid transport error: %s %s", method, exc.__class__.__name__)
        raise EidUnavailable("SK endpoint unreachable") from exc
    if resp.status_code in (400, 401, 403):
        logger.warning("eid request rejected: HTTP %s", resp.status_code)
        raise EidError(f"request rejected (HTTP {resp.status_code})")
    if resp.status_code == 404:
        raise EidNoSuitableAccount("no active eID account found")
    if resp.status_code == 471:
        raise EidNoSuitableAccount("no suitable account of requested type")
    if resp.status_code == 472:
        raise EidNoSuitableAccount("user must visit the eID portal")
    if not resp.is_success:
        logger.warning("eid endpoint error: HTTP %s", resp.status_code)
        raise EidUnavailable(f"SK endpoint error (HTTP {resp.status_code})")
    try:
        return resp.json()
    except ValueError as exc:
        raise EidUnavailable("SK endpoint returned non-JSON") from exc


# ---------------------------------------------------------------------------
# Smart-ID (RP API v3, notification flow, ACSP_V2)
# ---------------------------------------------------------------------------


class SmartIdProvider(EidProvider):
    key = "smart-id"

    def __init__(self) -> None:
        # Fail loud BEFORE any socket when production is selected without
        # contract credentials.
        self.rp_uuid, self.rp_name = _rp_credentials("smart-id")
        self.base_url = _smartid_base_url()

    async def start_authentication(
        self,
        personal_code: str,
        *,
        phone_number: str | None = None,
        language: str = "et",
    ) -> EidStart:
        rp_challenge_bytes = secrets.token_bytes(64)
        rp_challenge = base64.b64encode(rp_challenge_bytes).decode()
        interactions_b64 = base64.b64encode(
            json.dumps(
                [{"type": "displayTextAndPIN", "displayText60": _display_text()}],
                separators=(",", ":"),
            ).encode()
        ).decode()
        body = {
            "relyingPartyUUID": self.rp_uuid,
            "relyingPartyName": self.rp_name,
            "certificateLevel": "QUALIFIED",
            "signatureProtocol": "ACSP_V2",
            "signatureProtocolParameters": {
                "rpChallenge": rp_challenge,
                "signatureAlgorithm": "rsassa-pss",
                "signatureAlgorithmParameters": {"hashAlgorithm": "SHA-512"},
            },
            "interactions": interactions_b64,
            "vcType": "numeric4",
        }
        data = await _http_json(
            "POST",
            f"{self.base_url}/authentication/notification/etsi/PNOEE-{personal_code}",
            json_body=body,
        )
        session_id = data.get("sessionID", "")
        if not session_id:
            raise EidUnavailable("SK did not return a session id")
        vc = int.from_bytes(hashlib.sha256(rp_challenge_bytes).digest()[-2:], "big") % 10000
        return EidStart(
            provider=self.key,
            verification_code=f"{vc:04d}",
            state={
                "provider": self.key,
                "session_id": session_id,
                "rp_challenge": rp_challenge,
                "interactions": interactions_b64,
                "personal_code": personal_code,
            },
        )

    async def check_session(self, state: dict) -> EidAssertion | None:
        data = await _http_json(
            "GET", f"{self.base_url}/session/{state['session_id']}?timeoutMs=4000",
            timeout=15.0,
        )
        if data.get("state") != "COMPLETE":
            return None
        end_result = (data.get("result") or {}).get("endResult", "")
        if end_result != "OK":
            raise _SMARTID_END_RESULTS.get(end_result, EidError)(end_result)
        return self._validate(data, state)

    def _validate(self, data: dict, state: dict) -> EidAssertion:
        cert_info = data.get("cert") or {}
        if cert_info.get("certificateLevel") not in ("QUALIFIED",):
            raise EidValidationError("certificate level below QUALIFIED")
        try:
            cert_der = base64.b64decode(cert_info.get("value") or "")
        except Exception as exc:
            raise EidValidationError("certificate undecodable") from exc
        cert = validate_certificate(cert_der)

        if data.get("signatureProtocol") != "ACSP_V2":
            raise EidValidationError("unexpected signature protocol")
        sig = data.get("signature") or {}
        for field in ("value", "serverRandom", "userChallenge", "flowType", "signatureAlgorithm"):
            if not sig.get(field):
                raise EidValidationError(f"signature response missing {field}")

        # Reconstruct the ACSP_V2 payload exactly as specified by SK
        # (signature_protocols.html). Empty brokeredRpName and
        # initialCallbackUrl keep their separators for notification flows.
        interactions_b64 = state["interactions"]
        payload = "|".join(
            [
                _smartid_scheme_name(),
                "ACSP_V2",
                sig["serverRandom"],
                state["rp_challenge"],
                sig["userChallenge"],
                base64.b64encode(self.rp_name.encode()).decode(),
                "",  # brokeredRpName — not brokering
                base64.b64encode(hashlib.sha256(interactions_b64.encode()).digest()).decode(),
                data.get("interactionTypeUsed", ""),
                "",  # initialCallbackUrl — notification flow
                sig["flowType"],
            ]
        ).encode()

        pub = cert.public_key()
        if not isinstance(pub, rsa.RSAPublicKey):
            raise EidValidationError("unexpected key type for Smart-ID certificate")
        try:
            sig_bytes = base64.b64decode(sig["value"])
        except Exception as exc:
            raise EidValidationError("signature undecodable") from exc
        _verify_rsa(
            pub,
            sig_bytes,
            payload,
            sig["signatureAlgorithm"],
            sig.get("signatureAlgorithmParameters"),
        )

        assertion = extract_identity(cert, self.key)
        if assertion.personal_code != state.get("personal_code"):
            raise EidValidationError("certificate identity does not match request")
        doc = (data.get("result") or {}).get("documentNumber", "")
        return EidAssertion(
            personal_code=assertion.personal_code,
            country=assertion.country,
            given_name=assertion.given_name,
            surname=assertion.surname,
            document_number=doc,
            provider=self.key,
        )


_SMARTID_END_RESULTS: dict[str, type[EidError]] = {
    "USER_REFUSED": EidUserRefused,
    "USER_REFUSED_INTERACTION": EidUserRefused,
    "USER_REFUSED_DISPLAYTEXTANDPIN": EidUserRefused,
    "USER_REFUSED_VC_CHOICE": EidUserRefused,
    "USER_REFUSED_CONFIRMATIONMESSAGE": EidUserRefused,
    "USER_REFUSED_CONFIRMATIONMESSAGE_WITH_VC_CHOICE": EidUserRefused,
    "USER_REFUSED_CERT_CHOICE": EidUserRefused,
    "TIMEOUT": EidTimeout,
    "WRONG_VC": EidWrongVerificationCode,
    "DOCUMENT_UNUSABLE": EidNoSuitableAccount,
    "REQUIRED_INTERACTION_NOT_SUPPORTED_BY_APP": EidError,
    "PROTOCOL_FAILURE": EidError,
    "SERVER_ERROR": EidUnavailable,
}


# ---------------------------------------------------------------------------
# Mobile-ID (MID REST API v1)
# ---------------------------------------------------------------------------

_MID_LANGUAGES = {"et": "EST", "en": "ENG", "ru": "RUS"}

_PHONE_RE = re.compile(r"^\+372\d{7,8}$")


class MobileIdProvider(EidProvider):
    key = "mobile-id"
    requires_phone_number = True

    def __init__(self) -> None:
        self.rp_uuid, self.rp_name = _rp_credentials("mobile-id")
        self.base_url = _mid_base_url()

    async def start_authentication(
        self,
        personal_code: str,
        *,
        phone_number: str | None = None,
        language: str = "et",
    ) -> EidStart:
        phone = (phone_number or "").strip().replace(" ", "")
        if phone and not phone.startswith("+"):
            phone = f"+372{phone}"
        if not _PHONE_RE.match(phone):
            raise EidError("invalid phone number")
        digest = hashlib.sha256(secrets.token_bytes(64)).digest()
        body = {
            "relyingPartyUUID": self.rp_uuid,
            "relyingPartyName": self.rp_name,
            "phoneNumber": phone,
            "nationalIdentityNumber": personal_code,
            "hash": base64.b64encode(digest).decode(),
            "hashType": "SHA256",
            "language": _MID_LANGUAGES.get(language, "EST"),
            "displayText": _display_text(),
        }
        data = await _http_json("POST", f"{self.base_url}/authentication", json_body=body)
        session_id = data.get("sessionID", "")
        if not session_id:
            raise EidUnavailable("SK did not return a session id")
        # MID verification code: 6 leading bits + 7 trailing bits of the
        # digest, as a 4-digit decimal (SK-EID/MID documentation).
        vc = ((digest[0] & 0xFC) << 5) | (digest[-1] & 0x7F)
        return EidStart(
            provider=self.key,
            verification_code=f"{vc:04d}",
            state={
                "provider": self.key,
                "session_id": session_id,
                "digest": base64.b64encode(digest).decode(),
                "personal_code": personal_code,
            },
        )

    async def check_session(self, state: dict) -> EidAssertion | None:
        data = await _http_json(
            "GET",
            f"{self.base_url}/authentication/session/{state['session_id']}?timeoutMs=4000",
            timeout=15.0,
        )
        if data.get("state") != "COMPLETE":
            return None
        result = data.get("result", "")
        if result != "OK":
            raise _MID_RESULTS.get(result, EidError)(result)
        return self._validate(data, state)

    def _validate(self, data: dict, state: dict) -> EidAssertion:
        try:
            cert_der = base64.b64decode(data.get("cert") or "")
        except Exception as exc:
            raise EidValidationError("certificate undecodable") from exc
        cert = validate_certificate(cert_der)

        sig = data.get("signature") or {}
        try:
            sig_bytes = base64.b64decode(sig.get("value") or "")
            digest = base64.b64decode(state["digest"])
        except Exception as exc:
            raise EidValidationError("signature undecodable") from exc
        algorithm = sig.get("algorithm", "")
        m = re.match(r"^SHA(256|384|512)With", algorithm, re.IGNORECASE)
        if not m:
            raise EidValidationError("unsupported signature algorithm")
        _verify_prehashed(cert.public_key(), sig_bytes, digest, f"SHA{m.group(1)}")

        assertion = extract_identity(cert, self.key)
        if assertion.personal_code != state.get("personal_code"):
            raise EidValidationError("certificate identity does not match request")
        return assertion


_MID_RESULTS: dict[str, type[EidError]] = {
    "USER_CANCELLED": EidUserRefused,
    "TIMEOUT": EidTimeout,
    "NOT_MID_CLIENT": EidNoSuitableAccount,
    "SIGNATURE_HASH_MISMATCH": EidValidationError,
    "PHONE_ABSENT": EidUnavailable,
    "DELIVERY_ERROR": EidUnavailable,
    "SIM_ERROR": EidUnavailable,
}


# ---------------------------------------------------------------------------
# Web eID (ID-card) — PARKED
# ---------------------------------------------------------------------------


class WebEidProvider(EidProvider):
    """ID-card login via the Web eID browser extension — PARKED.

    Feasibility verdict (2026-07-12): the Web eID project publishes
    authentication-token validation libraries for **Java, .NET and PHP
    only**; there is no maintained Python implementation on PyPI (checked:
    ``web-eid-authtoken-validation-python`` and variants do not exist —
    the only Python artefact upstream is a deprecated test-token
    *generator*). Hand-rolling the Web eID token validation (OCSP checks,
    origin binding, token format v1/v2 handling) in this wave would be a
    security liability, and Smart-ID covers the overwhelming majority of
    Estonian users. Revisit if/when an official Python library appears or
    the validation is delegated to a vetted sidecar service.
    """

    key = "id-card"

    async def start_authentication(self, personal_code, *, phone_number=None, language="et"):
        raise EidUnavailable("ID-card login is not yet supported")

    async def check_session(self, state):
        raise EidUnavailable("ID-card login is not yet supported")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type[EidProvider]] = {
    SmartIdProvider.key: SmartIdProvider,
    MobileIdProvider.key: MobileIdProvider,
}


def enabled_provider_keys() -> list[str]:
    """Provider keys enabled by config (``SAEBOOKS_EID_PROVIDERS``).

    Defaults to both live providers. Unknown keys are ignored (so listing
    ``id-card`` before it ships does not 500 the login page).
    """
    raw = os.environ.get("SAEBOOKS_EID_PROVIDERS", "smart-id,mobile-id")
    return [k.strip() for k in raw.split(",") if k.strip() in _PROVIDERS]


def get_provider(key: str) -> EidProvider:
    """Instantiate the provider for ``key``.

    Raises ``KeyError`` for unknown/disabled keys and
    :class:`EidLiveCredentialsMissing` when production is selected without
    contract credentials.
    """
    if key not in enabled_provider_keys():
        raise KeyError(key)
    return _PROVIDERS[key]()
