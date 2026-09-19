"""Server-only QZ identity, shared by settings and operational printing."""

import base64
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
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
        if not re.fullmatch(r"(?:\s*-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*)+", certificate):
            raise ValueError("Only public certificates are allowed")
        certificates = x509.load_pem_x509_certificates(certificate.encode())
        cert = certificates[0]
        private_key = serialization.load_pem_private_key(key.encode(), password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 2048:
            raise ValueError("RSA signing key required")
        public_key = cert.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey) or public_key.public_numbers() != private_key.public_key().public_numbers():
            raise ValueError("Certificate and key do not match")
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise HTTPException(503, "QZ Tray certificate or signing key is invalid or does not match") from None
    public_pem = "\n".join(item.public_bytes(serialization.Encoding.PEM).decode().strip() for item in certificates)
    return cert, private_key, public_pem


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
        return None
    cert, private_key, public_pem = _parse_identity(certificate, key)
    now = datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise HTTPException(503, "QZ Tray certificate is expired or not yet valid")
    return public_pem, private_key


def qz_connection_settings() -> dict:
    identity = _identity()
    # Retain legacy manual approval only when no signer is configured at all.
    return {"mode": "signed" if identity else "manual-approval",
            "certificate": identity[0] if identity else None}


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
