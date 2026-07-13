"""Fixture-certificate factory for the eID tests.

Generates a throwaway issuing CA + end-user certificates at test time (no
committed key material) shaped like SK's real ones: subject carries
``serialNumber=PNOEE-<isikukood>`` + given name + surname, KeyUsage
includes digitalSignature, and the leaf is directly issued by the CA.
Tests point ``SAEBOOKS_EID_CA_BUNDLE`` at the CA's PEM to make the
provider trust it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

_KU_OFF = dict(
    content_commitment=False,
    key_encipherment=False,
    data_encipherment=False,
    key_agreement=False,
    encipher_only=False,
    decipher_only=False,
)


def make_ca(common_name: str = "TEST fixture eID CA"):
    """Return (private_key, certificate) for a self-signed issuing CA."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "EE"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fixture"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=False, key_cert_sign=True, crl_sign=True, **_KU_OFF),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_leaf(
    ca_key,
    ca_cert,
    *,
    personal_code: str = "40504040001",
    country: str = "EE",
    given_name: str = "OK",
    surname: str = "TEST",
    key_type: str = "rsa",
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    digital_signature: bool = True,
):
    """Return (private_key, certificate) for an SK-shaped end-user cert."""
    if key_type == "ec":
        key = ec.generate_private_key(ec.SECP256R1())
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "EE"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"{surname},{given_name}"),
            x509.NameAttribute(NameOID.SURNAME, surname),
            x509.NameAttribute(NameOID.GIVEN_NAME, given_name),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, f"PNO{country}-{personal_code}"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or (now - timedelta(days=1)))
        .not_valid_after(not_after or (now + timedelta(days=365)))
        .add_extension(
            x509.KeyUsage(
                digital_signature=digital_signature, key_cert_sign=False, crl_sign=False, **_KU_OFF
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def ca_bundle_pem(*certs) -> bytes:
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)


def cert_der_b64(cert) -> str:
    import base64

    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
