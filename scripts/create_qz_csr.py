"""Generate a NEW QZ signing key and public CSR on a private API-host directory.

Never generates a trusted certificate, installs trust, or replaces an existing key.
Only certificate-request.pem may be submitted to QZ's official issuance portal.
"""
import argparse
import csv
import os
from pathlib import Path
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def create_csr(directory: Path, organization: str, common_name: str = "pos.escalarai.tech"):
    directory = directory.resolve()
    workspace = Path(__file__).resolve().parents[2]
    if directory == workspace or workspace in directory.parents:
        raise ValueError("Use a private directory outside the workspace and web roots")
    if not organization.strip() or len(organization) > 180 or not common_name.strip() or len(common_name) > 180:
        raise ValueError("An organization and common name are required")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.name == "nt":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], check=True, capture_output=True, text=True)
        sid = next(csv.reader(result.stdout.strip().splitlines()))[1]
        subprocess.run(["icacls", str(directory), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F",
                        "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"], check=True, capture_output=True)
    # QZ's issuance portal specifies a 2048-bit RSA CSR, not the legacy own key.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(x509.NameOID.ORGANIZATION_NAME, organization.strip()),
        x509.NameAttribute(x509.NameOID.COMMON_NAME, common_name.strip()),
    ])).sign(key, hashes.SHA256()))
    for name, value in {
        "private-key.pem": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        "certificate-request.pem": csr.public_bytes(serialization.Encoding.PEM),
    }.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(value)
    return csr


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--common-name", default="pos.escalarai.tech")
    args = parser.parse_args()
    create_csr(args.directory, args.organization, args.common_name)
    print("New private key and public CSR created. No certificate issued or activated. Submit ONLY the CSR to QZ.")
