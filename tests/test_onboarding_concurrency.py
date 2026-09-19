import asyncio

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import Business, IntegrationCredential, Membership


def test_concurrent_onboarding_reserves_slug_before_creating_external_access(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'onboarding.db'}", connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    created = []

    def isolated_db():
        with sessions() as db:
            yield db

    async def create_owner(*_args):
        created.append("isolated-owner")
        await asyncio.sleep(0.1)
        return "isolated-owner"

    monkeypatch.setattr("app.api.create_supabase_owner_account", create_owner)
    app.dependency_overrides[get_db] = isolated_db
    payload = {"business": {"name": "Concurrent", "slug": "concurrent"},
               "branch": {"name": "Main", "slug": "main"}, "owner_name": "Owner",
               "owner_email": "owner@example.test", "owner_password": "Test-only-Password42!"}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async def post():
                return await client.post("/api/v1/admin/onboarding/restaurants", json=payload,
                                         headers={"X-Dev-Auth": "test-token", "X-Dev-Role": "superadmin"})
            return await asyncio.wait_for(asyncio.gather(post(), post()), 5)

    try:
        results = asyncio.run(run())
        assert sorted(response.status_code for response in results) == [201, 409]
        assert created == ["isolated-owner"]
        with sessions() as db:
            assert db.query(Business).count() == 1
            assert db.query(Membership).count() == 1
            assert db.query(IntegrationCredential).count() == 1
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()
