"""Shared checks for an existing POS transport reservation; no state changes."""
import hashlib
import secrets

from fastapi import HTTPException


def validate_delivery_claim(row, delivery_token):
    if not row or not row.claim_hash or not secrets.compare_digest(
            row.claim_hash, hashlib.sha256(delivery_token.encode()).hexdigest()):
        raise HTTPException(409, "Response claim is stale")


def validate_delivery_ack_receipt(status, receipt):
    if status == "sent" and not str(receipt.get("transport_id", "")).strip():
        raise HTTPException(422, "Confirmed transport receipt is required")
    if status == "not_sent" and receipt.get("transport_attempted") is not False:
        raise HTTPException(422, "Only a response never attempted by the transport may be released")
