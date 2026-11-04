# Public QZ trust anchor

`qz-root.pem` contains only the built-in public signing root from
[QZ Tray v2.3.0 Certificate.java](https://github.com/qzind/tray/blob/v2.3.0/src/qz/auth/Certificate.java).
It is NOT a customer certificate, license, private key, or localhost TLS root.
It grants no ability to sign. QZ Tray still performs its own trust, revocation,
and workstation consent checks. Do not infer official issuance from issuer text.

The QZ wire chain uses `--START INTERMEDIATE CERT--`. Preserve this format when
returning a verified leaf and intermediate; do not include private material.
Changing this anchor requires checking the official vendor release and tests.
