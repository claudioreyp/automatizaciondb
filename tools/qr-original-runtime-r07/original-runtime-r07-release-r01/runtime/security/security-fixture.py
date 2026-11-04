"""Proposed API70 security fixture for the existing serialized case lifecycle.

The authenticated control hook archives the prior case and invokes the existing
private memory seed under its shared lock. This installer only validates the
new fixed case and reads its real catalog; no process/container restart is needed.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
KEYS=frozenset({'M:SEC-COMPLAINT','M:SEC-01','M:SEC-02','M:SEC-03','M:SEC-04','M:SEC-06','M:SEC-07','M:SEC-08','M:SEC-09','M:SEC-11','M:SEC-13','M:SEC-19'})

def compact(v):
    return json.dumps(v,ensure_ascii=False,separators=(',',':'))

def digest(v):
    return hashlib.sha256(compact(v).encode('utf8')).hexdigest()

def contracts():
    m=json.loads((HERE/'manifest.json').read_text(encoding='utf8'))
    assert set(m['selected_keys'])==KEYS and m['execution']=='SOURCE_ONLY_NOT_EXECUTED'
    return {r['canonical_key']:r for r in m['cases']}

def memory_only(engine):
    from sqlalchemy.pool import StaticPool
    u=engine.url
    assert u.drivername=='sqlite+pysqlite' and u.database==':memory:'
    assert u.host is None and u.username is None and u.password is None and not u.query
    assert isinstance(engine.pool,StaticPool)
    with engine.connect() as c:
        assert all(r[1] in ('main','temp') and r[2]=='' for r in c.exec_driver_sql('PRAGMA database_list').all())

def install_security_fixture(sandbox,case_key,fixture_contract_sha256,sender):
    assert case_key in KEYS
    row=contracts()[case_key];contract=row['fixture_contract']
    assert digest(contract)==fixture_contract_sha256==row['fixture_contract_sha256']
    assert sender==contract['fresh_sender'],'EXACT_NEW_SYNTHETIC_SENDER_REQUIRED'
    memory_only(sandbox['engine'])
    assert sandbox['state']['case_id']==case_key,'EXACT_NEW_CASE_SEED_REQUIRED'
    from sqlalchemy import func,select
    from app.database import SessionLocal
    from app.main import app
    assert SessionLocal.kw.get('bind') is sandbox['engine'] and sandbox['client'].app is app,'EXACT_PRIVATE_API70_APP_ENGINE_REQUIRED'
    from app.models import (Order,Payment,KitchenTicket,PrintJob,OrderPaymentRequest,PaymentEvidence,
        DeliveryAssignment,AgentConversation,AgentMessageDelivery,AgentModelUsage,AgentAttentionRequest)
    models=[Order,Payment,KitchenTicket,PrintJob,OrderPaymentRequest,PaymentEvidence,DeliveryAssignment,
        AgentConversation,AgentMessageDelivery,AgentModelUsage,AgentAttentionRequest]
    with SessionLocal() as db:
        counts={m.__name__:db.scalar(select(func.count()).select_from(m)) for m in models}
    assert all(v==0 for v in counts.values()),'RESET_DID_NOT_PRODUCE_EMPTY_CASE'
    auth={'Authorization':'Bearer '+sandbox['tokens'][2]}
    foreign_auth={'Authorization':'Bearer '+sandbox['tokens'][3]}
    def get_catalog(headers):
        r=sandbox['client'].get('/api/v1/integrations/context/catalog',headers=headers)
        assert r.status_code==200,'ACTUAL_API70_CATALOG_REQUIRED'
        return r.json()
    foreign_before=get_catalog(foreign_auth)
    catalog=get_catalog(auth)
    foreign_after=get_catalog(foreign_auth)
    def stable(v):
        c=json.loads(compact(v))
        for field in ('checked_at','local_time'):
            c.get('service_availability',{}).pop(field,None)
        return c
    assert stable(foreign_before)==stable(foreign_after),'FOREIGN_CATALOG_CHANGED'
    return {'schema':'escalarai.original-expansion-fixture-proof.v1','synthetic_only':True,
        'database':'sqlite_memory','case_id':case_key,'case_key':case_key,'sender':sender,
        'fixture_contract_sha256':fixture_contract_sha256,'original_scope':contract['original_scope'],
        'execution_scope':contract['execution_scope'],'observed_catalog':catalog,
        'observed_catalog_sha256':digest(catalog),'foreign_catalog_unchanged':True,
        'foreign_comparison_omitted_observation_fields':['service_availability.checked_at','service_availability.local_time'],
        'commercial_counts':counts,'catalog_changed':False,'database_reset':True,
        'reset_count':sandbox['state']['reset_count'],
        'real_whatsapp':False,'physical_printing':False,
        'installer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
