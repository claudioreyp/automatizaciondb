"""Fixed backend case dispatch in the existing authenticated control lifecycle.

The harness archives the completed prior result/snapshot/journal BEFORE calling
this route, as it does before other synthetic start-case resets. Backend has no
archive callback, n8n API key, approval file or access to financial journals.
"""
import importlib.util
from pathlib import Path

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('original_security_fixture_r04',HERE/'security-fixture.py')
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

def dispatch_original_security_control(sandbox,body,*,control_lock):
    assert isinstance(body,dict) and set(body)=={'case_key','fixture_contract_sha256'}
    assert body['case_key'] in fixture.KEYS
    row=fixture.contracts()[body['case_key']]
    assert body['fixture_contract_sha256']==row['fixture_contract_sha256']
    if not control_lock.acquire(timeout=2):
        raise RuntimeError('ORIGINAL_CONTROL_BUSY')
    try:
        fixture.memory_only(sandbox['engine'])
        prior=dict(sandbox['state'])
        token_values=dict(sandbox['tokens']);control_value=sandbox['control']
        reset=sandbox['seed'](body['case_key'])
        assert sandbox['tokens']==token_values and sandbox['control']==control_value
        assert sandbox['state']=={'case_id':body['case_key'],'reset_count':prior['reset_count']+1}
        assert reset['synthetic_only'] is True and reset['database']=='sqlite_memory'
        proof=fixture.install_security_fixture(sandbox,body['case_key'],body['fixture_contract_sha256'],row['fixture_contract']['fresh_sender'])
        proof['prior_case_id']=prior['case_id']
        proof['control_lock_scope']='existing_control_lifecycle'
        return 200,proof
    finally:
        control_lock.release()
