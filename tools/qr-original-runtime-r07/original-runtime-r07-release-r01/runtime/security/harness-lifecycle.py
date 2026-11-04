"""Narrow LOCAL harness ordering before the existing fixed control request.

The caller supplies its existing archive writer and fixed authenticated request
function. No credentials, n8n API access, server files or approvals are introduced.
Backend never imports this module. Failed archive means no control request.
"""
import re
SHA=re.compile(r'[a-f0-9]{64}\Z')

def archive_then_start_security(previous_state,body,*,archive_previous_case,start_fixed_case):
    assert isinstance(body,dict) and set(body)=={'case_key','fixture_contract_sha256'}
    assert isinstance(previous_state,dict) and set(previous_state)=={'case_id','reset_count'}
    receipt=None
    if previous_state['case_id']!='unstarted':
        receipt=archive_previous_case(dict(previous_state))
        assert isinstance(receipt,dict)
        assert set(receipt)=={'case_id','reset_count','closed','durable','journal_complete',
            'models_inflight','snapshot_sha256','journal_sha256','result_sha256'}
        assert receipt['case_id']==previous_state['case_id'] and receipt['reset_count']==previous_state['reset_count']
        assert receipt['closed'] is True and receipt['durable'] is True and receipt['journal_complete'] is True
        assert receipt['models_inflight'] is False
        assert all(isinstance(receipt[k],str) and SHA.fullmatch(receipt[k]) for k in ('snapshot_sha256','journal_sha256','result_sha256'))
    # start_fixed_case is the harness's existing private start-original-case
    # request. Neither an arbitrary route nor a method can come from body.
    status,proof=start_fixed_case(body)
    return status,proof,receipt
