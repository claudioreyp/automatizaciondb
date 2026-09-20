from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app import qz_signing as signing


def certificate(name, key, issuer=None, issuer_key=None, ca=False, days=365):
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer.subject if issuer else subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2)).not_valid_after(now + timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .sign(issuer_key or key, hashes.SHA256()))


def pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode().strip()


@pytest.fixture
def chain(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("QZ_TRAY_CERTIFICATE_FILE", "QZ_TRAY_PRIVATE_KEY_FILE", "QZ_TRAY_TRUST_MODE", "QZ_REQUIRE_SIGNING"):
        monkeypatch.delenv(name, raising=False)
    root_key, intermediate_key, leaf_key = [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(3)]
    root = certificate("Fixture QZ root - not commercial", root_key, ca=True)
    intermediate = certificate("Fixture intermediate", intermediate_key, root, root_key, ca=True)
    leaf = certificate("Escalar AI POS Test", leaf_key, intermediate, intermediate_key, days=20)
    monkeypatch.setattr(signing, "_qz_root", lambda: root)
    monkeypatch.setenv("QZ_TRAY_PRIVATE_KEY", leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE", pem(leaf) + "\n" + signing.INTERMEDIATE_SEPARATOR + "\n" + pem(intermediate))
    signing._parse_identity.cache_clear()
    return root, intermediate, leaf, leaf_key, intermediate_key, root_key


@pytest.mark.parametrize("delimiter", ["\n", "\n--START INTERMEDIATE CERT--\n"])
def test_official_chain_format_and_public_metadata(chain, monkeypatch, delimiter):
    root, intermediate, leaf, *_ = chain
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE", pem(leaf) + delimiter + pem(intermediate))
    monkeypatch.setenv("QZ_TRAY_TRUST_MODE", "official")
    result = signing.qz_connection_settings()
    assert result["mode"] == "signed"
    assert result["certificate"] == pem(leaf) + "\n--START INTERMEDIATE CERT--\n" + pem(intermediate)
    assert pem(root) not in result["certificate"]
    assert result["identity"]["trust"] == "qz-issued"
    assert result["identity"]["activation"] == "remember"
    assert result["identity"]["expires_soon"] is True
    assert result["identity"]["fingerprint_sha256"] == leaf.fingerprint(hashes.SHA256()).hex()
    assert "PRIVATE" not in str(result)


@pytest.mark.parametrize("problem", ["wrong-root", "wrong-chain", "expired-intermediate", "not-ca", "private-trailer", "forged-issuer", "wrong-key"])
def test_chain_errors_fail_before_any_public_identity(chain, monkeypatch, problem):
    root, intermediate, leaf, leaf_key, intermediate_key, root_key = chain
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if problem == "wrong-root":
        monkeypatch.setattr(signing, "_qz_root", lambda: certificate("Unknown root", stranger, ca=True))
    elif problem == "wrong-chain":
        intermediate = certificate("Fixture intermediate", stranger, root, root_key, ca=True)
    elif problem in {"expired-intermediate", "not-ca"}:
        intermediate = certificate("Fixture intermediate", intermediate_key, root, root_key, ca=problem != "not-ca", days=-1 if problem == "expired-intermediate" else 365)
    elif problem == "forged-issuer":
        leaf = certificate("Escalar AI POS Test", leaf_key, intermediate, stranger)
    elif problem == "wrong-key":
        leaf = certificate("Escalar AI POS Test", stranger, intermediate, intermediate_key)
    value = pem(leaf) + "\n--START INTERMEDIATE CERT--\n" + pem(intermediate)
    if problem == "private-trailer":
        value += "\n-----BEGIN PRIVATE KEY-----\nnever-expose\n-----END PRIVATE KEY-----"
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE", value)
    with pytest.raises(HTTPException) as error:
        signing.qz_connection_settings()
    assert error.value.status_code == 503
    assert "BEGIN" not in error.value.detail


def test_direct_qz_issuer_and_complete_pem_chain(chain, monkeypatch):
    root, intermediate, leaf, leaf_key, _, root_key = chain
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE", "\n".join(map(pem, [leaf, intermediate, root])))
    assert signing.qz_connection_settings()["identity"]["trust"] == "qz-issued"
    direct = certificate("Escalar AI POS", leaf_key, root, root_key)
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE", pem(direct))
    assert signing.qz_connection_settings()["certificate"] == pem(direct)
    assert signing.qz_connection_settings()["identity"]["expires_soon"] is False


@pytest.mark.parametrize("environment", ["production", "staging", "unknown"])
def test_missing_identity_never_becomes_anonymous_outside_dev(monkeypatch, tmp_path, environment):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("QZ_REQUIRE_SIGNING", "false")
    with pytest.raises(HTTPException) as error:
        signing.qz_connection_settings()
    assert error.value.status_code == 503


def test_official_mode_never_trusts_a_self_signed_qz_name(chain, monkeypatch):
    *_, leaf_key, _, _ = chain
    own = certificate("QZ Industries, LLC", leaf_key)
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE", pem(own))
    monkeypatch.setenv("QZ_TRAY_TRUST_MODE", "official")
    with pytest.raises(HTTPException):
        signing.qz_connection_settings()


def test_public_root_has_expected_qz_fingerprint():
    # Fixed upstream resource; tests of issued chains use a separate fixture CA.
    root = x509.load_pem_x509_certificate((signing.Path(signing.__file__).parent / "resources/qz/qz-root.pem").read_bytes())
    root.verify_directly_issued_by(root)
    assert root.fingerprint(hashes.SHA256()).hex() == "84005513af3158c0791d5ae6d2a737abe2e4e658bf1fe428a5496d1a0d26d068"
    assert root.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value == "qzindustries.com"
