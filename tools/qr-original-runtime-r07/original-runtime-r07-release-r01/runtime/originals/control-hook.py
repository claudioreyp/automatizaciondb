"""Fixed extension hook for the EXISTING authenticated synthetic controller.

Does not listen, authenticate, pick URLs or call models. The current handler
must authenticate X-Evaluation-Control before invoking this function.
"""
import importlib.util
from pathlib import Path

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("original_fixture_hook",HERE/"original-fixture.py")
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

def dispatch_original_control(sandbox,body):
    assert isinstance(body,dict) and set(body)=={"case_key","fixture_contract_sha256"}
    assert body["case_key"] in fixture.KEYS
    row=fixture.contracts()[body["case_key"]]
    assert body["fixture_contract_sha256"]==row["fixture_contract_sha256"]
    fixture.memory_only(sandbox["engine"])
    # Same sandbox-only reset semantics as its existing start-case endpoint.
    # Required after recording the preceding case's snapshot, before the next.
    sandbox["seed"]("original-fresh-preparation")
    proof=fixture.install_original_fixture(sandbox,body["case_key"],body["fixture_contract_sha256"])
    return 200,proof

if __name__=="__main__":
    print('{"state":"INERT_NOT_STARTED","network":0,"models":0,"listener":false}')
