import asyncio
import os
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import Branch, Business, IdempotencyRecord, IntegrationCredential


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_concurrent_issuance_has_one_token_and_one_receipt(tmp_path, dialect):
    schema = None
    admin = None
    if dialect == "postgresql":
        url = os.environ.get("POS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Requires an isolated PostgreSQL schema")
        admin = sa.create_engine(url)
        schema = "test_issuance_" + uuid4().hex
        with admin.begin() as db:
            db.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = sa.create_engine(url, execution_options={"schema_translate_map": {None: schema}})
    else:
        engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'issuance.db'}", connect_args={"check_same_thread": False, "timeout": 15})
    sessions = sessionmaker(engine)

    def isolated_db():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    try:
        Base.metadata.create_all(engine)
        with sessions.begin() as db:
            business = Business(slug="isolated-issuance", name="Isolated issuance")
            db.add(business); db.flush()
            branch = Branch(business_id=business.id, slug="main", name="Main")
            db.add(branch); db.flush()
            branch_id = branch.id

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                async def post():
                    return await client.post("/api/v1/admin/integration-credentials",
                        headers={"X-Dev-Auth": "test-token", "X-Dev-Role": "superadmin", "Idempotency-Key": "same-operation"},
                        json={"branch_id": branch_id, "name": "Concurrent additional credential"})
                return await asyncio.gather(*[post() for _ in range(4)])
        responses = asyncio.run(run())
        assert sorted(response.status_code for response in responses) == [200, 200, 200, 201]
        assert len({response.json()["id"] for response in responses}) == 1
        assert sum("token" in response.json() for response in responses) == 1
        with sessions() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(IntegrationCredential)) == 1
            assert db.scalar(sa.select(sa.func.count()).select_from(IdempotencyRecord)) == 1
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()
        if schema:
            with admin.begin() as db:
                db.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()
