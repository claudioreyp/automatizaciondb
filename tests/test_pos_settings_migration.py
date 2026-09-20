from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.config import get_settings


def _config(database_path: Path) -> Config:
    repository_root = Path(__file__).resolve().parents[1]
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+pysqlite:///{database_path.as_posix()}",
    )
    return config


def test_upgrade_from_0017_backfills_existing_tenant(tmp_path, monkeypatch):
    database_path = tmp_path / "existing-tenant.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _config(database_path)
    command.upgrade(config, "20260831_0017")

    engine = sa.create_engine(database_url)
    timestamp = "2026-09-02 12:00:00"
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO businesses "
                "(id, slug, name, status, plan, currency, timezone, country_code, version, "
                "auto_accept_payment_evidence, auto_accept_limit, created_at, updated_at) "
                "VALUES (1, 'pizza-house', 'Pizza House', 'active', 'pro', 'PEN', "
                "'America/Lima', 'PE', 1, 0, 0, :timestamp, :timestamp)"
            ),
            {"timestamp": timestamp},
        )
        connection.execute(
            sa.text(
                "INSERT INTO branches "
                "(id, business_id, slug, name, opening_hours, accepted_payment_methods, "
                "delivery_enabled, takeaway_enabled, delivery_fee, whatsapp_status, active, "
                "version, created_at, updated_at) "
                "VALUES (1, 1, 'matriz', 'Sucursal principal', :hours, :methods, 1, 1, 5, "
                "'unknown', 1, 1, :timestamp, :timestamp)"
            ),
            {
                "hours": '{"lunes": [{"open": "09:00", "close": "23:00"}]}',
                "methods": '["cash", "card"]',
                "timestamp": timestamp,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO memberships "
                "(id, auth_user_id, email, full_name, business_id, branch_id, role, active, "
                "created_at, updated_at) "
                "VALUES (1, 'owner-user', 'owner@example.com', 'Propietario Principal', "
                "1, NULL, 'owner', 1, :timestamp, :timestamp)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20260920_0024"
        settings = connection.execute(
            sa.text(
                "SELECT fixed_delivery_fee, payment_methods, created_at, updated_at "
                "FROM branch_settings WHERE branch_id = 1"
            )
        ).mappings().one()
        assert float(settings["fixed_delivery_fee"]) == 5
        assert '"counter": ["cash", "card"]' in settings["payment_methods"]
        assert settings["created_at"] and settings["updated_at"]

        schedule = connection.execute(
            sa.text(
                "SELECT id, created_at, updated_at FROM service_schedules "
                "WHERE branch_id = 1 AND kind = 'primary'"
            )
        ).mappings().one()
        assert schedule["created_at"] and schedule["updated_at"]
        shift = connection.execute(
            sa.text(
                "SELECT created_at, updated_at FROM schedule_shifts WHERE schedule_id = :schedule_id"
            ),
            {"schedule_id": schedule["id"]},
        ).mappings().one()
        assert shift["created_at"] and shift["updated_at"]

        member = connection.execute(
            sa.text(
                "SELECT id, first_name, last_name, created_at, updated_at "
                "FROM staff_members WHERE auth_user_id = 'owner-user'"
            )
        ).mappings().one()
        assert (member["first_name"], member["last_name"]) == ("Propietario", "Principal")
        assert member["created_at"] and member["updated_at"]
        assert connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM staff_member_roles "
                "WHERE staff_member_id = :member_id AND role = 'owner'"
            ),
            {"member_id": member["id"]},
        ) == 1
        assert connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM staff_member_branches "
                "WHERE staff_member_id = :member_id AND branch_id = 1"
            ),
            {"member_id": member["id"]},
        ) == 1
    engine.dispose()
    get_settings.cache_clear()
