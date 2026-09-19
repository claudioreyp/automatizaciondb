"""Disposable, loopback-only API for the real two-browser access tests."""
import os
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

with tempfile.TemporaryDirectory(prefix="pos-agent-e2e-") as directory:
    os.environ.update({
        "ENVIRONMENT": "test", "DATABASE_URL": f"sqlite+pysqlite:///{Path(directory).as_posix()}/isolated.db",
        "UPLOAD_DIR": str(Path(directory) / "uploads"), "AUTO_CREATE_SCHEMA": "false",
        "DEV_AUTH_TOKEN": "isolated-agent-e2e-only", "DEVICE_AUTH_SECRET": "isolated-agent-e2e-secret-never-use-in-deployment",
        "CORS_ORIGINS": "http://127.0.0.1:5176", "POS_PUBLIC_BASE_URL": "http://127.0.0.1:5176",
        "SUPABASE_URL": "", "SUPABASE_SERVICE_ROLE_KEY": "", "OPENAI_API_KEY": "", "INTEGRATION_SERVICE_TOKEN": "",
    })
    from alembic import command
    from alembic.config import Config
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "head")
    from app.database import SessionLocal, engine
    from app.models import Branch, Business, ModuleEntitlement, StaffMember, StaffMemberBranch, StaffMemberRole
    from app.settings_service import hash_pin
    with SessionLocal.begin() as db:
        business = Business(slug="agent-isolated", name="Restaurante de prueba")
        db.add(business); db.flush()
        branch = Branch(business_id=business.id, slug="main", name="Sucursal de prueba")
        db.add(branch); db.flush()
        for module in ["pos", "orders", "cash", "kitchen", "tables", "delivery", "menu"]:
            db.add(ModuleEntitlement(business_id=business.id, module=module, enabled=True))
        for role, name in [("owner", "Responsable"), ("cashier", "Cajera")]:
            member = StaffMember(business_id=business.id, first_name=name, last_name="Prueba", pin_hash=hash_pin("8062"))
            db.add(member); db.flush()
            db.add_all([StaffMemberRole(business_id=business.id, staff_member_id=member.id, role=role),
                        StaffMemberBranch(business_id=business.id, staff_member_id=member.id, branch_id=branch.id)])
    import uvicorn
    from app.main import app
    try:
        uvicorn.run(app, host="127.0.0.1", port=8009, log_level="warning")
    finally:
        engine.dispose()
