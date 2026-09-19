import asyncio
import os
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app import password_resets
from app.database import Base, get_db
from app.main import app
from app.models import Business, Membership, PasswordResetOperation


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_same_identity_across_restaurants_has_only_one_concurrent_reset(tmp_path, monkeypatch, dialect):
    schema = None
    admin = None
    if dialect == "postgresql":
        url = os.environ.get("POS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Requires an isolated PostgreSQL schema")
        admin = sa.create_engine(url)
        schema = "test_reset_" + uuid4().hex
        with admin.begin() as db:
            db.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = sa.create_engine(url, execution_options={"schema_translate_map": {None: schema}})
    else:
        engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'reset.db'}", connect_args={"check_same_thread": False, "timeout": 10})
    sessions = sessionmaker(engine)
    release = Event()
    sent = Event()
    calls = []
    subject = str(uuid4())
    identity = {"id": subject, "email": "owner@example.test", "app_metadata": {}}
    settings = SimpleNamespace(supabase_url="https://auth.example.test", auth_admin_secret="test-reset-secret-with-at-least-32-bytes", pos_public_base_url="http://localhost:5173")
    monkeypatch.setattr(password_resets, "get_settings", lambda: settings)
    monkeypatch.setattr(password_resets, "provider_headers", lambda: {})
    monkeypatch.setattr(password_resets, "provider_user", lambda _: identity)

    def put(*args, **kwargs):
        calls.append(1)
        sent.set()
        assert release.wait(20)
        return httpx.Response(200, json={**identity, "app_metadata": kwargs["json"]["app_metadata"]})
    monkeypatch.setattr(password_resets.httpx, "put", put)

    def isolated_db():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    try:
        Base.metadata.create_all(engine)
        if schema:
            with engine.connect() as db:
                assert set(sa.inspect(db).get_table_names(schema=schema)) == set(Base.metadata.tables)
        with sessions.begin() as db:
            businesses = [Business(slug=f"reset-{i}", name="Isolated") for i in range(2)]
            db.add_all(businesses); db.flush()
            members = [Membership(auth_user_id=subject, email=identity["email"], role="owner", business_id=b.id) for b in businesses]
            db.add_all(members); db.flush()
            paths = [f"/api/v1/admin/businesses/{m.business_id}/memberships/{m.id}/password-reset" for m in members]

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                async def post(path):
                    return await client.post(path, headers={"X-Dev-Auth": "test-token", "X-Dev-Role": "superadmin", "Idempotency-Key": str(uuid4())}, json={"password": "Isolated-Owner-Test-482!", "expected_version": 0})
                first = asyncio.create_task(post(paths[0]))
                try:
                    assert await asyncio.to_thread(sent.wait, 20)
                    second = await asyncio.wait_for(post(paths[1]), 15)
                    assert second.status_code == 409
                finally:
                    release.set()
                assert (await first).json()["status"] == "succeeded"
        asyncio.run(run())
        assert calls == [1]
        with sessions() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(PasswordResetOperation)) == 1
    finally:
        release.set()
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()
        if schema:
            with admin.begin() as db:
                db.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()
