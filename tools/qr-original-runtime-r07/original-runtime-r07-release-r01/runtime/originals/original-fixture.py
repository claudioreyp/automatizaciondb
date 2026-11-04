"""Three allowlisted original fixtures; no listener and no model code.

install_original_fixture is an integration hook for the existing private memory
sandbox. It never chooses or resets a database. The caller's seed first proves
the existing engine is its own memory engine. Commercial rows block changes.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
KEYS=frozenset({"I:SUP-01-GENERIC-PIZZA","I:SUP-02-MINIMUM-ONE-FLAVOR","M:SEC-05"})

def compact(value):
    return json.dumps(value,ensure_ascii=False,separators=(",",":"))

def digest(value):
    return hashlib.sha256(compact(value).encode("utf8")).hexdigest()

def memory_only(engine):
    from sqlalchemy.pool import StaticPool
    u=engine.url
    assert u.drivername=="sqlite+pysqlite" and u.database==":memory:"
    assert u.host is None and u.username is None and u.password is None and not u.query
    assert isinstance(engine.pool,StaticPool)
    with engine.connect() as c:
        rows=c.exec_driver_sql("PRAGMA database_list").all()
        assert all(r[1] in ("main","temp") and r[2]=="" for r in rows)
    return True

def contracts():
    m=json.loads((HERE/"manifest.json").read_text(encoding="utf8"))
    assert set(m["selected_keys"])==KEYS
    return {c["canonical_key"]:c for c in m["cases"]}

def install_original_fixture(sandbox,case_key,fixture_contract_sha256):
    assert case_key in KEYS
    row=contracts()[case_key];contract=row["fixture_contract"]
    assert digest(contract)==fixture_contract_sha256==row["fixture_contract_sha256"]
    memory_only(sandbox["engine"])
    from sqlalchemy import delete,func,select
    from app.database import SessionLocal
    from app.models import (Category,Product,ProductVariant,Modifier,ModifierGroup,ProductModifierGroup,
        Order,Payment,KitchenTicket,PrintJob,OrderPaymentRequest,PaymentEvidence,DeliveryAssignment,AgentConversation)
    commercial=[Order,Payment,KitchenTicket,PrintJob,OrderPaymentRequest,PaymentEvidence,DeliveryAssignment]
    with SessionLocal() as db:
        assert all(db.scalar(select(func.count()).select_from(model))==0 for model in commercial)
        assert db.scalar(select(func.count()).select_from(AgentConversation))==0
    # Use the sandbox's own established reset contract; it independently proves
    # structural memory and cannot select a production engine.
    reset=sandbox["seed"](case_key)
    auth={"Authorization":"Bearer "+sandbox["tokens"][2]}
    other_auth={"Authorization":"Bearer "+sandbox["tokens"][3]}
    def api_catalog(headers):
        r=sandbox["client"].get("/api/v1/integrations/context/catalog",headers=headers)
        assert r.status_code==200,("catalog",r.status_code)
        return r.json()
    foreign_before=api_catalog(other_auth)
    if contract["product"] is not None:
        with SessionLocal.begin() as db:
            product_ids=list(db.scalars(select(Product.id).where(Product.business_id==2,Product.branch_id==2)))
            group_ids=list(db.scalars(select(ModifierGroup.id).where(ModifierGroup.business_id==2,ModifierGroup.branch_id==2)))
            db.execute(delete(ProductModifierGroup).where(ProductModifierGroup.product_id.in_(product_ids)))
            db.execute(delete(ProductVariant).where(ProductVariant.product_id.in_(product_ids)))
            db.execute(delete(Modifier).where(Modifier.group_id.in_(group_ids)))
            db.execute(delete(ModifierGroup).where(ModifierGroup.id.in_(group_ids)))
            db.execute(delete(Product).where(Product.id.in_(product_ids)))
            category=db.scalar(select(Category).where(Category.business_id==2,Category.branch_id==2))
            p=contract["product"]
            db.add(Product(id=p["id"],business_id=2,branch_id=2,category_id=category.id,sku="ORIGINAL-PIZZA-1",
                name=p["name"],price=p["price"],service_channels=["pos","digital_delivery","digital_takeaway"]))
            db.flush()
            db.add_all([ProductVariant(product_id=p["id"],**v) for v in p["variants"]])
            g=p["required_group"]
            group=ModifierGroup(business_id=2,branch_id=2,name=g["name"],required=True,
                minimum=g["minimum"],maximum=g["maximum"],allow_repeats=g["allow_repeats"])
            db.add(group);db.flush()
            db.add(ProductModifierGroup(product_id=p["id"],group_id=group.id))
            db.add_all([Modifier(group_id=group.id,name=n,price_delta=0,available=True) for n in g["options"]])
    catalog=api_catalog(auth)
    foreign_after=api_catalog(other_auth)
    # Two live reads evaluate their own wall time. Compare every commercial
    # field and revision; omit only the two documented observation timestamps.
    def without_read_clock(value):
        result=json.loads(compact(value))
        for field in ("checked_at","local_time"):
            result.get("service_availability",{}).pop(field,None)
        return result
    assert without_read_clock(foreign_before)==without_read_clock(foreign_after)
    counts={}
    with SessionLocal() as db:
        counts={model.__name__:db.scalar(select(func.count()).select_from(model)) for model in commercial}
    assert set(counts.values())=={0}
    return {**reset,"schema":"escalarai.original-fixture-proof.v1","case_key":case_key,
        "fixture_contract_sha256":fixture_contract_sha256,"original_scope":contract["original_scope"],
        "execution_scope":contract["execution_scope"],"observed_catalog":catalog,
        "observed_catalog_sha256":digest(catalog),"foreign_catalog_unchanged":True,
        "foreign_comparison_omitted_observation_fields":["service_availability.checked_at","service_availability.local_time"],
        "commercial_counts":counts,
        "installer_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

if __name__=="__main__":
    print(json.dumps({"state":"INERT_NOT_STARTED","network":0,"models":0,"listener":False}))
