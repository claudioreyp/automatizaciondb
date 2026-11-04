"""SOURCE-ONLY VPS one-shot create-only credential proposal. Do not auto-run.

Default CLI prints a nonsecret scope. --apply requires ROOT's reviewed R07
runtime/gates and a one-use API key staged outside the workspace. No CLI import
or existing-credential update is used. Never serializes payload/header/token.
"""
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.request
from pathlib import Path

PRIVATE=Path('/opt/escalar-qr/private')
BINDING=Path('/opt/escalar-qr/eval-run-r07/binding.json')
KEY=PRIVATE/'n8n-api-key-r07'
DEPLOY_RESULT=PRIVATE/'original-r07-deploy-r01/apply-result.json'
MARKER=PRIVATE/'notifier-r07-create.registry.json'
REFERENCE=PRIVATE/'notifier-r07-created.reference.json'
BASE='https://n8n.srv1547104.hstgr.cloud/api/v1'
PROJECT='Wz76dpTUvZYQaRdl'
NAME='QR API isolated notifier QrEvalNoticeR0720261104'
SCOPE={'business_id':2,'branch_id':2,'profile':'generic','scopes':['events:deliver','events:read','menu:read','orders:read']}
PACK='ff514ad7767879624c5ca35c829efda3b5984707aeb9fafe478f7cbf374bfc7e'
REVIEW='9894ae4158ec41a1a4e5e6c76a216f1a0da73404716216969e5cb2d5973afd53'
FORBIDDEN={'isSVaRTGTA42G88Z','QrEvalPos5499111580ba','QrEvalCtl577034d91271','r6usoZc9HKfkgluB'}

def need(ok,code):
    if not ok: raise AssertionError(code)

def digest(raw): return hashlib.sha256(raw).hexdigest()

def private_read(path,uid,gid,maximum=65536):
    parent=path.parent.lstat()
    need(stat.S_ISDIR(parent.st_mode) and not path.parent.is_symlink() and path.parent.resolve()==path.parent and
         parent.st_uid==uid and parent.st_gid==gid and stat.S_IMODE(parent.st_mode)==0o700,'PRIVATE_PARENT_OWNER_MODE')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        value=os.fstat(stream.fileno())
        need(stat.S_ISREG(value.st_mode) and value.st_uid==uid and value.st_gid==gid and
             stat.S_IMODE(value.st_mode)==0o600 and 0<value.st_size<=maximum,'PRIVATE_FILE_OWNER_MODE_SIZE')
        raw=stream.read(maximum+1)
        need(len(raw)==value.st_size,'PRIVATE_INPUT_CHANGED')
        return raw,(value.st_dev,value.st_ino,value.st_uid,value.st_gid,value.st_mode),digest(raw)

def exclusive(path,value):
    raw=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream: stream.write(raw);stream.flush();os.fsync(stream.fileno())

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None

def run():
    need(sys.platform=='linux' and os.geteuid()==0,'VPS_ROOT_ONLY')
    need(not MARKER.exists() and not REFERENCE.exists(),'ATTEMPT_ALREADY_CONSUMED_NO_RETRY')
    parent=PRIVATE.lstat()
    need(stat.S_ISDIR(parent.st_mode) and not PRIVATE.is_symlink() and parent.st_uid==0 and
         parent.st_gid==0 and stat.S_IMODE(parent.st_mode)==0o700,'PRIVATE_ROOT_PARENT_REQUIRED')
    state={'state':'STARTED_ONE_SHOT_NO_RETRY','requests':0,'credential_id':None,'payload_files':0,'key_removed':False,
           'models':0,'workflow_changes':0,'activation':False}
    exclusive(MARKER,state)
    key_identity=None;key_hash=None;created=None;failure=None;started=time.monotonic()
    try:
        key_raw,key_identity,key_hash=private_read(KEY,0,0,8192)
        key=key_raw.decode('utf-8-sig').strip()
        need(key and '\r' not in key and '\n' not in key,'PRIVATE_API_KEY_SHAPE')
        deployed_raw,_,_=private_read(DEPLOY_RESULT,0,0)
        gates=json.loads(deployed_raw)
        need(gates.get('state')=='R07_ISOLATED_FRESH_FIXTURE_ONLY_READY_IMPORT_AND_CONTROL_PENDING' and
             gates.get('r12_case_archived') is True and gates.get('binding_permissions_verified') is True and
             gates.get('model_block_closure_required') is False and
             gates.get('sourcepack_manifest_sha256')==PACK and gates.get('local_results_sha256')==REVIEW,
             'ACTUAL_DEPLOYMENT_ARCHIVED_READY_RECEIPT')
        binding_raw,_,binding_hash=private_read(BINDING,10001,10001)
        binding=json.loads(binding_raw)
        need(binding.get('schema')=='escalarai.qr-api-sandbox-binding.v1' and binding.get('synthetic_only') is True and
             binding.get('database')=='sqlite_memory' and binding.get('container_isolation') is True and
             binding.get('api_version')=='1.1.1' and binding.get('base_url')=='http://impulsa-qr-api-eval:8769' and
             binding.get('api_base_path')=='/api/v1' and binding.get('real_whatsapp') is False and
             binding.get('physical_printing') is False and binding.get('notifier_scope')==SCOPE,'EXACT_SYNTHETIC_BINDING_SCOPE')
        token=binding.get('notifier_token')
        need(isinstance(token,str) and re.fullmatch(r'smoke-notice-2\.[A-Za-z0-9_-]{54}',token) is not None,
             'FRESH_NOTIFIER_TOKEN_FORMAT')
        need(token!=binding.get('control_token') and token not in binding.get('agent_tokens',{}).values(),'NO_AGENT_CONTROL_TOKEN_REUSE')
        need(gates.get('private_binding_sha256')==binding_hash,'ROOT_REVIEWED_BINDING_HASH_REQUIRED')
        opener=urllib.request.build_opener(NoRedirect())
        def request(method,path,body=None):
            need(state['requests']<4 and time.monotonic()-started<60,'BOUNDED_REQUEST_DEADLINE')
            if path=='/credentials': need(method=='POST','CREATE_ONLY_ROUTE')
            elif path=='/credentials?limit=25': need(method=='GET','METADATA_LIST_ONLY')
            else: need(created and path=='/credentials/'+created+'/transfer' and method=='PUT' and
                       body=={'destinationProjectId':PROJECT},'OWN_FRESH_TRANSFER_ONLY')
            state['requests']+=1
            raw=None if body is None else json.dumps(body).encode()
            headers={'X-N8N-API-KEY':key}
            if raw is not None: headers['Content-Type']='application/json'
            req=urllib.request.Request(BASE+path,data=raw,headers=headers,method=method)
            with opener.open(req,timeout=min(20,60-(time.monotonic()-started))) as response:
                result=response.read(1024*1024+1)
                need(len(result)<=1024*1024,'BOUNDED_API_RESPONSE')
                if response.status==204: need(not result,'TRANSFER_204_EMPTY_BODY');return None
                return json.loads(result)
        # Atomic server-assigned create: no ID supplied, no upsert, no existing
        # credential GET/update/delete, and no production credential access.
        result=request('POST','/credentials',{'name':NAME,'type':'httpHeaderAuth',
                       'data':{'name':'Authorization','value':'Bearer '+token}})
        need(isinstance(result,dict) and result.get('name')==NAME and result.get('type')=='httpHeaderAuth' and
             isinstance(result.get('id'),str) and re.fullmatch(r'[A-Za-z0-9]{8,40}',result['id']) is not None and
             result['id'] not in FORBIDDEN,'OWN_CREATED_METADATA')
        created=result['id'];state['credential_id']=created
        # Never persist/print the complete create response: some versions may
        # include encrypted data. Public/private receipts allowlist metadata.
        del result
        def project_metadata():
            page=request('GET','/credentials?limit=25')
            need(isinstance(page,dict) and isinstance(page.get('data'),list) and len(page['data'])<=25,'BOUNDED_LIST_PAGE')
            rows=[row for row in page['data'] if row.get('id')==created]
            need(len(rows)==1 and rows[0].get('name')==NAME and rows[0].get('type')=='httpHeaderAuth','FRESH_ID_IN_NEWEST_PAGE')
            shared=rows[0].get('shared')
            need(isinstance(shared,list) and shared and all(isinstance(item,dict) and
                 set(item)=={'id','name','role','createdAt','updatedAt'} for item in shared),'OFFICIAL_FLAT_PROJECT_METADATA')
            owners=[item for item in shared if item.get('role')=='credential:owner']
            need(len(owners)==1,'ONE_OWNER_PROJECT')
            return owners[0]['id']
        owner=project_metadata()
        if owner!=PROJECT:
            request('PUT','/credentials/'+created+'/transfer',{'destinationProjectId':PROJECT})
            need(project_metadata()==PROJECT,'TARGET_PROJECT_READBACK')
        receipt={'schema':'escalarai.notifier-r07-created-reference.v1','state':'CREATED_NEW_ASSIGNED_ID_VERIFIED_PROJECT',
                 'credential_id':created,'credential_name':NAME,'credential_type':'httpHeaderAuth','project_id':PROJECT,
                 'scope':SCOPE,'binding_sha256':binding_hash,'created_count':1,'payload_files':0,'activation':False,'models':0}
        need(private_read(BINDING,10001,10001)[2]==binding_hash,'SOURCE_BINDING_UNCHANGED')
        exclusive(REFERENCE,receipt)
        state['state']='CREATED_NEW_ASSIGNED_ID_VERIFIED_PROJECT'
    except Exception as error:
        failure={'type':type(error).__name__,'http_status':getattr(error,'code',None)}
        state['state']='UNKNOWN_OR_FAILED_NO_RETRY'
    finally:
        # Remove only the exact one-use key we opened, never an existing normal
        # API key or a replacement at the same path. Do not remove R07 binding.
        if key_identity is not None:
            try:
                _,identity,current_hash=private_read(KEY,0,0,8192)
                need(identity==key_identity and current_hash==key_hash,'ONE_USE_KEY_IDENTITY_CHANGED')
                KEY.unlink();state['key_removed']=True
            except Exception as error:
                state['key_cleanup_error']={'type':type(error).__name__}
                state['creation_state_before_cleanup_failure']=state['state']
                state['state']='FAILED_KEY_CLEANUP_NO_ACTIVATION'
        state['failure']=failure
        state['elapsed_ms']=round((time.monotonic()-started)*1000)
        MARKER.write_text(json.dumps(state,sort_keys=True,indent=2)+'\n',encoding='utf-8')
        # No response bodies, token/hash/key, binding bytes or header values.
        print(json.dumps({key:state[key] for key in ('state','credential_id','requests','payload_files','key_removed','models','activation')}))
    return 0 if state['state']=='CREATED_NEW_ASSIGNED_ID_VERIFIED_PROJECT' and state['key_removed'] else 1

if __name__=='__main__':
    if sys.argv[1:]==['--apply']: sys.exit(run())
    need(not sys.argv[1:],'EXACT_CLI_ONLY')
    print(json.dumps({'state':'SOURCE_PROPOSAL_NOT_RUN','credential_name':NAME,'type':'httpHeaderAuth',
                      'project_id':PROJECT,'scope':SCOPE,'credential_create_maximum':1,'network':0,'models':0}))
