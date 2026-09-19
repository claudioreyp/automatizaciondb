from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

from app.qz_activation import activation_bundle
from scripts.create_qz_identity import create_identity


@pytest.fixture
def own_identity(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Escalar AI POS")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30)).sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode().strip()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE", pem)
    monkeypatch.setenv("QZ_TRAY_PRIVATE_KEY", private)
    return cert, pem, private


@pytest.mark.parametrize("role", ["owner", "manager", "cashier", "waiter", "kitchen"])
def test_download_is_public_only_and_scoped(client, tenant, auth_headers, own_identity, role):
    cert, pem, private = own_identity
    path = f"/api/v1/settings/branches/{tenant['branch_id']}/printing/qz/activation"
    response = client.get(path, headers={**auth_headers, "X-Dev-Role": role})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    with ZipFile(BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"digital-certificate.txt", "activar-windows.ps1",
                                        "activar-macos-linux.sh", "ACTIVAR-WINDOWS.cmd", "LEEME.txt"}
        assert archive.read("digital-certificate.txt").decode().strip() == pem
        combined = b"\n".join(archive.read(name) for name in archive.namelist())
        assert private.encode() not in combined
        assert b"-----BEGIN PRIVATE KEY-----" not in combined
        fingerprint = cert.fingerprint(hashes.SHA256()).hex().encode()
        assert fingerprint in archive.read("ACTIVAR-WINDOWS.cmd")
        assert fingerprint in archive.read("LEEME.txt")
        assert b"\r" not in archive.read("activar-macos-linux.sh")
    assert client.get(path, headers={**auth_headers, "X-Dev-Role": "dispatcher"}).status_code == 403
    outside = {**auth_headers, "X-Business-Id": str(tenant['other_business_id']),
               "X-Branch-Id": str(tenant['other_branch_id'])}
    assert client.get(path, headers=outside).status_code in (403, 404)
    assert client.get(path).status_code in (401, 403)


def test_download_without_signing_identity_fails_closed(client, tenant, auth_headers):
    response = client.get(f"/api/v1/settings/branches/{tenant['branch_id']}/printing/qz/activation", headers=auth_headers)
    assert response.status_code == 503
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("extra", ["private-key", "other-text"])
def test_public_endpoints_reject_concatenated_private_or_unexpected_material(client, tenant, auth_headers, own_identity, monkeypatch, extra):
    _, pem, private = own_identity
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE", pem + "\n" + (private if extra == "private-key" else "do-not-expose-extra-data"))
    for path in (f"/api/v1/settings/branches/{tenant['branch_id']}/printing/qz/activation",
                 f"/api/v1/settings/branches/{tenant['branch_id']}/printing/qz",
                 "/api/v1/settings/printing/qz/certificate"):
        response = client.get(path, headers=auth_headers)
        assert response.status_code == 503
        assert "-----BEGIN" not in response.text
        assert "do-not-expose-extra-data" not in response.text


def test_bundle_defensively_rejects_extra_material(own_identity, monkeypatch):
    from fastapi import HTTPException
    _, pem, private = own_identity
    monkeypatch.setattr("app.qz_activation.qz_certificate", lambda: pem + "\n" + private)
    with pytest.raises(HTTPException) as error:
        activation_bundle()
    assert error.value.status_code == 409


def test_package_does_not_activate_commercial_or_other_identity(monkeypatch, own_identity):
    from fastapi import HTTPException
    cert, _, _ = own_identity
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Other POS")])
    different = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                 .serial_number(x509.random_serial_number()).not_valid_before(cert.not_valid_before_utc)
                 .not_valid_after(cert.not_valid_after_utc).sign(key, hashes.SHA256()))
    monkeypatch.setattr("app.qz_activation.qz_certificate", lambda: different.public_bytes(serialization.Encoding.PEM).decode())
    with pytest.raises(HTTPException) as error:
        activation_bundle()
    assert error.value.status_code == 409


def test_generator_refuses_workspace_and_existing_directories(tmp_path):
    with pytest.raises(ValueError):
        create_identity(Path(__file__).resolve().parents[1] / "must-not-create-qz-keys")
    with pytest.raises(FileExistsError):
        create_identity(tmp_path)
    target = tmp_path / "private-identity"
    cert = create_identity(target)
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert cert.public_key().key_size == 3072
    private = serialization.load_pem_private_key((target / "private-key.pem").read_bytes(), password=None)
    assert private.public_key().public_numbers() == cert.public_key().public_numbers()
    before = (target / "private-key.pem").read_bytes()
    with pytest.raises(FileExistsError):
        create_identity(target)
    assert (target / "private-key.pem").read_bytes() == before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows activation validation")
@pytest.mark.parametrize("problem", ["fingerprint", "private-material", "malformed"])
def test_windows_validation_stops_before_machine_changes(tmp_path, own_identity, problem):
    cert, pem, private = own_identity
    fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    if problem == "fingerprint":
        fingerprint = "0" * 64
    elif problem == "private-material":
        pem += private
    else:
        pem = "not a certificate"
    public = tmp_path / "certificate.txt"
    public.write_text(pem, encoding="ascii")
    script = Path(__file__).resolve().parents[1] / "app/resources/qz/activate-windows.ps1"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(script), "-CertificatePath",
                             str(public), "-ExpectedSha256", fingerprint, "-CheckOnly"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "BEGIN PRIVATE KEY" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Java properties parser guard")
def test_windows_preserves_all_java_property_key_forms():
    script = Path(__file__).resolve().parents[1] / "app/resources/qz/activate-windows.ps1"
    command = r"""
    $ast = [Management.Automation.Language.Parser]::ParseFile('__SCRIPT__', [ref]$null, [ref]$null)
    $function = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-CustomRoots'}, $true)
    . ([scriptblock]::Create($function.Extent.Text))
    foreach ($line in @('trustedRootCert=C:/other.crt', 'trustedRootCert C:/other.crt', 'authcert.override:other.crt', 'trus\tedRootCert other.crt', '\u0074rustedRootCert other.crt', 'trustedR\')) {
        if (-not (Test-CustomRoots @($line))) { throw 'An existing root was missed' }
    }
    if (Test-CustomRoots @('# trustedRootCert=comment', '! authcert.override=comment', 'wss.keystore=C:\\Users\\example\\qz.jks', 'wss.alias=qz-tray')) { throw 'Ordinary QZ settings were blocked' }
    """.replace("__SCRIPT__", str(script).replace("'", "''"))
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
