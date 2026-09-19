from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Branch, BranchSettings, Business, DeliveryBand, Order


def test_upgrade_and_downgrade_preserve_existing_fees_orders_and_settings(tmp_path, monkeypatch):
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'delivery-migration.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    engine = sa.create_engine(database_url)
    try:
        command.upgrade(config, "20260910_0020")
        with Session(engine) as db:
            business = Business(slug="existing", name="Existing")
            db.add(business)
            db.flush()
            branch = Branch(business_id=business.id, slug="main", name="Main", delivery_fee=7)
            db.add(branch)
            db.flush()
            db.add_all([
                BranchSettings(business_id=business.id, branch_id=branch.id, delivery_mode="bands",
                               fixed_delivery_fee=7, free_delivery_threshold=100, minimum_order_amount=20),
                DeliveryBand(business_id=business.id, branch_id=branch.id, minimum_km=0, maximum_km=5, fee=9),
                Order(business_id=business.id, branch_id=branch.id, number="old-order", channel="delivery",
                      subtotal=30, total=37, delivery_fee=7),
            ])
            db.commit()
        # Bootstrap migrations use live models. Reproduce the actual deployed 0020 schema.
        with engine.begin() as connection:
            connection.execute(sa.text("ALTER TABLE branch_settings DROP COLUMN delivery_policy"))
            tables = ["businesses", "branches", "branch_settings", "delivery_bands", "orders"]
            before = {table: connection.execute(sa.text(f"SELECT * FROM {table}")).mappings().all() for table in tables}
        command.upgrade(config, "20260912_0021")
        with engine.begin() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20260912_0021"
            assert connection.scalar(sa.text("SELECT delivery_policy FROM branch_settings")) is None
            column = next(item for item in sa.inspect(connection).get_columns("branch_settings") if item["name"] == "delivery_policy")
            assert column["nullable"] is True
            for table in tables:
                after = connection.execute(sa.text(f"SELECT * FROM {table}")).mappings().all()
                assert [{key: value for key, value in row.items() if key != "delivery_policy"} for row in after] == before[table]
            connection.execute(sa.text("UPDATE branch_settings SET delivery_policy = :policy"), {
                "policy": '{"neighborhoods": [], "origin": null, "outside_band_mode": "quote"}',
            })
        command.downgrade(config, "20260910_0020")
        with engine.connect() as connection:
            assert "delivery_policy" not in {item["name"] for item in sa.inspect(connection).get_columns("branch_settings")}
            for table in tables:
                assert connection.execute(sa.text(f"SELECT * FROM {table}")).mappings().all() == before[table]
        command.upgrade(config, "20260912_0021")
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT delivery_policy FROM branch_settings")) is None
    finally:
        engine.dispose()
        get_settings.cache_clear()
