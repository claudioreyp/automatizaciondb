"""Create a private, self-signed QZ identity outside the workspace. Never overwrite keys."""

import argparse
import csv
import os
from pathlib import Path
import subprocess
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def create_identity(directory: Path) -> x509.Certificate:
    directory = directory.resolve()
    workspace = Path(__file__).resolve().parents[2]
    if directory == workspace or workspace in directory.parents:
        raise ValueError("Keep the signing identity outside the workspace and web directories")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.name == "nt":
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  check=True, capture_output=True, text=True)
        sid = next(csv.reader(identity.stdout.strip().splitlines()))[1]
        # Protect the directory before any private material is written.
        subprocess.run(["icacls", str(directory), "/inheritance:r", "/grant:r",
                        f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                        "*S-1-5-32-544:(OI)(CI)F"], check=True, capture_output=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([
        x509.NameAttribute(x509.NameOID.ORGANIZATION_NAME, "Escalar AI POS"),
        x509.NameAttribute(x509.NameOID.COMMON_NAME, "Escalar AI POS"),
    ])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=730))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                           key_encipherment=False, data_encipherment=False, key_agreement=False,
                           key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False),
                           critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    files = {
        "private-key.pem": key.private_bytes(serialization.Encoding.PEM,
                                             serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()),
        "digital-certificate.txt": cert.public_bytes(serialization.Encoding.PEM),
    }
    for filename, data in files.items():
        descriptor = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
    return cert


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    certificate = create_identity(arguments.directory)
    print("Created Escalar AI POS identity. Private key remains in the API host only.")
    print("Certificate SHA256:", certificate.fingerprint(hashes.SHA256()).hex())
    print("Valid until:", certificate.not_valid_after_utc.isoformat())
