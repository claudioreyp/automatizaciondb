"""Freeze local POS data and media, then upgrade only a disposable recovery copy."""
import hashlib
import json
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile, ZIP_DEFLATED

from alembic import command
import sqlalchemy as sa

from app.models import AuthSecurityState, Membership
from scripts.transfer_sqlite_to_postgres import (
    ROOT, Base, snapshot, sqlite_engine, read_rows, fingerprint, migration_config, inventory, validate_schema,
)


def prepare():
    frozen = snapshot(ROOT / "impulsa_pos.db", ROOT / "backups")
    working = snapshot(frozen, ROOT / "backups")
    old = sqlite_engine(frozen)
    engine = sa.create_engine(f"sqlite:///{working.as_posix()}")
    with old.connect() as connection:
        names = set(sa.inspect(connection).get_table_names()) - {"alembic_version"}
        original = {name: fingerprint(read_rows(connection, Base.metadata.tables[name]), Base.metadata.tables[name]) for name in names}
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
        members = connection.execute(sa.select(Membership.__table__)).mappings().all()
        supers = {row["auth_user_id"] for row in members if row["role"] == "superadmin"}
        owners = set()
        for row in members:
            if row["role"] != "owner" or row["auth_user_id"] in supers:
                continue
            try:
                UUID(row["auth_user_id"])
            except ValueError:
                continue
            owners.add(row["auth_user_id"])
        for subject in owners:
            connection.execute(AuthSecurityState.__table__.insert().values(
                auth_user_id=subject, version=0, requires_password_reset=True))
        for name, digest in original.items():
            assert fingerprint(read_rows(connection, Base.metadata.tables[name]), Base.metadata.tables[name]) == digest
        validate_schema(connection, source=True)
        tables = inventory(connection)
    media = []
    archive = working.with_suffix(".uploads.zip")
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        for path in sorted((ROOT / "uploads").rglob("*")):
            if path.is_file():
                relative = path.relative_to(ROOT / "uploads").as_posix()
                bundle.write(path, relative)
                media.append({"path": relative, "size": path.stat().st_size,
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    report = {"original_backup": str(frozen), "source": str(working), "tables": tables,
              "original_tables_unchanged": len(original), "media_backup": str(archive), "media": media,
              "owners_require_password_reset": len(owners), "auth_passwords_included": False}
    working.with_suffix(".manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    old.dispose()
    engine.dispose()
    print(json.dumps({"source": str(working), "original_tables_unchanged": len(original),
                      "rows": sum(t["rows"] for t in tables.values()), "media_files": len(media),
                      "owners_pending_renewal": len(owners)}))


if __name__ == "__main__":
    prepare()
