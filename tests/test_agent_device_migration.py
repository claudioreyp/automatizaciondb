from pathlib import Path
import json

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.config import get_settings


def test_additive_agent_migration_preserves_existing_primary_and_device(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{(tmp_path / 'agent-migration.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    engine = sa.create_engine(url)
    device_columns = ["credential_expires_at", "staff_session_hash", "staff_session_expires_at", "session_staff_id", "session_access_hash", "failed_pin_attempts", "pin_locked_until"]
    try:
        command.upgrade(config, "20260912_0021")
        with engine.begin() as db:
            # The bootstrap uses live metadata; restore the real pre-upgrade shape.
            for name in ["agent_name", "agent_menu_images"]:
                db.execute(sa.text(f"ALTER TABLE branches DROP COLUMN {name}"))
            for name in device_columns:
                db.execute(sa.text(f"ALTER TABLE paired_devices DROP COLUMN {name}"))
            db.execute(sa.text("INSERT INTO businesses (id, slug, name, status, plan, currency, timezone, auto_accept_payment_evidence, auto_accept_limit, created_at, updated_at) VALUES (1, 'old', 'Old', 'active', 'basic', 'PEN', 'America/Lima', 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
            db.execute(sa.text("INSERT INTO branches (id, business_id, slug, name, opening_hours, accepted_payment_methods, delivery_enabled, takeaway_enabled, delivery_fee, whatsapp_status, active, menu_card_storage_path, created_at, updated_at) VALUES (1, 1, 'main', 'Main', '{}', '[]', 1, 1, 9, 'unknown', 1, 'existing-menu.png', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
            db.execute(sa.text("INSERT INTO paired_devices (id, business_id, branch_id, name, token_hash, active, version, created_at, updated_at) VALUES (1, 1, 1, 'Printer', 'legacy-hash', 1, 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
            before = {table: db.execute(sa.text(f"SELECT * FROM {table}")).mappings().all() for table in ["branches", "paired_devices"]}
            tables = sa.inspect(db).get_table_names()
        command.upgrade(config, "head")
        with engine.connect() as db:
            assert sa.inspect(db).get_table_names() == tables
            gallery = json.loads(db.scalar(sa.text("SELECT agent_menu_images FROM branches")))
            assert gallery == [{"id": "legacy", "path": "existing-menu.png"}]
            assert db.scalar(sa.text("SELECT credential_expires_at FROM paired_devices")) is None
            assert db.scalar(sa.text("SELECT token_hash FROM paired_devices")) == "legacy-hash"
        command.downgrade(config, "20260912_0021")
        with engine.connect() as db:
            for table in before:
                assert db.execute(sa.text(f"SELECT * FROM {table}")).mappings().all() == before[table]
    finally:
        engine.dispose()
        get_settings.cache_clear()
