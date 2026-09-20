import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fastapi import HTTPException

from app.qz_signing import qz_connection_settings, qz_sign_payload
from test_command_workflow import create_order


VARIABLES = ("QZ_TRAY_CERTIFICATE", "QZ_TRAY_PRIVATE_KEY", "QZ_TRAY_CERTIFICATE_FILE", "QZ_TRAY_PRIVATE_KEY_FILE", "QZ_REQUIRE_SIGNING", "QZ_TRAY_TRUST_MODE")


def identity(key=None, start=-1, end=1):
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Escalar AI POS Test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now + timedelta(days=start))
            .not_valid_after(now + timedelta(days=end)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode().strip(), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode().strip(), key


@pytest.fixture(autouse=True)
def isolated_signing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, cert, key):
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE", cert)
    monkeypatch.setenv("QZ_TRAY_PRIVATE_KEY", key)


def test_legacy_manual_approval_only_when_no_identity_configured():
    assert qz_connection_settings() == {"mode": "manual-approval", "certificate": None}
    with pytest.raises(HTTPException) as error:
        qz_sign_payload("print")
    assert error.value.status_code == 503


@pytest.mark.parametrize("source", ["inline", "escaped", "files", "dotenv"])
def test_identity_sources_and_exact_sha512_signature(monkeypatch, tmp_path, source):
    cert, key, private = identity()
    if source in {"inline", "escaped"}:
        configure(monkeypatch, cert.replace("\n", "\\n") if source == "escaped" else cert,
                  key.replace("\n", "\\n") if source == "escaped" else key)
    else:
        (tmp_path / "cert.pem").write_text(cert, encoding="utf-8")
        (tmp_path / "key.pem").write_text(key, encoding="utf-8")
        if source == "files":
            monkeypatch.setenv("QZ_TRAY_CERTIFICATE_FILE", str(tmp_path / "cert.pem"))
            monkeypatch.setenv("QZ_TRAY_PRIVATE_KEY_FILE", str(tmp_path / "key.pem"))
        else:
            (tmp_path / ".env.local").write_text(
                "QZ_TRAY_CERTIFICATE_FILE=cert.pem\nQZ_TRAY_PRIVATE_KEY_FILE=key.pem\n", encoding="utf-8")
    assert qz_connection_settings()["certificate"] == cert
    assert qz_connection_settings()["mode"] == "signed"
    assert qz_connection_settings()["identity"]["trust"] == "self-signed"
    payload = json.dumps({"call": "printers.detail", "timestamp": 123, "params": {"name": "Cocina ñ"}}, ensure_ascii=False)
    private.public_key().verify(base64.b64decode(qz_sign_payload(payload)), payload.encode(), padding.PKCS1v15(), hashes.SHA512())


@pytest.mark.parametrize("problem", ["missing-key", "missing-certificate", "mismatch", "malformed", "expired", "future", "ec", "weak", "unreadable", "empty", "conflict"])
def test_bad_identity_fails_closed_without_leaking_secrets(monkeypatch, tmp_path, problem):
    cert, key, _ = identity()
    if problem == "missing-key":
        key = ""
    elif problem == "missing-certificate":
        cert = ""
    elif problem == "mismatch":
        key = identity()[1]
    elif problem == "malformed":
        key = "DO-NOT-EXPOSE-THIS-SECRET"
    elif problem in {"expired", "future", "ec", "weak"}:
        options = {"expired": {"start": -2, "end": -1}, "future": {"start": 1, "end": 2},
                   "ec": {"key": ec.generate_private_key(ec.SECP256R1())},
                   "weak": {"key": rsa.generate_private_key(public_exponent=65537, key_size=1024)}}
        cert, key, _ = identity(**options[problem])
    configure(monkeypatch, cert, key)
    if problem in {"unreadable", "empty", "conflict"}:
        path = tmp_path / "private-location.pem"
        if problem == "empty":
            path.write_text("", encoding="utf-8")
        monkeypatch.setenv("QZ_TRAY_PRIVATE_KEY_FILE", str(path))
        if problem != "conflict":
            monkeypatch.setenv("QZ_TRAY_PRIVATE_KEY", "")
    for action in (qz_connection_settings, lambda: qz_sign_payload("print")):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 503
        assert "BEGIN" not in error.value.detail and "DO-NOT-EXPOSE" not in error.value.detail
        assert str(tmp_path) not in error.value.detail


def test_rotating_file_identity_does_not_keep_cached_certificate(monkeypatch, tmp_path):
    monkeypatch.setenv("QZ_TRAY_CERTIFICATE_FILE", "cert.pem")
    monkeypatch.setenv("QZ_TRAY_PRIVATE_KEY_FILE", "key.pem")
    for _ in range(2):
        cert, key, private = identity()
        (tmp_path / "cert.pem").write_text(cert, encoding="utf-8")
        (tmp_path / "key.pem").write_text(key, encoding="utf-8")
        assert qz_connection_settings()["certificate"] == cert
        private.public_key().verify(base64.b64decode(qz_sign_payload("exact")), b"exact", padding.PKCS1v15(), hashes.SHA512())


def test_settings_and_order_use_same_identity_and_keep_scope(client, tenant, auth_headers, monkeypatch):
    cert, key, private = identity()
    configure(monkeypatch, cert, key)
    order = create_order(client, tenant, auth_headers)
    settings_url = f"/api/v1/settings/branches/{tenant['branch_id']}/printing/qz"
    order_url = f"/api/v1/orders/{order['id']}/printing/qz"
    for url in (settings_url, order_url):
        response = client.get(url, headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["mode"] == "signed"
        assert response.json()["certificate"] == cert
        assert response.json()["identity"]["trust"] == "self-signed"
        assert response.headers["cache-control"] == "no-store"
        assert "PRIVATE KEY" not in response.text
    payload = json.dumps({"call": "printers.detail", "params": {}})
    response = client.post(f"/api/v1/settings/printing/qz/sign?branch_id={tenant['branch_id']}",
                           headers={**auth_headers, "X-Dev-Role": "cashier"}, json={"payload": payload})
    assert response.status_code == 200
    private.public_key().verify(base64.b64decode(response.json()["signature"]), payload.encode(), padding.PKCS1v15(), hashes.SHA512())
    outside = {**auth_headers, "X-Business-Id": str(tenant["other_business_id"]), "X-Branch-Id": str(tenant["other_branch_id"])}
    assert client.get(order_url, headers=outside).status_code == 404
    assert client.get(settings_url, headers=outside).status_code in {403, 404}
    monkeypatch.setenv("QZ_TRAY_PRIVATE_KEY", "broken")
    assert client.get(settings_url, headers=auth_headers).status_code == 503
    assert client.get(order_url, headers=auth_headers).status_code == 503


def hash_request(request):
    raw = request if isinstance(request, str) else json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    return {"payload": hashlib.sha256(raw.encode("utf-8")).hexdigest(), "request": raw}


@pytest.fixture
def signing_spy(monkeypatch):
    from app import settings_api
    spy = Mock(return_value="test-signature")
    monkeypatch.setattr(settings_api, "qz_sign_payload", spy)
    return spy


@pytest.mark.parametrize("role", ["superadmin", "owner", "manager", "cashier", "waiter", "kitchen"])
def test_actual_digest_protocol_signs_exact_hash_for_each_role(client, tenant, auth_headers, monkeypatch, role):
    cert, key, private = identity()
    configure(monkeypatch, cert, key)
    body = hash_request(' {"call":"printers.detail", "params":{"name":"Cocina \u00f1"}, "timestamp":123}\n')
    response = client.post(f"/api/v1/settings/printing/qz/sign?branch_id={tenant['branch_id']}&business_id={tenant['business_id']}",
                           headers={**auth_headers, "X-Dev-Role": role}, json=body)
    assert response.status_code == 200, response.text
    private.public_key().verify(base64.b64decode(response.json()["signature"]),
                                body["payload"].encode(), padding.PKCS1v15(), hashes.SHA512())


@pytest.mark.parametrize("role", ["superadmin", "owner", "manager", "cashier", "waiter", "kitchen"])
def test_hashed_operations_retain_printing_allowlist_and_legacy_compatibility(client, tenant, auth_headers, signing_spy, role):
    url = f"/api/v1/settings/printing/qz/sign?branch_id={tenant['branch_id']}&business_id={tenant['business_id']}"
    headers = {**auth_headers, "X-Dev-Role": role}
    pixel = {"type": "pixel", "format": "html", "flavor": "plain", "data": "<p>Ticket</p>"}
    thermal = {**pixel, "type": "raw", "options": {"language": "ESCPOS"}}
    raw = lambda command: {"type": "raw", "format": "command", "flavor": "hex", "data": command}
    allowed = [{"call": call, "params": {}} for call in ("printers.find", "printers.getDefault", "printers.detail")]
    allowed += [{"call": "print", "params": {"printer": {"name": "POS-80"}, "data": [pixel]}},
                {"call": "print", "params": {"printer": {"name": "POS-80"}, "data": [raw("1B40"), thermal, raw("0A1D564100")]}}]
    for request in allowed:
        body = hash_request({**request, "timestamp": 123})
        for wire in (body, {"payload": body["request"]}):
            response = client.post(url, headers=headers, json=wire)
            assert response.status_code == 200, response.text
            signing_spy.assert_called_with(wire["payload"])
    accepted = signing_spy.call_count
    forbidden = [{"call": call, "params": {}} for call in ("file.read", "file.write", "usb.sendData", "socket.open", "serial.openPort")]
    forbidden += [{"call": "print", "params": {"printer": {"name": "POS-80"}, **params}} for params in (
        {}, {"data": []}, {"data": ["file:///private"]},
        {"data": [{**pixel, "flavor": "file", "data": "file:///private"}]},
        {"data": [raw("1B700019FA")]}, {"data": [raw("0A1D5641001B700019FA")]},
        {"data": [{**thermal, "options": {"language": "ZPL"}}]},
    )]
    for request in forbidden:
        response = client.post(url, headers=headers, json=hash_request(request))
        assert response.status_code == 403, response.text
    assert signing_spy.call_count == accepted


@pytest.mark.parametrize("role", ["superadmin", "owner", "manager", "cashier", "waiter", "kitchen"])
def test_explicit_request_requires_exact_digest_for_all_roles(client, tenant, auth_headers, signing_spy, role):
    url = f"/api/v1/settings/printing/qz/sign?branch_id={tenant['branch_id']}&business_id={tenant['business_id']}"
    headers = {**auth_headers, "X-Dev-Role": role}
    original = hash_request({"call": "printers.detail", "params": {}, "timestamp": 123})
    invalid = [
        {**original, "request": original["request"] + " "},
        {**original, "request": original["request"].replace("123", "124")},
        {**original, "request": original["request"].replace("printers.detail", "file.read")},
        {**original, "payload": "0" * 64},
        *[{**original, "payload": value} for value in ("", "g" * 64, "a" * 63, "a" * 65, original["payload"].upper(), "\u00f1" * 64, original["request"])],
        {**original, "request": ""}, {**original, "request": {}},
        {**original, "request": "x" * 200001},
    ]
    for body in invalid:
        response = client.post(url, headers=headers, json=body)
        assert response.status_code == 422, response.text
    signing_spy.assert_not_called()


@pytest.mark.parametrize("role", ["cashier", "waiter", "kitchen"])
def test_opaque_hash_without_body_and_invalid_json_fail_closed_for_operational_roles(client, tenant, auth_headers, signing_spy, role):
    url = f"/api/v1/settings/printing/qz/sign?branch_id={tenant['branch_id']}"
    headers = {**auth_headers, "X-Dev-Role": role}
    digest = hash_request({"call": "printers.detail"})["payload"]
    invalid = [{"payload": digest}, {"payload": digest, "request": None}, {}, {"request": "{}"}]
    invalid += [hash_request(raw) for raw in ("not json", "null", "[]", '"print"', "123", '{}', '{"call":"print"}', '{"call":[]}', '{"call":{}}')]
    for body in invalid:
        response = client.post(url, headers=headers, json=body)
        assert response.status_code in {403, 422}, response.text
    signing_spy.assert_not_called()


def test_hashed_signing_keeps_business_branch_and_role_scope(client, tenant, auth_headers, signing_spy):
    body = hash_request({"call": "printers.detail", "params": {}, "timestamp": 123})
    url = f"/api/v1/settings/printing/qz/sign?branch_id={tenant['branch_id']}"
    assert client.post(url, headers={**auth_headers, "X-Dev-Role": "dispatcher"}, json=body).status_code == 403
    assert client.post(f"/api/v1/settings/printing/qz/sign?branch_id={tenant['other_branch_id']}",
                       headers=auth_headers, json=body).status_code == 403
    for role in ("cashier", "waiter", "kitchen"):
        assert client.post("/api/v1/settings/printing/qz/sign", headers={**auth_headers, "X-Dev-Role": role}, json=body).status_code == 403
    signing_spy.assert_not_called()


@pytest.mark.parametrize("branch_scoped", [False, True])
def test_admin_also_requires_known_print_content(client, tenant, auth_headers, signing_spy, branch_scoped):
    url = "/api/v1/settings/printing/qz/sign"
    if branch_scoped:
        url += f"?branch_id={tenant['branch_id']}"
    body = hash_request({"call": "printers.detail", "timestamp": 123})
    assert client.post(url, headers=auth_headers, json={"payload": body["payload"]}).status_code == 422
    assert client.post(url, headers=auth_headers, json=body).status_code == 200
    signing_spy.assert_called_with(body["payload"])
    assert client.post(url, headers=auth_headers, json={**body, "request": "{}"}).status_code == 422
    assert signing_spy.call_count == 1


@pytest.mark.parametrize("role", ["cashier", "waiter", "kitchen"])
@pytest.mark.parametrize("protocol", ["legacy", "digest"])
def test_operational_print_accepts_only_named_queues_without_changing_signed_bytes(client, tenant, auth_headers, signing_spy, role, protocol):
    url = f"/api/v1/settings/printing/qz/sign?branch_id={tenant['branch_id']}"
    headers = {**auth_headers, "X-Dev-Role": role}
    for name in ("EPSON TM-T20III Receipt", r"\\PRINT-SERVER\Caja", "Cocina \u00f1 (Red)", "  POS-80  "):
        request = {"call": "print", "params": {"printer": {"name": name}, "data": [
            {"type": "raw", "format": "command", "flavor": "hex", "data": "1B40"},
        ]}, "timestamp": 123}
        raw = json.dumps(request, ensure_ascii=False, indent=2) + "\n"
        body = {"payload": raw} if protocol == "legacy" else hash_request(raw)
        response = client.post(url, headers=headers, json=body)
        assert response.status_code == 200, response.text
        signing_spy.assert_called_with(body["payload"])
    assert signing_spy.call_count == 4


@pytest.mark.parametrize("role", ["cashier", "waiter", "kitchen"])
@pytest.mark.parametrize("protocol", ["legacy", "digest"])
def test_operational_print_rejects_alternate_destinations_even_with_name(client, tenant, auth_headers, signing_spy, role, protocol):
    url = f"/api/v1/settings/printing/qz/sign?branch_id={tenant['branch_id']}"
    headers = {**auth_headers, "X-Dev-Role": role}
    invalid_printers = [None, {}, [], "POS-80", 42]
    invalid_printers += [{"name": name} for name in (None, "", " \t\n", False, 42, [], {})]
    for extra in ({"host": "127.0.0.1"}, {"file": "C:/fixture/output.txt"}, {"port": 9100},
                  {"host": "192.0.2.1", "port": 9100}, {"host": None}, {"file": None},
                  {"port": None}, {"unknown": True}):
        invalid_printers.extend([extra, {"name": "POS-80", **extra}])
    data = [{"type": "pixel", "format": "html", "flavor": "plain", "data": "<p>Ticket</p>"}]
    invalid_params = [{"printer": printer, "data": data} for printer in invalid_printers]
    invalid_params += [{"data": data}, None, [], "invalid"]
    for params in invalid_params:
        request = {"call": "print", "params": params, "timestamp": 123}
        body = {"payload": json.dumps(request)} if protocol == "legacy" else hash_request(request)
        response = client.post(url, headers=headers, json=body)
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Only named installed printers are allowed"
    signing_spy.assert_not_called()


@pytest.mark.parametrize("role", ["superadmin", "owner", "manager"])
@pytest.mark.parametrize("branch_scoped", [False, True])
def test_admin_cannot_sign_alternate_destinations(client, tenant, auth_headers, signing_spy, role, branch_scoped):
    url = f"/api/v1/settings/printing/qz/sign?business_id={tenant['business_id']}"
    if branch_scoped:
        url += f"&branch_id={tenant['branch_id']}"
    headers = {**auth_headers, "X-Dev-Role": role}
    for printer in ({"host": "192.0.2.1", "port": 9100}, {"file": "C:/fixture/output.txt"}, None):
        body = hash_request({"call": "print", "params": {"printer": printer, "data": [
            {"type": "raw", "format": "command", "flavor": "hex", "data": "1B40"},
        ]}, "timestamp": 123})
        for wire in ({"payload": body["request"]}, {"payload": body["payload"]}, body):
            response = client.post(url, headers=headers, json=wire)
            assert response.status_code in {403, 422}, response.text
    signing_spy.assert_not_called()
