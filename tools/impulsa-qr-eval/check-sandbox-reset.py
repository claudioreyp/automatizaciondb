"""Two real API in-memory resets; no HTTP listener or model/consumer call.

ROOT may run inside the existing image with network none and the corrected
sandbox mounted at the ESCALAR_QR_RESET_SANDBOX_SOURCE path.
"""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path

source=Path(os.environ.get("ESCALAR_QR_RESET_SANDBOX_SOURCE",str(Path(__file__).resolve().parent/"api-sandbox.py")))
spec=importlib.util.spec_from_file_location("strict_reset_sandbox",source)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
sandbox=module.create_sandbox()
identities=(dict(sandbox["tokens"]),sandbox["control"])
rows=[]
try:
    with sandbox["client"] as client:
        assert client.get("/api/v1/health").status_code==200
        for case_id in ("SMOKE00-RESET-ONE","SMOKE00-RESET-TWO"):
            result=sandbox["seed"](case_id)
            proof=module.assert_private_memory_engine(sandbox["engine"])
            assert proof["temp_store_memory"] and proof["internal_temp_present"]
            assert result["case_id"]==case_id and result["synthetic_only"] and result["database"]=="sqlite_memory"
            assert identities==(sandbox["tokens"],sandbox["control"])
            catalogs=[]
            for business in (2,3):
                response=client.get("/api/v1/integrations/context/catalog",headers={"Authorization":"Bearer "+sandbox["tokens"][business]})
                assert response.status_code==200
                snapshot=sandbox["snapshot"](business,"519990000000")
                assert all(len(snapshot[name])==0 for name in ("orders","payments","tickets","print_jobs","conversations","model_usage"))
                catalogs.append({"business_id":business,"catalog_http":response.status_code,"products":response.json()["product_count"],"commerce_count":0})
            rows.append({"case_id":case_id,"reset_count":result["reset_count"],"proof":proof,"catalogs":catalogs})
    report={"state":"REAL_API_TWO_RESETS_MEMORY_TEMP_PASS","sqlalchemy_version":importlib.metadata.version("sqlalchemy"),
        "sandbox_source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"resets":rows,
        "token_identities_preserved_within_process":True,"health_http":200,"models":0,"listeners":0,"network":0,
        "consumer_messages":0,"physical_printing":0}
    print(json.dumps(report),flush=True)
finally:sandbox["engine"].dispose()
