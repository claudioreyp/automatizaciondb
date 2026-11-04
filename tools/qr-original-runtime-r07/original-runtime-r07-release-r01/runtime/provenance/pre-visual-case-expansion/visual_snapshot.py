"""Fixed authenticated-control payload: a faithful private SQLite image.

Inert on import. The existing private control handler supplies authentication.
Call this with its shared control Lock, without holding that Lock beforehand.
No database path, SQL, table selector, scope or credential is accepted from HTTP.
"""
import base64
import hashlib
import json
import re
import sqlite3

SCHEMA = "escalarai.synthetic-pos-sqlite-snapshot.v1"
CASE_ID = "SMOKE01-GREETING-PIZZA-CASH"
SENDER = re.compile(r"519990[0-9]{5,9}\Z")
MAX_DATABASE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 6 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 1


def _need(condition, code):
    if not condition:
        raise ValueError(code)


def dispatch_visual_snapshot(sandbox, body, *, control_lock):
    """Fixed POST /control/visual-snapshot, after existing control auth."""
    _need(isinstance(body, dict) and set(body) == {"case_id", "sender"}, "VISUAL_FIXED_BODY")
    return snapshot(sandbox, control_lock=control_lock, case_id=body["case_id"], sender=body["sender"])


def table_counts(connection):
    """Identifiers come only from this database's schema, never the request."""
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
        for name in names}


def inspect_image(raw):
    _need(isinstance(raw, bytes) and 0 < len(raw) <= MAX_DATABASE_BYTES
        and raw.startswith(b"SQLite format 3\0"), "VISUAL_SQLITE_IMAGE")
    copy = sqlite3.connect(":memory:")
    try:
        copy.deserialize(raw)
        copy.execute("PRAGMA query_only=ON")
        _need(copy.execute("PRAGMA quick_check").fetchone() == ("ok",), "VISUAL_SQLITE_INTEGRITY")
        return table_counts(copy)
    finally:
        copy.close()


def snapshot(sandbox, *, control_lock, case_id, sender):
    """Serialize the same live engine under the fixed lifecycle/control lock.

    Both seeded synthetic tenants are retained byte-for-byte in the database
    image; the local viewer authenticates and exposes only business/branch 2/2.
    This is a copy of all tables, not a reconstruction of the JSON snapshot.
    """
    _need(case_id == CASE_ID, "VISUAL_FIXED_CASE")
    _need(isinstance(sender, str) and SENDER.fullmatch(sender), "VISUAL_SYNTHETIC_SENDER")
    _need(control_lock.acquire(timeout=LOCK_TIMEOUT_SECONDS), "VISUAL_CONTROL_BUSY")
    try:
        from sqlalchemy import select
        from sqlalchemy.pool import StaticPool
        from app.database import SessionLocal
        from app.models import Business, Branch, Order

        _need(sandbox["state"].get("case_id") == case_id, "VISUAL_CURRENT_CASE")
        _need(set(sandbox["tokens"]) == {2, 3}, "VISUAL_SYNTHETIC_TENANTS")
        engine = sandbox["engine"]
        _need(SessionLocal.kw.get("bind") is engine, "VISUAL_SAME_ENGINE")
        url = engine.url
        _need(url.drivername == "sqlite+pysqlite" and url.database == ":memory:"
            and url.host is None and url.username is None and url.password is None
            and url.port is None and not url.query and isinstance(engine.pool, StaticPool), "VISUAL_PRIVATE_MEMORY_ONLY")
        state = dict(sandbox["state"])
        with SessionLocal() as db:
            businesses = list(db.scalars(select(Business).order_by(Business.id)))
            branches = list(db.scalars(select(Branch).order_by(Branch.id)))
            _need([(b.id, b.slug) for b in businesses] == [(2, "smoke-2"), (3, "smoke-3")]
                and [(b.id, b.business_id) for b in branches] == [(2, 2), (3, 3)], "VISUAL_SYNTHETIC_TENANTS")
            orders = list(db.scalars(select(Order).where(Order.business_id == 2,
                Order.branch_id == 2, Order.whatsapp_chat_id == sender).order_by(Order.id)))
            _need(bool(orders) and all(o.customer_phone == sender for o in orders), "VISUAL_OWN_ORDER_REQUIRED")
            order_ids = [row.id for row in orders]

        # StaticPool is the sandbox's single in-memory DBAPI connection. Opening
        # a separate sqlite ':memory:' connection would silently copy no orders.
        with engine.connect() as connection:
            dbapi = connection.connection.driver_connection
            _need(isinstance(dbapi, sqlite3.Connection) and not dbapi.in_transaction, "VISUAL_COMMITTED_SQLITE_REQUIRED")
            attached = dbapi.execute("PRAGMA database_list").fetchall()
            _need(all(row[1] in {"main", "temp"} and row[2] == "" for row in attached)
                and sum(row[1] == "main" for row in attached) == 1, "VISUAL_PRIVATE_MEMORY_ONLY")
            _need(dbapi.execute("PRAGMA temp_store").fetchone() == (2,)
                and dbapi.execute("SELECT count(*) FROM sqlite_temp_master WHERE type='table'").fetchone() == (0,),
                "VISUAL_NO_TEMP_TABLES")
            pages = dbapi.execute("PRAGMA page_count").fetchone()[0]
            page_size = dbapi.execute("PRAGMA page_size").fetchone()[0]
            _need(0 < pages * page_size <= MAX_DATABASE_BYTES, "VISUAL_DATABASE_SIZE")
            _need(callable(getattr(dbapi, "serialize", None)), "VISUAL_SQLITE_SERIALIZE_REQUIRED")
            before_counts = table_counts(dbapi)
            raw = dbapi.serialize()
            _need(0 < len(raw) <= MAX_DATABASE_BYTES and inspect_image(raw) == before_counts, "VISUAL_SNAPSHOT_CHANGED")
            # Reads must not alter even technical rows, tokens, or source state.
            _need(dbapi.serialize() == raw and sandbox["state"] == state, "VISUAL_SOURCE_CHANGED")

        result = {"schema": SCHEMA, "synthetic_only": True, "database": "sqlite_memory",
            "case_id": case_id, "sender": sender, "business_id": 2, "branch_id": 2,
            "source_reset_count": state["reset_count"], "order_ids": order_ids,
            "all_source_tables_preserved": True, "source_unchanged": True,
            "image_tenants": [2, 3], "viewer_scope": {"business_id": 2, "branch_id": 2},
            "real_whatsapp": False, "physical_printing": False,
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "table_counts": before_counts, "snapshot_base64": base64.b64encode(raw).decode("ascii")}
        _need(len(json.dumps(result, separators=(",", ":")).encode("utf8")) <= MAX_RESPONSE_BYTES, "VISUAL_RESPONSE_SIZE")
        return result
    finally:
        control_lock.release()


def decode_snapshot(envelope):
    """Validate a saved control response before writing the local immutable copy."""
    _need(isinstance(envelope, dict) and envelope.get("schema") == SCHEMA
        and envelope.get("synthetic_only") is True and envelope.get("database") == "sqlite_memory"
        and envelope.get("case_id") == CASE_ID
        and envelope.get("business_id") == envelope.get("branch_id") == 2
        and envelope.get("viewer_scope") == {"business_id": 2, "branch_id": 2}
        and envelope.get("image_tenants") == [2, 3]
        and envelope.get("source_unchanged") is True and envelope.get("all_source_tables_preserved") is True
        and envelope.get("real_whatsapp") is False and envelope.get("physical_printing") is False,
        "VISUAL_SNAPSHOT_METADATA")
    _need(isinstance(envelope.get("sender"), str) and SENDER.fullmatch(envelope["sender"]), "VISUAL_SYNTHETIC_SENDER")
    encoded = envelope.get("snapshot_base64")
    _need(isinstance(encoded, str) and 0 < len(encoded) <= (MAX_DATABASE_BYTES + 2) // 3 * 4,
        "VISUAL_DATABASE_SIZE")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("VISUAL_BASE64") from error
    _need(len(raw) == envelope.get("bytes") and hashlib.sha256(raw).hexdigest() == envelope.get("sha256"),
        "VISUAL_SNAPSHOT_DIGEST")
    _need(inspect_image(raw) == envelope.get("table_counts"), "VISUAL_SNAPSHOT_COUNTS")
    copy = sqlite3.connect(":memory:")
    try:
        copy.deserialize(raw)
        _need(copy.execute("SELECT id, slug FROM businesses ORDER BY id").fetchall() == [(2, "smoke-2"), (3, "smoke-3")]
            and copy.execute("SELECT id, business_id FROM branches ORDER BY id").fetchall() == [(2, 2), (3, 3)],
            "VISUAL_SYNTHETIC_TENANTS")
        own = copy.execute("SELECT id FROM orders WHERE business_id=2 AND branch_id=2 AND whatsapp_chat_id=? AND customer_phone=? ORDER BY id",
            (envelope["sender"], envelope["sender"])).fetchall()
        _need([row[0] for row in own] == envelope.get("order_ids") and bool(own), "VISUAL_OWN_ORDER_REQUIRED")
    finally:
        copy.close()
    return raw
