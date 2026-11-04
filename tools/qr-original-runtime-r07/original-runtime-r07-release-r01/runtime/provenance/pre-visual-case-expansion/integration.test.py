"""LOCAL ONLY real API70/control/proxy/controller assembly checks.

No listener, remote request, native original case, model or physical printing.
One fresh private-memory engine, fifteen fixture contracts, one visual fixture.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import time

sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
OUT=HERE/'local-run-r01'
def sha(v):return hashlib.sha256(v).hexdigest()
def load(name,path):
    s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def write(name,value):
    f=OUT/name;f.parent.mkdir(parents=True,exist_ok=True)
    raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8')
    with f.open('xb') as s:s.write(raw);s.flush();os.fsync(s.fileno())
    assert f.read_bytes()==raw
    return sha(raw)

def main():
    assert __debug__ and not OUT.exists();OUT.mkdir();started=time.monotonic();sandbox=None;blocked=[]
    def deny(*a,**k):blocked.append(True);raise AssertionError('R07_LOCAL_NETWORK_FORBIDDEN')
    socket.create_connection=deny
    import urllib.request
    urllib.request.urlopen=deny
    import httpx
    httpx.HTTPTransport.handle_request=deny
    try:
        proof=json.loads((HERE/'provenance/api70-commit-check.json').read_bytes())
        assert len(proof['files'])==70
        for ref in proof['files']:assert sha((HERE/ref['file']).read_bytes())==ref['sha256']
        os.environ['ESCALAR_QR_SANDBOX_API_ROOT']=str(HERE/'api')
        os.environ['UPLOAD_DIR']=str(OUT/'uploads')
        module=load('r07_integrated_private_sandbox',HERE/'sandbox.py')
        sandbox=module.create_sandbox()
        notifier=load('r07_same_notifier',HERE/'notifier_fixture.py')
        notice=notifier.attach_notifier_fixture(sandbox,module.assert_private_memory_engine)
        router=load('r07_actual_control_router',HERE/'control_router.py')
        dispatch=router.create_control_dispatch(sandbox,notice)
        lock=sandbox['control_lock'];engine=sandbox['engine'];tokens=dict(sandbox['tokens']);private=sandbox['control']
        notifier_token=notice['token']
        from sqlalchemy import select
        from app.database import SessionLocal
        from app.models import IntegrationCredential,AgentAttentionRequest,Order,OrderItem
        def credentials():
            with SessionLocal() as db:return [(r.id,r.token_hash,tuple(r.scopes),r.profile,r.business_id,r.branch_id)
                for r in db.scalars(select(IntegrationCredential).order_by(IntegrationCredential.id))]
        initial_credentials=credentials();assert len(initial_credentials)==3
        rows=[]
        for group in ['originals','security']:
            rows+=json.loads((HERE/group/'manifest.json').read_bytes())['cases']
        assert len(rows)==15 and len({r['canonical_key']for r in rows})==15
        before=dict(sandbox['state'])
        assert dispatch('POST','/control/start-original-case',b'not-json','wrong')==(401,{'code':'CONTROL_AUTH'})
        assert sandbox['state']==before
        malformed=[{'case_key':'NOT-ALLOWLISTED','fixture_contract_sha256':'0'*64},
            {'case_key':rows[0]['canonical_key'],'fixture_contract_sha256':'0'*64},
            {'case_key':rows[0]['canonical_key'],'fixture_contract_sha256':rows[0]['fixture_contract_sha256'],'url':'https://example.invalid'}]
        for body in malformed:
            try:dispatch('POST','/control/start-original-case',json.dumps(body).encode(),private)
            except AssertionError:pass
            else:raise AssertionError('INVALID_FIXED_CONTRACT_ACCEPTED')
            assert sandbox['state']==before
        results=[];last=None;archives=0
        for index,row in enumerate(rows):
            if last is not None:
                sender=last.get('fixture_contract',{}).get('fresh_sender','519990170001')
                status,previous=dispatch('GET','/control/snapshot?business_id=2&sender='+sender,b'',private)
                assert status==200 and previous['case_id']==last['canonical_key'] and len(previous['attention'])==1
                write(f'archives/{index:02d}.json',previous);archives+=1
            status,p=dispatch('POST','/control/start-original-case',json.dumps({'case_key':row['canonical_key'],
                'fixture_contract_sha256':row['fixture_contract_sha256']}).encode(),private)
            assert status==200 and p['case_key']==row['canonical_key'] and p['foreign_catalog_unchanged']
            assert all(n==0 for n in p['commercial_counts'].values()) and sandbox['engine'] is engine
            assert sandbox['tokens']==tokens and sandbox['control']==private and notice['token']==notifier_token
            assert credentials()==initial_credentials and not lock.locked()
            if row['canonical_key'].startswith('I:'):
                product=next(x for x in p['observed_catalog']['products']if x['id']==1)
                assert product['price']==24 and any(g['minimum']==2 and g['maximum']==3 and g['required']for g in product['modifier_groups'])
            sender=row['fixture_contract'].get('fresh_sender','519990170001')
            with SessionLocal.begin() as db:
                db.add(AgentAttentionRequest(business_id=2,branch_id=2,sender=sender,kind='local_fixture_marker',
                    summary='LOCAL ASSEMBLY ARCHIVE MARKER ONLY',source_key='r07-local-'+str(index)))
            last=row;results.append({'case_key':row['canonical_key'],'schema':p['schema'],'tables_initially_empty':True})
        status,last_snapshot=dispatch('GET','/control/snapshot?business_id=2&sender='+last['fixture_contract']['fresh_sender'],b'',private)
        write('archives/15.json',last_snapshot);archives+=1
        # One fixed local order solely to exercise the actual visual route.
        status,_=dispatch('POST','/control/start-case',json.dumps({'case_id':'SMOKE01-GREETING-PIZZA-CASH'}).encode(),private)
        assert status==200
        from datetime import datetime,timezone
        with SessionLocal.begin() as db:
            order=Order(business_id=2,branch_id=2,number='LOCAL-R07-VISUAL',folio=1,source='whatsapp_agent',
                channel='takeaway',status='confirmed',payment_method='cash',customer_name='LOCAL NO ENVIAR',
                customer_phone='519990120001',whatsapp_chat_id='519990120001',total=3,subtotal=3,
                submitted_at=datetime.now(timezone.utc))
            db.add(order);db.flush();db.add(OrderItem(order_id=order.id,product_id=102,product_name='Agua sin gas',quantity=1,unit_price=3,line_total=3))
        with engine.connect() as c:before_image=c.connection.driver_connection.serialize()
        status,envelope=dispatch('POST','/control/visual-snapshot',json.dumps({'case_id':'SMOKE01-GREETING-PIZZA-CASH','sender':'519990120001'}).encode(),private)
        assert status==200 and envelope['source_unchanged'] and not lock.locked()
        visual=load('r07_visual_decoder',HERE/'visual_snapshot.py')
        raw=visual.decode_snapshot(envelope);assert raw==before_image
        with engine.connect() as c:assert c.connection.driver_connection.serialize()==before_image
        proxy=load('r07_fixed_proxy_validation',HERE/'proxy.py')
        for route in ['/control/start-original-case','/control/visual-snapshot']:
            proxy.validate_request('POST',route,[],b'')
        try:proxy.validate_request('POST','https://example.invalid/control/start-original-case',[],b'')
        except proxy.ProxyFailure:pass
        else:raise AssertionError('PROXY_ACCEPTED_ARBITRARY_ORIGIN')
        controller=load('r07_controller_proposal',HERE/'controller.py')
        previous_controller=load('r06_frozen_controller',HERE.parent/'notifier-runtime-r06/controller.py')
        new=controller.build_proposal()['workflow'];old=previous_controller.build_proposal()['workflow']
        prepare=next(n for n in new['nodes']if n['id']=='prepare')['parameters']['jsCode']
        restored=json.loads(json.dumps(new))
        next(n for n in restored['nodes']if n['id']=='prepare')['parameters']['jsCode']=next(n for n in old['nodes']if n['id']=='prepare')['parameters']['jsCode']
        assert restored==old and new['active'] is False
        write('controller-prepare-test-input.json',{'code':prepare,'contracts':[{k:r[k]for k in ['canonical_key','fixture_contract_sha256']}for r in rows]})
        assert not blocked and credentials()==initial_credentials
        result={'schema':'escalarai.original-runtime-r07-local-integration.v1','status':'PASS_LOCAL_ONLY',
            'api70_pins':70,'fixed_original_contracts':15,'archives_before_next_reset':archives,'unauthorized_and_invalid_contracts_no_reset':True,
            'same_engine_and_three_tokens':True,'original_pizza_price24_minimum2_preserved':True,
            'visual_route_exact_image_source_unchanged':True,'visual_bytes':len(raw),'visual_sha256':sha(raw),
            'controller_all_nodes_edges_credentials_settings_preserved_except_prepare':True,'controller_off':True,
            'proxy_origin_and_limits_preserved':True,'local_visual_fixture_orders':1,
            'network_attempts':0,'models':0,'vps':0,'originals_executed':0,'originals_accepted':0,'physical_printing':False,
            'wall_ms':round((time.monotonic()-started)*1000),'contracts':results}
        write('result.json',result);print(json.dumps({k:v for k,v in result.items()if k!='contracts'}))
    except Exception as e:
        write('failure.json',{'state':'LOCAL_FAILURE_PRESERVED','error_class':type(e).__name__,'error':str(e)[:300],
            'network_attempts':len(blocked),'models':0,'originals_executed':0})
        raise
    finally:
        if sandbox:sandbox['client'].close();sandbox['engine'].dispose()

if __name__=='__main__':main()
