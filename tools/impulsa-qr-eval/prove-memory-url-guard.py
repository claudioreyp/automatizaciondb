"""Prove the strict memory guard against the installed SQLAlchemy version.

No API import, server, network, credentials or SQLite file is used. ROOT can
run this in the existing SQLAlchemy2.1.8 image, mounting the corrected source
as /patched-api-sandbox.py and setting ESCALAR_QR_MEMORY_GUARD_SOURCE to it.
"""
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

source=Path(os.environ.get("ESCALAR_QR_MEMORY_GUARD_SOURCE","/sandbox/api-sandbox.py"))
tree=ast.parse(source.read_text(encoding="utf-8-sig"))
guard_nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef)
    and node.name in ("configure_private_memory_temp_store","assert_private_memory_engine")]
assert len(guard_nodes)==2
namespace={}
exec(compile(ast.Module(body=guard_nodes,type_ignores=[]),str(source),"exec"),namespace)
guard=namespace["assert_private_memory_engine"]
positive=create_engine("sqlite+pysqlite:///:memory:",poolclass=StaticPool)
namespace["configure_private_memory_temp_store"](positive)
connections=[]
event.listen(positive,"connect",lambda *args:connections.append("memory"))
try:
    proof=guard(positive)
    with positive.begin() as connection:
        connection.exec_driver_sql("CREATE TEMP TABLE synthetic_guard_probe (value INTEGER)")
        connection.exec_driver_sql("INSERT INTO synthetic_guard_probe VALUES (7)")
    temp_proof=guard(positive)
    assert temp_proof["internal_temp_present"] and temp_proof["temp_store_memory"]
    with positive.connect() as connection:
        assert connection.exec_driver_sql("SELECT value FROM synthetic_guard_probe").scalar_one()==7
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS forbidden_attached_memory")
    try:guard(positive)
    except AssertionError:attached_rejected=True
    else:raise AssertionError("ATTACHED_DATABASE_ACCEPTED")
    with positive.connect() as connection:
        connection.exec_driver_sql("DETACH DATABASE forbidden_attached_memory")
    report={"state":"STRICT_MEMORY_GUARD_PASS","sqlalchemy_version":importlib.metadata.version("sqlalchemy"),
        "fresh_known_memory_url_canonical":str(positive.url),
        "old_string_guard_would_accept":str(positive.url)=="sqlite+pysqlite:///:memory:",
        "actual_database_field_is_memory":positive.url.database==":memory:",
        "positive_proof":proof,"internal_temp_proof":temp_proof,"temporary_data_preserved":True,
        "attached_memory_rejected":attached_rejected,"memory_connections":len(connections),"rejected":[]}
finally:positive.dispose()

for name,url,pool in [
    ("file","sqlite+pysqlite:///./FORBIDDEN-DO-NOT-CREATE.db",StaticPool),
    ("unset_database","sqlite+pysqlite://",StaticPool),
    ("query_parameter","sqlite+pysqlite:///:memory:?cache=shared",StaticPool),
    ("implicit_driver","sqlite:///:memory:",StaticPool),
    ("non_static_pool","sqlite+pysqlite:///:memory:",None),
]:
    engine=create_engine(url,**({"poolclass":pool} if pool is not None else {}))
    calls=[]
    def forbid_connection(*args):
        calls.append(1)
        raise RuntimeError("INVALID_ENGINE_MUST_NOT_CONNECT")
    event.listen(engine,"connect",forbid_connection)
    try:
        try:guard(engine)
        except AssertionError:pass
        else:raise AssertionError("INVALID_ENGINE_ACCEPTED")
        assert not calls
        report["rejected"].append({"case":name,"rejected_before_connection":True,"connections":0})
    finally:engine.dispose()

assert not Path("FORBIDDEN-DO-NOT-CREATE.db").exists()
report.update(guard_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),network=0,models=0,
    listeners=0,tokens_created=0,real_database_connections=0,file_databases_created=0)
print(json.dumps(report),flush=True)
