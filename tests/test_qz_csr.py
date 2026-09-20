from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from scripts.create_qz_csr import create_csr


def test_new_csr_matches_private_key_without_exposing_it(tmp_path):
    target = tmp_path / "private-host-directory"
    create_csr(target, " Escalar AI POS ")
    private = serialization.load_pem_private_key((target / "private-key.pem").read_bytes(), password=None)
    public = (target / "certificate-request.pem").read_bytes()
    csr = x509.load_pem_x509_csr(public)
    assert csr.is_signature_valid
    assert csr.public_key().public_numbers() == private.public_key().public_numbers()
    assert private.key_size == 2048
    assert csr.subject.get_attributes_for_oid(x509.NameOID.ORGANIZATION_NAME)[0].value == "Escalar AI POS"
    assert b"PRIVATE KEY" not in public
    assert b"BEGIN CERTIFICATE-----" not in public
    before = (target / "private-key.pem").read_bytes()
    with pytest.raises(FileExistsError):
        create_csr(target, "Escalar AI POS")
    assert (target / "private-key.pem").read_bytes() == before


def test_csr_refuses_the_workspace():
    workspace = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="outside the workspace"):
        create_csr(workspace / "never-create-qz-key", "Escalar AI POS")


def test_csr_requires_an_identity_before_creating_files(tmp_path):
    target = tmp_path / "invalid-identity"
    with pytest.raises(ValueError):
        create_csr(target, " ")
    assert not target.exists()
