"""Fixed authenticated controls for one private API70 memory sandbox.

No database selection, n8n API credential, archive callback, arbitrary command or
caller-selected route exists. Existing serial HTTPServer invokes this router.
Archive of the completed previous case belongs to the harness before start-case.
"""
import importlib.util
import json
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs,urlsplit

HERE=Path(__file__).resolve().parent
SENDER=re.compile(r'519990[0-9]{5,9}\Z')

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,HERE/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def create_control_dispatch(sandbox,notice_fixture):
    original=load('r07_original_three','originals/control-hook.py')
    security=load('r07_original_security_twelve','security/control-hook.py')
    visual=load('r07_fixed_visual_snapshot','visual_snapshot.py')
    shared_lock=sandbox['control_lock']
    original_rows=original.fixture.contracts();security_rows=security.fixture.contracts()
    assert len(original_rows)==3 and len(security_rows)==12 and not (set(original_rows)&set(security_rows))
    def dispatch(method,target,raw,control_header):
        if not secrets.compare_digest(control_header,sandbox['control']):
            return 401,{'code':'CONTROL_AUTH'}
        parsed=urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return 404,{'code':'CONTROL_ROUTE'}
        path=parsed.path
        if path=='/control/start-original-case' and method=='POST' and not parsed.query:
            body=json.loads(raw)
            assert isinstance(body,dict) and set(body)=={'case_key','fixture_contract_sha256'}
            key=body['case_key']
            if key in security_rows:
                assert body['fixture_contract_sha256']==security_rows[key]['fixture_contract_sha256']
                return security.dispatch_original_security_control(sandbox,body,control_lock=shared_lock)
            assert key in original_rows and body['fixture_contract_sha256']==original_rows[key]['fixture_contract_sha256']
            with shared_lock:
                return original.dispatch_original_control(sandbox,body)
        if path=='/control/visual-snapshot' and method=='POST' and not parsed.query:
            body=json.loads(raw)
            assert isinstance(body,dict) and set(body)=={'case_id','sender'}
            # The visual helper validates exact fixed case, synthetic scope and
            # sender ownership, and acquires the SAME lock internally.
            return 200,visual.dispatch_visual_snapshot(sandbox,body,control_lock=shared_lock)
        if path=='/control/start-case' and method=='POST' and not parsed.query:
            body=json.loads(raw)
            assert isinstance(body,dict) and set(body)=={'case_id'}
            assert isinstance(body['case_id'],str) and re.fullmatch(r'SMOKE[0-9]{2}-[A-Z0-9-]{1,80}',body['case_id'])
            with shared_lock:
                return 200,sandbox['seed'](body['case_id'])
        if path=='/control/snapshot' and method=='GET':
            query=parse_qs(parsed.query)
            assert set(query)=={'business_id','sender'} and all(len(v)==1 for v in query.values())
            business=int(query['business_id'][0]);sender=query['sender'][0]
            assert business in (2,3) and SENDER.fullmatch(sender)
            with shared_lock:
                return 200,sandbox['snapshot'](business,sender)
        if path=='/control/notice-ready' and method=='POST' and not parsed.query:
            with shared_lock:
                return 200,notice_fixture['ready'](json.loads(raw))
        if path=='/control/notice-snapshot' and method=='GET':
            query=parse_qs(parsed.query)
            assert set(query)=={'sender'} and len(query['sender'])==1
            with shared_lock:
                return 200,notice_fixture['snapshot'](query['sender'][0])
        if path=='/control/stop' and method=='POST' and not parsed.query:
            return 200,{'state':'STOPPING'}
        return 404,{'code':'CONTROL_ROUTE'}
    return dispatch
