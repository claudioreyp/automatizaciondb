"""Provision a private DML-only API login after a verified Supabase transfer.

No existing role password is changed. Credentials stay in the ignored target
environment file; never pass them on the command line.
"""
import argparse
import json
from pathlib import Path
import secrets

from dotenv import dotenv_values, set_key
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.database import Base
from scripts.transfer_sqlite_to_postgres import ROOT, TransferError, destination, validate_schema

ROLE = "escalar_pos_api"


def configure(env_file, project):
    admin = destination(env_file, project)
    settings = dotenv_values(env_file)
    prior_url = settings.get("RUNTIME_DATABASE_URL")
    if prior_url:
        url = make_url(prior_url)
        expected_user = ROLE + ("." + project if ".pooler." in (admin.url.host or "") else "")
        if (url.host != admin.url.host or url.username != expected_user or url.database != "postgres"
                or url.port != admin.url.port or url.query != admin.url.query
                or url.drivername != admin.url.drivername):
            raise TransferError("Unexpected existing runtime connection; refusing to replace it.")
        password = url.password
    else:
        password = secrets.token_hex(32)
        user = ROLE + ("." + project if ".pooler." in (admin.url.host or "") else "")
        url = admin.url.set(username=user, password=password)
        # Preserve the candidate before the remote write, including uncertain outcomes.
        set_key(env_file, "RUNTIME_DATABASE_URL", url.render_as_string(hide_password=False))
    try:
        with admin.begin() as db:
            db.exec_driver_sql("SET LOCAL ROLE postgres")
            validate_schema(db)
            if not db.scalar(sa.text("SELECT pg_try_advisory_xact_lock(1936748403)")):
                raise TransferError("Another migration operation is running.")
            exists = db.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": ROLE})
            if exists and not prior_url:
                raise TransferError("Runtime role already exists; no password will be changed.")
            if exists and db.scalar(sa.text("""
                SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
                    OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid)
                FROM pg_roles WHERE rolname=:role
            """), {"role": ROLE}):
                raise TransferError("Existing runtime role has elevated privileges; manual review required.")
            if not exists:
                if not password or any(char not in "0123456789abcdef" for char in password):
                    raise TransferError("Expected a locally generated runtime credential.")
                db.exec_driver_sql(f"CREATE ROLE {ROLE} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 15")
            db.exec_driver_sql(f"GRANT CONNECT ON DATABASE postgres TO {ROLE}")
            db.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
            for table in Base.metadata.sorted_tables:
                name = db.dialect.identifier_preparer.quote(table.name)
                db.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{name} TO {ROLE}")
                has_policy = db.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename=:table AND policyname='pos_api_server_access')"), {"table": table.name})
                if not has_policy:
                    # This server-only role enforces tenancy in FastAPI, not PostgREST.
                    db.exec_driver_sql(f"CREATE POLICY pos_api_server_access ON public.{name} TO {ROLE} USING (true) WITH CHECK (true)")
                for column in table.primary_key.columns:
                    sequence = db.scalar(sa.text("SELECT pg_get_serial_sequence(:table,:column)"), {"table": "public." + table.name, "column": column.name})
                    if sequence:
                        _, sequence_name = sequence.split(".", 1)
                        quoted = db.dialect.identifier_preparer.quote(sequence_name)
                        db.exec_driver_sql(f"GRANT USAGE, SELECT ON SEQUENCE public.{quoted} TO {ROLE}")
            db.exec_driver_sql(f"GRANT SELECT ON public.alembic_version TO {ROLE}")
            if not db.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename='alembic_version' AND policyname='pos_api_version_read')")):
                db.exec_driver_sql(f"CREATE POLICY pos_api_version_read ON public.alembic_version FOR SELECT TO {ROLE} USING (true)")
            db.exec_driver_sql(f"ALTER ROLE {ROLE} SET statement_timeout = '30s'")
        runtime = sa.create_engine(url, hide_parameters=True, pool_pre_ping=True, connect_args={"connect_timeout": 20})
        try:
            with runtime.connect() as db:
                if db.scalar(sa.text("SELECT current_user")) != ROLE:
                    raise TransferError("Unexpected runtime database identity.")
                if db.scalar(sa.text("SELECT count(*) FROM businesses")) == 0:
                    raise TransferError("Runtime cannot read the restored businesses.")
                # Resolve by catalog OID: the restricted role deliberately lacks
                # USAGE on auth, so resolving 'auth.users' by name is forbidden.
                if db.scalar(sa.text("SELECT has_table_privilege(current_user,c.oid,'SELECT') FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='auth' AND c.relname='users'")):
                    raise TransferError("Runtime has unexpected access to Auth tables.")
                if db.scalar(sa.text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")):
                    raise TransferError("Runtime has unexpected schema creation privileges.")
            set_key(env_file, "DATABASE_URL", url.render_as_string(hide_password=False))
        finally:
            runtime.dispose()
    finally:
        admin.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-env", type=Path, default=ROOT / ".env.supabase-target")
    parser.add_argument("--confirm-project", required=True)
    args = parser.parse_args()
    try:
        configure(args.target_env, args.confirm_project)
        print(json.dumps({"runtime_login_verified": True, "auth_tables_accessible": False, "schema_creation_allowed": False, "applications_switched": False}))
    except TransferError as error:
        print(str(error))
        raise SystemExit(1)
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "automatic_retry": False}))
        raise SystemExit(1)
