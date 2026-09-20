"""Durable issuance receipts deliberately exclude recoverable bearer secrets."""

import hashlib
import json
import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import IdempotencyRecord


def operation_scope(user_id: str) -> str:
    actor = hashlib.sha256(user_id.encode()).hexdigest()[:32]
    return f"admin.credential.create:{actor}"


def validate_operation_key(key: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key):
        raise HTTPException(422, "Idempotency-Key debe tener 1 a 128 caracteres alfanumericos, puntos o guiones")
    return key


def issuance_fingerprint(payload) -> str:
    body = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def issuance_record(db: Session, user_id: str, business_id: int, key: str):
    return db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.business_id == business_id,
        IdempotencyRecord.scope == operation_scope(user_id),
        IdempotencyRecord.idempotency_key == key,
    ))
