"""Server-only QZ identity, shared by settings and operational printing."""

import base64
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings

from .config import Settings


class QzSigningSettings(BaseSettings):
    model_config = Settings.model_config

    qz_tray_certificate: SecretStr = SecretStr("")
    qz_tray_private_key: SecretStr = SecretStr("")
    qz_tray_certificate_file: str = ""
    qz_tray_private_key_file: str = ""
    environment: str = "development"
    qz_require_signing: bool = False
    qz_tray_trust_mode: Literal["compatible", "official"] = "compatible"


INTERMEDIATE_SEPARATOR = "--START INTERMEDIATE CERT--"


@lru_cache(maxsize=1)
def _qz_root():
    # Public trust anchor from qzind/tray v2.3.0, src/qz/auth/Certificate.java.
    # This is not a localhost TLS certificate or a customer signing identity.
    return x509.load_pem_x509_certificate(
        (Path(__file__).parent / "resources/qz/qz-root.pem").read_bytes())


def _pem(inline: SecretStr, filename: str) -> str:
    value = inline.get_secret_value().strip()
    if value and filename.strip():
        raise HTTPException(503, "QZ Tray signing configuration has conflicting sources")
    if filename.strip():
        try:
            with Path(filename).open("r", encoding="utf-8-sig") as source:
                value = source.read(131073)
            if len(value) > 131072:
                raise ValueError("PEM too large")
        except (OSError, UnicodeError, ValueError):
            raise HTTPException(503, "QZ Tray signing file could not be read") from None
        if not value.strip():
            raise HTTPException(503, "QZ Tray signing file is empty")
    return value.replace("\\n", "\n").strip()


@lru_cache(maxsize=2)
def _parse_identity(certificate: str, key: str):
    try:
        # A permissive PEM parser can ignore a concatenated private key. Never
        # return the input verbatim as the public identity sent to browsers.
        if certificate.count(INTERMEDIATE_SEPARATOR) > 1:
            raise ValueError("Too many intermediates")
        pem_input = certificate.replace(INTERMEDIATE_SEPARATOR, "\n")
        if not re.fullmatch(r"(?:\s*-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*)+", pem_input):
            raise ValueError("Only public certificates are allowed")
        certificates = x509.load_pem_x509_certificates(pem_input.encode())
        if not 1 <= len(certificates) <= 3:
            raise ValueError("Unsupported chain")
        cert = certificates[0]
        private_key = serialization.load_pem_private_key(key.encode(), password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 2048:
            raise ValueError("RSA signing key required")
        public_key = cert.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey) or public_key.public_numbers() != private_key.public_key().public_numbers():
            raise ValueError("Certificate and key do not match")
        root = _qz_root()
        own = len(certificates) == 1 and cert.issuer == cert.subject
        if own:
            cert.verify_directly_issued_by(cert)
            chain = certificates
        else:
            chain = list(certificates)
            if chain[-1].fingerprint(hashes.SHA256()) != root.fingerprint(hashes.SHA256()):
                chain.append(root)
            if len(chain) > 3:
                raise ValueError("QZ supports one intermediate")
            for index, parent in enumerate(chain[1:], 1):
                chain[index - 1].verify_directly_issued_by(parent)
                constraints = parent.extensions.get_extension_for_class(x509.BasicConstraints).value
                if not constraints.ca or (constraints.path_length is not None and index - 1 > constraints.path_length):
                    raise ValueError("Invalid certificate authority")
                try:
                    if not parent.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
                        raise ValueError("Issuer cannot sign certificates")
                except x509.ExtensionNotFound:
                    pass
        try:
            if not cert.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature:
                raise ValueError("Certificate cannot sign messages")
        except x509.ExtensionNotFound:
            pass
    except (ValueError, TypeError, OSError, InvalidSignature, UnsupportedAlgorithm,
            x509.ExtensionNotFound, x509.DuplicateExtension):
        raise HTTPException(503, "QZ Tray certificate or signing key is invalid or does not match") from None
    # QZ's native parser expects this delimiter, not concatenated PEM blocks.
    sent = chain if own else chain[:-1]
    public_pem = ("\n" + INTERMEDIATE_SEPARATOR + "\n").join(
        item.public_bytes(serialization.Encoding.PEM).decode().strip() for item in sent)
    return cert, private_key, public_pem, chain, own


def _identity():
    # Reload .env/.env.local and secret-file mounts so rotation cannot retain an old identity.
    try:
        config = QzSigningSettings()
    except ValidationError:
        raise HTTPException(503, "QZ Tray signing configuration is invalid") from None
    certificate = _pem(config.qz_tray_certificate, config.qz_tray_certificate_file)
    key = _pem(config.qz_tray_private_key, config.qz_tray_private_key_file)
    if bool(certificate) != bool(key):
        raise HTTPException(503, "QZ Tray signing configuration is incomplete")
    if not certificate:
        if config.qz_require_signing or config.environment.lower() not in {"development", "dev", "test"}:
            raise HTTPException(503, "QZ Tray signing is required; printing is unavailable until configured")
        return None
    cert, private_key, public_pem, chain, own = _parse_identity(certificate, key)
    if own and config.qz_tray_trust_mode == "official":
        raise HTTPException(503, "QZ Tray requires an officially issued certificate")
    now = datetime.now(timezone.utc)
    if any(not item.not_valid_before_utc <= now < item.not_valid_after_utc for item in chain):
        raise HTTPException(503, "QZ Tray certificate is expired or not yet valid")
    expires = min(item.not_valid_after_utc for item in chain)
    name = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    identity = {
        "subject": cert.subject.rfc4514_string(), "issuer": cert.issuer.rfc4514_string(),
        "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
        "valid_to": expires.isoformat(), "expires_soon": (expires - now).total_seconds() <= 30 * 86400,
        "trust": "self-signed" if own else "qz-issued",
        "activation": ("install-certificate" if name and name[0].value == "Escalar AI POS" else "administrator") if own else "remember",
    }
    return public_pem, private_key, identity


def qz_connection_settings() -> dict:
    identity = _identity()
    # Retain legacy manual approval only when no signer is configured at all.
    result = {"mode": "signed" if identity else "manual-approval",
              "certificate": identity[0] if identity else None}
    if identity:
        result["identity"] = identity[2]
    return result


def qz_certificate() -> str:
    identity = _identity()
    if not identity:
        raise HTTPException(503, "QZ Tray certificate is not configured")
    return identity[0]


def qz_sign_payload(payload: str) -> str:
    identity = _identity()
    if not identity:
        raise HTTPException(503, "QZ Tray signing key is not configured")
    signature = identity[1].sign(payload.encode(), padding.PKCS1v15(), hashes.SHA512())
    return base64.b64encode(signature).decode()
