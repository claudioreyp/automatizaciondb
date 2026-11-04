"""Compare the original and candidate catalogs without remote dependencies.

Every worker uses a new in-memory SQLite database. Both sources receive identical
synthetic products, variants, required modifiers and stock recipes. Results are
local request timings, not production latency or a database capacity claim.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tarfile
import time

BASE_REVISION = "e8fd497c40ee1ed9d14379841acd6a578d367293"


def isolated_environment():
    env = {key: value for key, value in os.environ.items() if not any(
        part in key.upper() for part in ("DATABASE", "SUPABASE", "OPENAI", "ANTHROPIC", "QZ_TRAY", "VITE_"))}
    env.update(ENVIRONMENT="test", DATABASE_URL="sqlite+pysqlite:///:memory:",
        AUTO_CREATE_SCHEMA="true", ESCALAR_ENV_FILE_DISABLED="true",
        DEV_AUTH_TOKEN="synthetic-benchmark-only", INTEGRATION_SERVICE_TOKEN="", PYTHONUTF8="1")
    return env


def source_digest(folder):
    digest = hashlib.sha256()
    for file in sorted((folder / "app").rglob("*.py")):
        digest.update(file.relative_to(folder).as_posix().encode("utf-8"))
        digest.update(file.read_bytes())
    return digest.hexdigest()


def worker(source, output):
    # The controller sets these before Python imports either version of app.
    assert os.environ["DATABASE_URL"] == "sqlite+pysqlite:///:memory:"
    assert os.environ["ESCALAR_ENV_FILE_DISABLED"] == "true"
    sys.path.insert(0, str(source))
    from datetime import time as clock_time
    from fastapi.testclient import TestClient
    from sqlalchemy import event
    from app.auth import hash_integration_token
    from app.database import Base, SessionLocal, engine
    from app.main import app
    from app.models import (Business, Branch, Category, IntegrationCredential,
        InventoryItem, Modifier, ModifierGroup, Product, ProductModifierGroup,
        ProductVariant, RecipeItem, ScheduleShift, ServiceSchedule)

    assert engine.url.database == ":memory:"
    measurements = []
    for size in (50, 500):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with SessionLocal.begin() as db:
            business = Business(slug="catalog-benchmark", name="Restaurante sintético")
            db.add(business); db.flush()
            branch = Branch(business_id=business.id, slug="main", name="Sucursal sintética",
                accepted_payment_methods=["cash", "yape"])
            db.add(branch); db.flush()
            category = Category(business_id=business.id, branch_id=branch.id, name="Pizzas")
            inventory = InventoryItem(business_id=business.id, branch_id=branch.id,
                sku="BASE", name="Insumo sintético", unit="kg", quantity=100000, minimum_stock=1)
            group = ModifierGroup(business_id=business.id, branch_id=branch.id,
                name="Sabores", minimum=2, maximum=3, required=True)
            schedule = ServiceSchedule(business_id=business.id, branch_id=branch.id,
                name="Horario sintético", kind="primary", active=True)
            db.add_all([category, inventory, group, schedule]); db.flush()
            db.add_all([ScheduleShift(schedule_id=schedule.id, day_of_week=day,
                starts_at=clock_time(0), ends_at=clock_time(0)) for day in range(7)])
            db.add_all([Modifier(group_id=group.id, name=name, price_delta=0)
                for name in ("Pepperoni", "Hawaiana", "Vegetariana")])
            products = [Product(business_id=business.id, branch_id=branch.id,
                category_id=category.id, sku=f"SYNTH-{index}", name=f"Pizza {index:04}",
                price=20, track_stock=True) for index in range(size)]
            db.add_all(products); db.flush()
            for product in products:
                db.add_all([ProductVariant(product_id=product.id, name="Mediana", price_delta=0),
                    ProductVariant(product_id=product.id, name="Familiar", price_delta=8),
                    ProductModifierGroup(product_id=product.id, group_id=group.id),
                    RecipeItem(product_id=product.id, inventory_item_id=inventory.id, quantity="0.5")])
            db.add(IntegrationCredential(business_id=business.id, branch_id=branch.id,
                name="Synthetic benchmark", profile="restaurant_agent", token_prefix="isolated-benchmark",
                token_hash=hash_integration_token("isolated-benchmark.token"), scopes=["menu:read"]))
        samples = []
        with TestClient(app) as client:
            # Warm module/schema caches once; every measured request gets its own
            # request-scoped DB session and still performs commercial reads.
            warm = client.get("/api/v1/integrations/context/catalog",
                headers={"Authorization": "Bearer isolated-benchmark.token"})
            assert warm.status_code == 200, warm.text
            for _ in range(5):
                queries = []
                def count(conn, cursor, sql, parameters, context, many):
                    if sql.lstrip().upper().startswith("SELECT"):
                        queries.append(sql)
                event.listen(engine, "before_cursor_execute", count)
                started = time.perf_counter()
                try:
                    response = client.get("/api/v1/integrations/context/catalog",
                        headers={"Authorization": "Bearer isolated-benchmark.token"})
                finally:
                    elapsed = (time.perf_counter() - started) * 1000
                    event.remove(engine, "before_cursor_execute", count)
                assert response.status_code == 200, response.text
                assert len(response.json()["products"]) == size
                samples.append({"selects": len(queries), "bytes": len(response.content),
                    "elapsed_ms": round(elapsed, 3)})
        assert len({sample["selects"] for sample in samples}) == 1
        assert len({sample["bytes"] for sample in samples}) == 1
        measurements.append({"products": size, "selects": samples[0]["selects"],
            "bytes": samples[0]["bytes"], "median_elapsed_ms": round(statistics.median(
                sample["elapsed_ms"] for sample in samples), 3), "samples": samples})
    output.write_text(json.dumps({"source_sha256": source_digest(source),
        "measurements": measurements}, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-source", type=Path)
    parser.add_argument("--worker-output", type=Path)
    args = parser.parse_args()
    if args.worker_source:
        worker(args.worker_source.resolve(strict=True), args.worker_output)
        return 0
    root = Path(__file__).resolve().parents[1]
    artifact = root / ".test-results" / ("catalog-comparison-" + str(time.time_ns()))
    artifact.mkdir(parents=True, exist_ok=False)
    base = artifact / "base"
    candidate = artifact / "candidate"
    base.mkdir(); candidate.mkdir()
    archived = subprocess.run(["git", "archive", BASE_REVISION, "app"], cwd=root,
        check=True, capture_output=True, timeout=20).stdout
    with tarfile.open(fileobj=io.BytesIO(archived), mode="r:") as tar:
        for member in tar.getmembers():
            target = (base / member.name).resolve()
            if not target.is_relative_to(base.resolve()) or member.issym() or member.islnk():
                raise ValueError("Unexpected archive path")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
    shutil.copytree(root / "app", candidate / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for label, source in (("base", base), ("candidate", candidate)):
        with (artifact / (label + ".log")).open("wb") as log:
            result = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                "--worker-source", str(source), "--worker-output", str(artifact / (label + ".json"))],
                cwd=source, env=isolated_environment(), stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, timeout=90)
        if result.returncode:
            print(json.dumps({"failed_worker": label, "artifacts": str(artifact)}))
            return result.returncode
    before = json.loads((artifact / "base.json").read_text(encoding="utf-8"))
    after = json.loads((artifact / "candidate.json").read_text(encoding="utf-8"))
    comparisons = []
    for previous, current in zip(before["measurements"], after["measurements"]):
        comparisons.append({"products": current["products"], "base": previous, "candidate": current,
            "select_reduction_percent": round(100 * (1 - current["selects"] / previous["selects"]), 2),
            "time_reduction_percent": round(100 * (1 - current["median_elapsed_ms"] / previous["median_elapsed_ms"]), 2)})
    report = {"scope": "synthetic_loopback_free_in_memory_sqlite", "base_revision": BASE_REVISION,
        "base_source_sha256": before["source_sha256"], "candidate_source_sha256": after["source_sha256"],
        "method": "Same seed; one warm-up, five requests per size/version; no external I/O.",
        "comparisons": comparisons}
    (artifact / "comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"artifacts": str(artifact), "comparisons": comparisons}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
