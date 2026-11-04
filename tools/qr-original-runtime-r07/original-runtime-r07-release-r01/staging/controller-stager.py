"""SOURCE-ONLY fresh OFF controller clone proposal. Never activates a workflow.

ROOT supplies fresh readback/gates/reference metadata in inputs/. --prepare is
local only; --apply later makes at most eight fixed REST metadata requests.
The existing controller and all fourteen native flows are never PUT/deleted.
"""
import copy
import hashlib
import importlib.util
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE=Path(__file__).resolve().parent
PHASE=HERE.parents[1]
PACK=PHASE/'verification/original-runtime-r07'
INPUT=HERE/'inputs'
PREPARED=HERE/'prepared'
APPLIED=HERE/'applied'
OLD='pNYNOdf96YGoffVb'
PATH='qr-api11-eval-20261104/control-r07'
FIELDS=('name','nodes','connections','settings')
PACK_HASH='ff514ad7767879624c5ca35c829efda3b5984707aeb9fafe478f7cbf374bfc7e'
REVIEW_HASH='9894ae4158ec41a1a4e5e6c76a216f1a0da73404716216969e5cb2d5973afd53'
SOURCE_HASH='ebecfbf403245fa86502f623d1c6d9370ca221e91840a7ee9e1dd29214aff826'

def need(ok,code):
    if not ok: raise AssertionError(code)

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path): return json.loads(path.read_text(encoding='utf-8-sig'))
def editable(graph): return {key:copy.deepcopy(graph[key]) for key in FIELDS}
def write(directory,name,value):
    with (directory/name).open('x',encoding='utf-8') as target: json.dump(value,target,ensure_ascii=False,indent=2);target.write('\n')

def gates():
    value=load(INPUT/'deployment-apply-result.json')
    need(value.get('state')=='R07_ISOLATED_FRESH_FIXTURE_ONLY_READY_IMPORT_AND_CONTROL_PENDING' and
         value.get('r12_case_archived') is True and value.get('binding_permissions_verified') is True and
         value.get('model_block_closure_required') is False and value.get('sourcepack_manifest_sha256')==PACK_HASH and
         value.get('local_results_sha256')==REVIEW_HASH, 'ACTUAL_DEPLOYMENT_ARCHIVED_READY_RECEIPT')
    refresh=load(INPUT/'producer-control-refresh.registry.json')
    need(refresh.get('status')=='SYNTHETIC_IMPORT_COMPLETED' and refresh.get('credentials')==2 and
         refresh.get('ids')==['QrEvalCtl577034d91271','QrEvalPos5499111580ba'] and
         refresh.get('source_binding_sha256')==value.get('private_binding_sha256') and
         refresh.get('only_data_value_changed') is True and refresh.get('cleanup_required') is False,
         'EXACT_TWO_SYNTHETIC_REFRESH_COMPLETION_RECEIPT')
    ref=load(INPUT/'notifier-created.reference.json')
    cleanup=load(INPUT/'notifier-create.registry.json')
    need(ref.get('state')=='CREATED_NEW_ASSIGNED_ID_VERIFIED_PROJECT' and ref.get('created_count')==1 and
         ref.get('project_id')=='Wz76dpTUvZYQaRdl' and cleanup.get('key_removed') is True and
         cleanup.get('credential_id')==ref.get('credential_id') and
         ref.get('binding_sha256')==value.get('private_binding_sha256'), 'OWN_CREDENTIAL_AND_KEY_CLEANUP_RECEIPTS')
    return value,ref


def prepare():
    need(sha(PACK/'sourcepack-manifest.json')==PACK_HASH and sha(PACK/'SOURCE-RESULTS.json')==REVIEW_HASH and
         sha(PACK/'controller.py')==SOURCE_HASH,'FROZEN_REVIEWED_R07_SOURCE_REQUIRED')
    value,ref=gates()
    baseline=load(INPUT/'controller-baseline.FULL.json')
    need(baseline.get('id')==OLD and isinstance(baseline.get('versionId'),str) and baseline.get('active') is False and baseline.get('activeVersionId') is None,'FRESH_OFF_EXISTING_CONTROLLER_REFERENCE')
    spec=importlib.util.spec_from_file_location('reviewed_r07_pure_controller',PACK/'controller.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    proposal=module.build_proposal()
    need(proposal['expected_existing_controller_id']==OLD and proposal['workflow']['active'] is False,
         'PURE_OFF_PROPOSAL_REFERENCE')
    candidate=editable(proposal['workflow'])
    inverse=copy.deepcopy(candidate)
    prepare_node=next(node for node in inverse['nodes'] if node['id']=='prepare')
    code=prepare_node['parameters']['jsCode']
    transforms=load(PACK/'provenance/source-transforms.json')['transforms']
    for item in reversed(transforms):
        if item['file']=='controller.py':
            need(code.count(item['to'])==1,'EXACT_R07_ROUTE_SOURCE_SEAM')
            code=code.replace(item['to'],item['from'])
    start="}else if(['notice-ready','notice-snapshot'].includes(body.operation)"
    end="}else if(!(body.operation==='ack-once'"
    need(code.count(start)==1 and code.count(end)==1,'EXACT_NOTICE_BRANCH_BOUNDARIES')
    first,last=code.index(start),code.index(end)
    need(first<last and "body.sender==='519990900001'" in code[first:last],'FIXED_NOTICE_SENDER')
    prepare_node['parameters']['jsCode']=code[:first]+code[last:]
    need(inverse==editable(baseline),'EXACT_OLD_EIGHT_NODES_CREDENTIALS_SETTINGS_AND_ROUTES')
    entry=next(node for node in candidate['nodes'] if node['id']=='entry')
    entry['parameters']['path']=PATH
    entry['webhookId']='qr-api11-eval-control-r07-20261104'
    entry['disabled']=True
    candidate['name']+=' · R07 fresh OFF clone'
    need(len(candidate['nodes'])==8 and len({node['id'] for node in candidate['nodes']})==8,'EXACT_EIGHT_NODE_IDS')
    # Inverse proof for the three explicit clone-identity seams too.
    restored=copy.deepcopy(candidate)
    restored['name']=proposal['workflow']['name']
    cloned_entry=next(node for node in restored['nodes'] if node['id']=='entry')
    original_entry=next(node for node in proposal['workflow']['nodes'] if node['id']=='entry')
    for key in ('parameters','webhookId'): cloned_entry[key]=copy.deepcopy(original_entry[key])
    cloned_entry.pop('disabled',None)
    need(restored==editable(proposal['workflow']),'ONLY_EXPLICIT_CLONE_IDENTITY_SEAMS')
    PREPARED.mkdir(exist_ok=False)
    write(PREPARED,'controller.OFF.json',candidate)
    write(PREPARED,'baseline.FULL.json',baseline)
    write(PREPARED,'root-gates.json',value)
    write(PREPARED,'notifier-created.reference.json',ref)
    write(PREPARED,'manifest.json',{'state':'PREPARED_FRESH_OFF_CLONE_NOT_STAGED',
          'old_controller_id':OLD,'new_workflow_id':'SERVER_ASSIGNED_ON_ONE_CREATE',
          'webhook_path':PATH,'controller_sha256':sha(PREPARED/'controller.OFF.json'),
          'baseline_sha256':sha(PREPARED/'baseline.FULL.json'),'sourcepack_sha256':PACK_HASH,
          'all_old_nodes_preserved_by_inverse':True,'entry_disabled':True,'activation_allowed':False,
          'credential_reference_only':True,'network':0,'models':0})

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None

def apply():
    gates() # Re-check separate refresh and own key cleanup receipt; never infer.
    manifest=load(PREPARED/'manifest.json')
    need(sha(PREPARED/'controller.OFF.json')==manifest['controller_sha256'] and
         sha(PREPARED/'baseline.FULL.json')==manifest['baseline_sha256'],'PREPARED_INPUTS_CHANGED')
    candidate=load(PREPARED/'controller.OFF.json');baseline=load(PREPARED/'baseline.FULL.json')
    APPLIED.mkdir(exist_ok=False) # Consumes this whole attempt before any request.
    need(sha(PHASE/'deploy-n8n.py')=='7c925ceaca3894ab6049d88697d47fd736028f9e7550567b8b64d2485d4fe037',
         'EXISTING_API_HELPER_PIN')
    spec=importlib.util.spec_from_file_location('reviewed_r07_api',PHASE/'deploy-n8n.py')
    api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
    need(api.BASE=='https://n8n.srv1547104.hstgr.cloud','FIXED_N8N_ORIGIN')
    urllib.request.install_opener(urllib.request.build_opener(NoRedirect()))
    own=None;failure=None;cleanup=None;staged=False;calls=0;started=time.monotonic()
    def request(method,path,body=None):
        nonlocal calls
        need(calls<8 and time.monotonic()-started<90,'BOUNDED_EIGHT_REQUESTS_90_SECONDS')
        need((method=='GET' and path=='/workflows/'+OLD) or
             (method=='POST' and path=='/workflows') or
             (own is not None and path=='/workflows/'+own and method in ('GET','PUT')) or
             (own is not None and path=='/workflows/'+own+'/deactivate' and method=='POST'),
             'GET_OLD_OR_NEW_OWN_ID_ONLY_NO_ACTIVATION')
        calls+=1
        return api.request(method,path,body,timeout=min(15,90-(time.monotonic()-started)))
    try:
        current=request('GET','/workflows/'+OLD)
        write(APPLIED,'old.before.FULL.json',current)
        need(current==baseline,'FRESH_BASELINE_FULL_CHANGED_STOP')
        created=request('POST','/workflows',candidate)
        wid=created.get('id')
        protected=load(PHASE/'n8n/joint-bindings.json')['profiles']
        denied={OLD,'9IDkhRko8wx9lmOH'}|{v for profile in protected.values() for v in profile['workflow_ids'].values()}
        need(isinstance(wid,str) and re.fullmatch(r'[A-Za-z0-9]{8,40}',wid) is not None and wid not in denied,
             'NEW_SERVER_ASSIGNED_CONTROLLER_ID')
        own=wid
        write(APPLIED,'owned.reference.json',{'workflow_id':own,'name':candidate['name'],'path':PATH,'active':False})
        actual=request('GET','/workflows/'+own)
        write(APPLIED,'new.OFF.FULL.json',actual)
        need(actual['id']==own and actual['active'] is False and actual.get('activeVersionId') is None and
             editable(actual)==candidate,'NEW_CONTROLLER_EXACT_OFF_UNPUBLISHED_READBACK')
        unchanged=request('GET','/workflows/'+OLD)
        write(APPLIED,'old.after.FULL.json',unchanged)
        need(unchanged==baseline,'OLD_CONTROLLER_FULL_CHANGED')
        staged=True
    except Exception as error:
        failure={'type':type(error).__name__,'http_status':getattr(error,'code',None)}
    finally:
        if own and not staged:
            cleanup_errors=[]
            scrub=copy.deepcopy(candidate)
            for node in scrub['nodes']:
                node['disabled']=True;node.pop('credentials',None)
                if node['type']=='n8n-nodes-base.code':node['parameters']['jsCode']='return [];'
            try:
                request('POST','/workflows/'+own+'/deactivate',{})
            except Exception as error:
                cleanup_errors.append({'step':'OWN_DEACTIVATE','type':type(error).__name__})
            try:
                request('PUT','/workflows/'+own,scrub)
            except Exception as error:
                cleanup_errors.append({'step':'OWN_SCRUB','type':type(error).__name__})
            try:
                closed=request('GET','/workflows/'+own)
                write(APPLIED,'failure.cleanup.FULL.json',closed)
                need(closed['active'] is False and closed.get('activeVersionId') is None and editable(closed)==scrub,
                     'FAILED_OWN_CLONE_FULL_OFF_ERASED')
                cleanup='OWN_OFF_UNPUBLISHED_CODE_ERASED_CREDS_EMPTY'
            except Exception as error:
                cleanup_errors.append({'step':'OWN_FINAL_READBACK','type':type(error).__name__})
                cleanup='UNKNOWN_'+type(error).__name__
            write(APPLIED,'failure.cleanup-errors.json',cleanup_errors)
        result={'state':'STAGED_FRESH_OFF_CLONE_NOT_ACTIVATED' if staged else 'FAILED_OR_UNKNOWN_NO_RETRY',
                'workflow_id':own,'failure':failure,'cleanup':cleanup,'requests':calls,
                'old_controller_puts':0,'production_credential_access':0,'activation':False,'models':0}
        write(APPLIED,'result.json',result);print(json.dumps(result))
    return 0 if staged else 1

if __name__=='__main__':
    need(len(sys.argv)==2 and sys.argv[1] in ('--prepare','--apply'),'EXACT_PREPARE_OR_APPLY_CLI')
    if sys.argv[1]=='--prepare':prepare()
    else:sys.exit(apply())
