"""Provider-layer tests for Smart-ID / Mobiil-ID — mocked SK transport.

Covers the full session state machine (started → verification code →
COMPLETE/OK, USER_REFUSED, TIMEOUT, WRONG_VC), certificate validation
with fixture certs (expired, untrusted issuer, wrong key usage, identity
mismatch) and signature validation (good + tampered), plus the
production-credentials fail-loud gate.
"""
from __future__ import annotations

import base64
import hashlib
import json

import pytest
import respx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import (
    Prehashed,
    decode_dss_signature,
)
from httpx import Response

from saebooks_web import eid_providers as ep
from tests._eid_certs import ca_bundle_pem, cert_der_b64, make_ca, make_leaf

SMARTID = "https://sid.demo.sk.ee/smart-id-rp/v3"
MID = "https://tsp.demo.sk.ee/mid-api"

CODE = "40504040001"


@pytest.fixture
def fixture_ca(tmp_path, monkeypatch):
    """Generate a CA, point the provider trust bundle at it."""
    ca_key, ca_cert = make_ca()
    bundle = tmp_path / "fixture_ca.crt"
    bundle.write_bytes(ca_bundle_pem(ca_cert))
    monkeypatch.setenv("SAEBOOKS_EID_CA_BUNDLE", str(bundle))
    return ca_key, ca_cert


# ---------------------------------------------------------------------------
# normalize_personal_code
# ---------------------------------------------------------------------------


def test_normalize_personal_code() -> None:
    assert ep.normalize_personal_code("40504040001") == CODE
    assert ep.normalize_personal_code(" PNOEE-40504040001 ") == CODE
    assert ep.normalize_personal_code("60001019906") == "60001019906"
    assert ep.normalize_personal_code("40504040002") is None  # bad checksum
    assert ep.normalize_personal_code("1234") is None
    assert ep.normalize_personal_code("90504040001") is None  # bad century digit
    assert ep.normalize_personal_code("abcdefghijk") is None


# ---------------------------------------------------------------------------
# Production fail-loud
# ---------------------------------------------------------------------------


def test_production_without_credentials_fails_before_any_socket(monkeypatch) -> None:
    monkeypatch.setenv("SAEBOOKS_EID_ENV", "production")
    monkeypatch.delenv("SAEBOOKS_EID_RP_UUID", raising=False)
    monkeypatch.delenv("SAEBOOKS_EID_RP_NAME", raising=False)
    with pytest.raises(ep.EidLiveCredentialsMissing):
        ep.SmartIdProvider()
    with pytest.raises(ep.EidLiveCredentialsMissing):
        ep.MobileIdProvider()


def test_production_with_credentials_constructs(monkeypatch) -> None:
    monkeypatch.setenv("SAEBOOKS_EID_ENV", "production")
    monkeypatch.setenv("SAEBOOKS_EID_RP_UUID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("SAEBOOKS_EID_RP_NAME", "Tasur")
    prov = ep.SmartIdProvider()
    assert prov.base_url.startswith("https://rp-api.smart-id.com")
    assert prov.rp_name == "Tasur"


# ---------------------------------------------------------------------------
# Smart-ID
# ---------------------------------------------------------------------------


async def _sid_start(respx_mock) -> ep.EidStart:
    respx_mock.post(f"{SMARTID}/authentication/notification/etsi/PNOEE-{CODE}").mock(
        return_value=Response(200, json={"sessionID": "sess-1"})
    )
    return await ep.SmartIdProvider().start_authentication(CODE)


@pytest.mark.anyio
@respx.mock
async def test_smartid_start_verification_code(respx_mock) -> None:
    start = await _sid_start(respx_mock)
    assert start.provider == "smart-id"
    assert start.state["session_id"] == "sess-1"
    # VC must match SK's documented formula over the rpChallenge we sent.
    sent = json.loads(respx_mock.calls[0].request.content)
    assert sent["signatureProtocol"] == "ACSP_V2"
    assert sent["vcType"] == "numeric4"
    rp_challenge = base64.b64decode(sent["signatureProtocolParameters"]["rpChallenge"])
    assert len(rp_challenge) == 64
    vc = int.from_bytes(hashlib.sha256(rp_challenge).digest()[-2:], "big") % 10000
    assert start.verification_code == f"{vc:04d}"


@pytest.mark.anyio
@respx.mock
async def test_smartid_running_returns_none(respx_mock) -> None:
    start = await _sid_start(respx_mock)
    respx_mock.get(f"{SMARTID}/session/sess-1").mock(
        return_value=Response(200, json={"state": "RUNNING"})
    )
    assert await ep.SmartIdProvider().check_session(start.state) is None


@pytest.mark.parametrize(
    ("end_result", "exc"),
    [
        ("USER_REFUSED", ep.EidUserRefused),
        ("USER_REFUSED_DISPLAYTEXTANDPIN", ep.EidUserRefused),
        ("TIMEOUT", ep.EidTimeout),
        ("WRONG_VC", ep.EidWrongVerificationCode),
        ("DOCUMENT_UNUSABLE", ep.EidNoSuitableAccount),
        ("SOMETHING_NEW", ep.EidError),
    ],
)
@pytest.mark.anyio
@respx.mock
async def test_smartid_terminal_end_results(respx_mock, end_result, exc) -> None:
    start = await _sid_start(respx_mock)
    respx_mock.get(f"{SMARTID}/session/sess-1").mock(
        return_value=Response(
            200, json={"state": "COMPLETE", "result": {"endResult": end_result}}
        )
    )
    with pytest.raises(exc):
        await ep.SmartIdProvider().check_session(start.state)


def _sid_ok_response(state: dict, leaf_key, leaf_cert, *, tamper=False, itu="displayTextAndPIN"):
    """Build a COMPLETE/OK session response with a real ACSP_V2 signature."""
    server_random = base64.b64encode(b"server-random-bytes-0123").decode()
    user_challenge = base64.urlsafe_b64encode(hashlib.sha256(b"ucv").digest()).rstrip(b"=").decode()
    payload = "|".join(
        [
            "smart-id-demo",
            "ACSP_V2",
            server_random,
            state["rp_challenge"],
            user_challenge,
            base64.b64encode(b"DEMO").decode(),
            "",
            base64.b64encode(hashlib.sha256(state["interactions"].encode()).digest()).decode(),
            itu,
            "",
            "Notification",
        ]
    ).encode()
    if tamper:
        payload += b"x"
    sig = leaf_key.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64),
        hashes.SHA512(),
    )
    return {
        "state": "COMPLETE",
        "result": {"endResult": "OK", "documentNumber": f"PNOEE-{CODE}-FIX-Q"},
        "signatureProtocol": "ACSP_V2",
        "interactionTypeUsed": itu,
        "signature": {
            "value": base64.b64encode(sig).decode(),
            "serverRandom": server_random,
            "userChallenge": user_challenge,
            "flowType": "Notification",
            "signatureAlgorithm": "rsassa-pss",
            "signatureAlgorithmParameters": {"hashAlgorithm": "SHA-512", "saltLength": 64},
        },
        "cert": {"value": cert_der_b64(leaf_cert), "certificateLevel": "QUALIFIED"},
    }


@pytest.mark.anyio
@respx.mock
async def test_smartid_ok_yields_validated_assertion(respx_mock, fixture_ca) -> None:
    ca_key, ca_cert = fixture_ca
    leaf_key, leaf_cert = make_leaf(ca_key, ca_cert, personal_code=CODE)
    start = await _sid_start(respx_mock)
    respx_mock.get(f"{SMARTID}/session/sess-1").mock(
        return_value=Response(200, json=_sid_ok_response(start.state, leaf_key, leaf_cert))
    )
    assertion = await ep.SmartIdProvider().check_session(start.state)
    assert assertion is not None
    assert assertion.personal_code == CODE
    assert assertion.country == "EE"
    assert assertion.given_name == "OK"
    assert assertion.surname == "TEST"
    assert assertion.document_number == f"PNOEE-{CODE}-FIX-Q"
    assert assertion.provider == "smart-id"


@pytest.mark.anyio
@respx.mock
async def test_smartid_tampered_signature_rejected(respx_mock, fixture_ca) -> None:
    ca_key, ca_cert = fixture_ca
    leaf_key, leaf_cert = make_leaf(ca_key, ca_cert, personal_code=CODE)
    start = await _sid_start(respx_mock)
    respx_mock.get(f"{SMARTID}/session/sess-1").mock(
        return_value=Response(
            200, json=_sid_ok_response(start.state, leaf_key, leaf_cert, tamper=True)
        )
    )
    with pytest.raises(ep.EidValidationError):
        await ep.SmartIdProvider().check_session(start.state)


@pytest.mark.anyio
@respx.mock
async def test_smartid_expired_certificate_rejected(respx_mock, fixture_ca) -> None:
    from datetime import UTC, datetime, timedelta

    ca_key, ca_cert = fixture_ca
    leaf_key, leaf_cert = make_leaf(
        ca_key,
        ca_cert,
        personal_code=CODE,
        not_before=datetime.now(UTC) - timedelta(days=800),
        not_after=datetime.now(UTC) - timedelta(days=30),
    )
    start = await _sid_start(respx_mock)
    respx_mock.get(f"{SMARTID}/session/sess-1").mock(
        return_value=Response(200, json=_sid_ok_response(start.state, leaf_key, leaf_cert))
    )
    with pytest.raises(ep.EidValidationError):
        await ep.SmartIdProvider().check_session(start.state)


@pytest.mark.anyio
@respx.mock
async def test_smartid_untrusted_issuer_rejected(respx_mock, fixture_ca) -> None:
    rogue_key, rogue_ca = make_ca("Rogue CA")
    leaf_key, leaf_cert = make_leaf(rogue_key, rogue_ca, personal_code=CODE)
    start = await _sid_start(respx_mock)
    respx_mock.get(f"{SMARTID}/session/sess-1").mock(
        return_value=Response(200, json=_sid_ok_response(start.state, leaf_key, leaf_cert))
    )
    with pytest.raises(ep.EidValidationError):
        await ep.SmartIdProvider().check_session(start.state)


@pytest.mark.anyio
@respx.mock
async def test_smartid_identity_mismatch_rejected(respx_mock, fixture_ca) -> None:
    """A valid certificate for a DIFFERENT personal code must be refused."""
    ca_key, ca_cert = fixture_ca
    leaf_key, leaf_cert = make_leaf(ca_key, ca_cert, personal_code="60001019906")
    start = await _sid_start(respx_mock)
    respx_mock.get(f"{SMARTID}/session/sess-1").mock(
        return_value=Response(200, json=_sid_ok_response(start.state, leaf_key, leaf_cert))
    )
    with pytest.raises(ep.EidValidationError):
        await ep.SmartIdProvider().check_session(start.state)


@pytest.mark.anyio
@respx.mock
async def test_smartid_advanced_level_rejected(respx_mock, fixture_ca) -> None:
    ca_key, ca_cert = fixture_ca
    leaf_key, leaf_cert = make_leaf(ca_key, ca_cert, personal_code=CODE)
    start = await _sid_start(respx_mock)
    body = _sid_ok_response(start.state, leaf_key, leaf_cert)
    body["cert"]["certificateLevel"] = "ADVANCED"
    respx_mock.get(f"{SMARTID}/session/sess-1").mock(return_value=Response(200, json=body))
    with pytest.raises(ep.EidValidationError):
        await ep.SmartIdProvider().check_session(start.state)


@pytest.mark.anyio
@respx.mock
async def test_smartid_no_account_maps_to_no_suitable_account(respx_mock) -> None:
    respx_mock.post(f"{SMARTID}/authentication/notification/etsi/PNOEE-{CODE}").mock(
        return_value=Response(404, json={"title": "Not Found"})
    )
    with pytest.raises(ep.EidNoSuitableAccount):
        await ep.SmartIdProvider().start_authentication(CODE)


# ---------------------------------------------------------------------------
# Mobile-ID
# ---------------------------------------------------------------------------


async def _mid_start(respx_mock) -> ep.EidStart:
    respx_mock.post(f"{MID}/authentication").mock(
        return_value=Response(200, json={"sessionID": "mid-sess-1"})
    )
    return await ep.MobileIdProvider().start_authentication(
        CODE, phone_number="+37200000766"
    )


@pytest.mark.anyio
@respx.mock
async def test_mid_start_verification_code(respx_mock) -> None:
    start = await _mid_start(respx_mock)
    sent = json.loads(respx_mock.calls[0].request.content)
    assert sent["phoneNumber"] == "+37200000766"
    assert sent["nationalIdentityNumber"] == CODE
    assert sent["hashType"] == "SHA256"
    digest = base64.b64decode(sent["hash"])
    vc = ((digest[0] & 0xFC) << 5) | (digest[-1] & 0x7F)
    assert start.verification_code == f"{vc:04d}"


@pytest.mark.anyio
async def test_mid_rejects_bad_phone_number() -> None:
    with pytest.raises(ep.EidError):
        await ep.MobileIdProvider().start_authentication(CODE, phone_number="12345")


def _mid_ok_response(state: dict, leaf_key, leaf_cert, *, tamper=False):
    digest = base64.b64decode(state["digest"])
    if tamper:
        digest = hashlib.sha256(b"other").digest()
    der_sig = leaf_key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
    # SK returns EC signatures as raw R||S — convert.
    r, s = decode_dss_signature(der_sig)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return {
        "state": "COMPLETE",
        "result": "OK",
        "signature": {
            "value": base64.b64encode(raw).decode(),
            "algorithm": "SHA256WithECEncryption",
        },
        "cert": cert_der_b64(leaf_cert),
    }


@pytest.mark.anyio
@respx.mock
async def test_mid_ok_yields_validated_assertion(respx_mock, fixture_ca) -> None:
    ca_key, ca_cert = fixture_ca
    leaf_key, leaf_cert = make_leaf(ca_key, ca_cert, personal_code=CODE, key_type="ec")
    start = await _mid_start(respx_mock)
    respx_mock.get(f"{MID}/authentication/session/mid-sess-1").mock(
        return_value=Response(200, json=_mid_ok_response(start.state, leaf_key, leaf_cert))
    )
    assertion = await ep.MobileIdProvider().check_session(start.state)
    assert assertion is not None
    assert assertion.personal_code == CODE
    assert assertion.provider == "mobile-id"


@pytest.mark.anyio
@respx.mock
async def test_mid_tampered_signature_rejected(respx_mock, fixture_ca) -> None:
    ca_key, ca_cert = fixture_ca
    leaf_key, leaf_cert = make_leaf(ca_key, ca_cert, personal_code=CODE, key_type="ec")
    start = await _mid_start(respx_mock)
    respx_mock.get(f"{MID}/authentication/session/mid-sess-1").mock(
        return_value=Response(
            200, json=_mid_ok_response(start.state, leaf_key, leaf_cert, tamper=True)
        )
    )
    with pytest.raises(ep.EidValidationError):
        await ep.MobileIdProvider().check_session(start.state)


@pytest.mark.parametrize(
    ("result", "exc"),
    [
        ("USER_CANCELLED", ep.EidUserRefused),
        ("TIMEOUT", ep.EidTimeout),
        ("NOT_MID_CLIENT", ep.EidNoSuitableAccount),
        ("SIGNATURE_HASH_MISMATCH", ep.EidValidationError),
        ("PHONE_ABSENT", ep.EidUnavailable),
    ],
)
@pytest.mark.anyio
@respx.mock
async def test_mid_terminal_results(respx_mock, result, exc) -> None:
    start = await _mid_start(respx_mock)
    respx_mock.get(f"{MID}/authentication/session/mid-sess-1").mock(
        return_value=Response(200, json={"state": "COMPLETE", "result": result})
    )
    with pytest.raises(exc):
        await ep.MobileIdProvider().check_session(start.state)


@pytest.mark.anyio
@respx.mock
async def test_mid_running_returns_none(respx_mock) -> None:
    start = await _mid_start(respx_mock)
    respx_mock.get(f"{MID}/authentication/session/mid-sess-1").mock(
        return_value=Response(200, json={"state": "RUNNING"})
    )
    assert await ep.MobileIdProvider().check_session(start.state) is None


# ---------------------------------------------------------------------------
# Registry / parked provider
# ---------------------------------------------------------------------------


def test_registry_defaults_and_unknown_keys(monkeypatch) -> None:
    assert ep.enabled_provider_keys() == ["smart-id", "mobile-id"]
    monkeypatch.setenv("SAEBOOKS_EID_PROVIDERS", "smart-id,id-card,bogus")
    assert ep.enabled_provider_keys() == ["smart-id"]
    with pytest.raises(KeyError):
        ep.get_provider("mobile-id")
