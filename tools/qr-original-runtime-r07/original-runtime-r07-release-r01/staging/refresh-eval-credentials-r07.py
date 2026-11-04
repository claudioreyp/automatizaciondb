"""Prepared VPS-only synthetic credential refresh. Never prints secret values.
No direct HTTP/model call is made. The explicit CLI import updates only the
two synthetic credential IDs. ROOT may explicitly invoke
--apply --all14-off-confirmed on the VPS after reviewing this file.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
OLD = Path('/opt/escalar-qr/private/eval-credentials-r05.json')
NEW = Path('/opt/escalar-qr/private/eval-credentials-r07.json')
BINDING = Path('/opt/escalar-qr/eval-run-r07/binding.json')
REGISTRY = Path('/opt/escalar-qr/private/eval-refresh-r07.registry.json')
CONTAINER = 'n8n-n8n-1'
IMPORT_FILE = '/tmp/import-r07.json'
PROJECT_ID = 'Wz76dpTUvZYQaRdl'
DEADLINE_SECONDS = 45
EXPECTED = {
    'QrEvalPos5499111580ba': {'name': 'QR API isolated 20261104 2/2', 'header': 'Authorization'},
    'QrEvalCtl577034d91271': {'name': 'QR API isolated control 20261104', 'header': 'X-Evaluation-Control'},
}
class Stop(Exception):
    pass

def need(ok, code):
    if not ok:
        raise Stop(code)

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def token(value, prefix=''):
    return isinstance(value, str) and re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9_-]{54}', value) is not None

def build_payload(old, binding):
    need(isinstance(old, list) and len(old) == 2, 'EXACTLY_TWO_SYNTHETIC_CREDENTIALS_REQUIRED')
    need(all(isinstance(c, dict) for c in old), 'CREDENTIAL_SHAPE')
    need({c.get('id') for c in old} == set(EXPECTED), 'SYNTHETIC_ID_ALLOWLIST')
    need(binding.get('notifier_scope') == {'business_id':2,'branch_id':2,'profile':'generic','scopes':['events:deliver','events:read','menu:read','orders:read']}, 'R07_FOUR_SCOPE_NOTIFIER_BINDING')
    need(binding.get('schema') == 'escalarai.qr-api-sandbox-binding.v1', 'BINDING_SCHEMA')
    need(binding.get('synthetic_only') is True and binding.get('database') == 'sqlite_memory', 'SYNTHETIC_MEMORY_ONLY')
    need(binding.get('api_version') == '1.1.1' and binding.get('container_isolation') is True, 'API111_CONTAINER_REQUIRED')
    need(binding.get('real_whatsapp') is False and binding.get('physical_printing') is False, 'SIDE_EFFECTS_DISABLED')
    need(binding.get('base_url') == 'http://impulsa-qr-api-eval:8769' and binding.get('api_base_path') == '/api/v1', 'ISOLATED_ORIGIN_REQUIRED')
    tokens = binding.get('agent_tokens')
    need(isinstance(tokens, dict) and set(tokens) == {'2', '3'}, 'EXACT_FIXTURE_SCOPES')
    need(token(tokens['2'], 'smoke-2.') and token(tokens['3'], 'smoke-3.'), 'SYNTHETIC_SCOPE_TOKEN')
    need(token(binding.get('control_token')), 'SYNTHETIC_CONTROL_TOKEN')
    new = copy.deepcopy(old)
    for prior, current in zip(old, new):
        expected = EXPECTED[prior['id']]
        need(prior.get('name') == expected['name'] and prior.get('type') == 'httpHeaderAuth', 'CREDENTIAL_NAME_TYPE_BINDING')
        data = prior.get('data')
        need(isinstance(data, dict) and set(data) == {'name', 'value'} and data['name'] == expected['header'], 'HEADER_SHAPE_BINDING')
        if prior['id'] == 'QrEvalPos5499111580ba':
            need(isinstance(data['value'], str) and data['value'].startswith('Bearer ') and token(data['value'][7:], 'smoke-2.'), 'OLD_SYNTHETIC_POS_ONLY')
            current['data']['value'] = 'Bearer ' + tokens['2']
        else:
            need(token(data['value']), 'OLD_SYNTHETIC_CONTROL_ONLY')
            current['data']['value'] = binding['control_token']
        need(current['data']['value'] != data['value'], 'FRESH_BINDING_REQUIRED')
        inverse = copy.deepcopy(current)
        inverse['data']['value'] = data['value']
        need(inverse == prior, 'ONLY_DATA_VALUE_MAY_CHANGE')
    return new

def validate_input_stats(old_stat, binding_stat, binding_parent_stat):
    need(stat.S_ISREG(old_stat.st_mode) and old_stat.st_uid == old_stat.st_gid == 0 and stat.S_IMODE(old_stat.st_mode) == 0o600, 'OLD_ROOT_PRIVATE_REGULAR_REQUIRED')
    need(stat.S_ISREG(binding_stat.st_mode) and binding_stat.st_uid == binding_stat.st_gid == 10001, 'BINDING_UID10001_REGULAR_REQUIRED')
    need(stat.S_IMODE(binding_stat.st_mode) == 0o600, 'BINDING_FILE0600_REQUIRED')
    need(stat.S_ISDIR(binding_parent_stat.st_mode) and binding_parent_stat.st_uid == binding_parent_stat.st_gid == 10001, 'BINDING_PARENT_UID10001_DIRECTORY_REQUIRED')
    need(stat.S_IMODE(binding_parent_stat.st_mode) == 0o700, 'BINDING_PARENT0700_REQUIRED')
    need(all(1 <= value.st_size <= 65536 for value in [old_stat, binding_stat]), 'SMALL_FIXED_INPUT_REQUIRED')

def read_input_no_follow(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        current = os.fstat(source.fileno())
        need(stat.S_ISREG(current.st_mode) and 1 <= current.st_size <= 65536, 'FIXED_INPUT_REGULAR_SIZE_REQUIRED')
        expected_uid = 0 if path == OLD else 10001 if path == BINDING else None
        need(expected_uid is not None and current.st_uid == expected_uid, 'FIXED_INPUT_UID_REQUIRED')
        need(stat.S_IMODE(current.st_mode) == 0o600, 'PRIVATE_INPUT_FILE0600_REQUIRED')
        raw = source.read(65537)
        need(len(raw) == current.st_size, 'FIXED_INPUT_CHANGED_DURING_READ')
        return raw


def read_deployment_receipt():
    path=Path('/opt/escalar-qr/private/original-r07-deploy-r01/apply-result.json')
    parent=path.parent.lstat()
    need(not path.parent.is_symlink() and path.parent.resolve()==path.parent and
         stat.S_ISDIR(parent.st_mode) and parent.st_uid==parent.st_gid==0 and
         stat.S_IMODE(parent.st_mode)==0o700, 'DEPLOYMENT_RECEIPT_PRIVATE_PARENT')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        current=os.fstat(stream.fileno())
        need(stat.S_ISREG(current.st_mode) and current.st_uid==current.st_gid==0 and
             stat.S_IMODE(current.st_mode)==0o600 and 1<=current.st_size<=65536,
             'DEPLOYMENT_RECEIPT_PRIVATE_FILE')
        raw=stream.read(65537)
        need(len(raw)==current.st_size,'DEPLOYMENT_RECEIPT_CHANGED')
        return json.loads(raw)

def validate_source_snapshots(old_bytes, binding_bytes, new_bytes, old_sha, binding_sha, new_sha):
    need(sha(old_bytes) == old_sha, 'OLD_SOURCE_CHANGED')
    need(sha(binding_bytes) == binding_sha, 'BINDING_SOURCE_CHANGED')
    need(sha(new_bytes) == new_sha, 'NEW_PAYLOAD_CHANGED')

def write_exclusive(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())

def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

def import_node_code(payload_sha, payload_bytes, budget_ms):
    need(re.fullmatch(r'[a-f0-9]{64}', payload_sha) is not None, 'PAYLOAD_DIGEST')
    need(isinstance(payload_bytes, int) and 1 <= payload_bytes <= 65536, 'PAYLOAD_LENGTH')
    need(isinstance(budget_ms, int) and 1000 <= budget_ms <= 30000, 'IMPORT_TIME_BUDGET')
    # The child runs as node, reads only the fixed private import file and
    # inherits n8n's configured encryption key. All child output is hashed.
    return r"""const fs=require('fs'),crypto=require('crypto'),cp=require('child_process');
const p='/tmp/import-r07.json',fd=fs.openSync(p,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK),s=fs.fstatSync(fd);
if(!s.isFile()||s.uid!==process.getuid()||(s.mode&0o777)!==0o600||s.size!==__BYTES__){fs.closeSync(fd);process.exit(31);}
const raw=fs.readFileSync(fd);fs.closeSync(fd);if(raw.length!==__BYTES__||crypto.createHash('sha256').update(raw).digest('hex')!=='__SHA__')process.exit(31);
const rows=JSON.parse(raw);if(!Array.isArray(rows)||rows.length!==2||rows.map(x=>x.id).sort().join('|')!=='QrEvalCtl577034d91271|QrEvalPos5499111580ba')process.exit(32);
let countOut=0,countErr=0,timedOut=false,finished=false;const out=crypto.createHash('sha256'),err=crypto.createHash('sha256');
const child=cp.spawn('n8n',['import:credentials','--input','/tmp/import-r07.json','--projectId','Wz76dpTUvZYQaRdl'],{stdio:['ignore','pipe','pipe']});
child.stdout.on('data',d=>{countOut+=d.length;out.update(d);});child.stderr.on('data',d=>{countErr+=d.length;err.update(d);});
const timer=setTimeout(()=>{timedOut=true;child.kill('SIGTERM');setTimeout(()=>{if(!finished)child.kill('SIGKILL');},1500).unref();},__MS__);
child.on('error',()=>{clearTimeout(timer);finished=true;process.stdout.write(JSON.stringify({status:'IMPORT_SPAWN_UNKNOWN',timed_out:false}));process.exitCode=41;});
child.on('close',(code,signal)=>{if(finished)return;finished=true;clearTimeout(timer);process.stdout.write(JSON.stringify({status:code===0&&!timedOut?'IMPORT_EXIT_ZERO':'IMPORT_RESULT_UNKNOWN',exit_code:code,signal:signal,timed_out:timedOut,stdout_bytes:countOut,stderr_bytes:countErr,stdout_sha256:out.digest('hex'),stderr_sha256:err.digest('hex')}));process.exitCode=code===0&&!timedOut?0:42;});
""".replace('__SHA__', payload_sha).replace('__BYTES__', str(payload_bytes)).replace('__MS__', str(budget_ms))

DESTINATION_CHECK = "const f=require('fs');try{f.lstatSync('/tmp/import-r07.json');process.exit(27);}catch(e){if(e.code!=='ENOENT')throw e;}process.stdout.write(JSON.stringify({status:'DESTINATION_ABSENT'}));"
def cleanup_node_code(payload_sha, payload_bytes):
    need(re.fullmatch(r'[a-f0-9]{64}', payload_sha) is not None, 'PAYLOAD_DIGEST')
    return "const f=require('fs'),c=require('crypto'),p='/tmp/import-r07.json';let s;try{s=f.lstatSync(p);}catch(e){if(e.code!=='ENOENT')throw e;}if(s){if(!s.isFile()||s.isSymbolicLink()||s.size!==" + str(payload_bytes) + ")process.exit(29);if(c.createHash('sha256').update(f.readFileSync(p)).digest('hex')!=='" + payload_sha + "')process.exit(29);f.unlinkSync(p);}process.stdout.write(JSON.stringify({status:'PRIVATE_IMPORT_REMOVED'}));"


def run_import(run_command, payload_sha, payload_bytes, deadline, monotonic=time.monotonic):
    registry = []
    import_entered = False
    private_input_may_remain = False
    def command(role, arguments, reserve=0):
        remaining = deadline - monotonic() - reserve
        need(remaining > 0, 'TOTAL45_DEADLINE')
        metadata = {'role': role, 'status': 'STARTED'}
        registry.append(metadata)
        try:
            completed = run_command(arguments, timeout=remaining)
        except Exception as exc:
            metadata.update({'status': 'TRANSPORT_UNKNOWN', 'exception_type': type(exc).__name__})
            raise
        metadata.update({'status': 'RETURNED', 'returncode': completed.returncode,
                    'stdout_bytes': len(completed.stdout), 'stderr_bytes': len(completed.stderr),
                    'stdout_sha256': sha(completed.stdout), 'stderr_sha256': sha(completed.stderr)})
        need(completed.returncode == 0, 'COMMAND_' + role + '_UNKNOWN_STOP')
        return completed.stdout
    try:
        command('CHECK_DESTINATION', ['docker', 'exec', '--user', 'root', CONTAINER, 'node', '-e', DESTINATION_CHECK], reserve=6)
        private_input_may_remain = True
        command('COPY_PRIVATE_INPUT', ['docker', 'cp', str(NEW), CONTAINER + ':' + IMPORT_FILE], reserve=6)
        command('OWN_PRIVATE_INPUT', ['docker', 'exec', '--user', 'root', CONTAINER, 'chown', '-h', 'node:node', IMPORT_FILE], reserve=6)
        remaining = deadline - monotonic()
        need(remaining >= 8, 'INSUFFICIENT_TIME_BEFORE_IMPORT')
        budget_ms = min(30000, int((remaining - 5) * 1000) - 2000)
        script = import_node_code(payload_sha, payload_bytes, budget_ms)
        import_entered = True
        response = command('IMPORT_TWO_FIXED_IDS', ['docker', 'exec', '--user', 'node', CONTAINER, 'node', '-e', script], reserve=3)
        parsed = json.loads(response)
        need(isinstance(parsed, dict) and set(parsed) == {'status', 'exit_code', 'signal', 'timed_out', 'stdout_bytes', 'stderr_bytes', 'stdout_sha256', 'stderr_sha256'}, 'IMPORT_REGISTRY_SHAPE')
        need(parsed.get('status') == 'IMPORT_EXIT_ZERO' and parsed.get('exit_code') == 0 and parsed.get('timed_out') is False and parsed.get('signal') is None, 'IMPORT_REGISTRY_NOT_CONFIRMED')
        need(all(isinstance(parsed[k], int) and not isinstance(parsed[k], bool) and parsed[k] >= 0 for k in ['stdout_bytes', 'stderr_bytes']), 'IMPORT_REGISTRY_COUNTS')
        need(all(isinstance(parsed[k], str) and re.fullmatch(r'[a-f0-9]{64}', parsed[k]) is not None for k in ['stdout_sha256', 'stderr_sha256']), 'IMPORT_REGISTRY_DIGESTS')
        command('REMOVE_PRIVATE_CONTAINER_INPUT', ['docker', 'exec', '--user', 'node', CONTAINER, 'node', '-e', cleanup_node_code(payload_sha, payload_bytes)])
        private_input_may_remain = False
        return {'status': 'SYNTHETIC_IMPORT_COMPLETED', 'import_entered': True, 'commands': registry,
                'import_registry': parsed, 'credentials': 2, 'project_id': PROJECT_ID,
                'ids': sorted(EXPECTED), 'scope': {'business_id': 2, 'branch_id': 2},
                'models': 0, 'workflow_changes': 0, 'automatic_retry': False, 'cleanup_required': False}
    except Exception as exc:
        return {'status': 'STOP_IMPORT_UNKNOWN_NO_RETRY' if import_entered else 'STOP_BEFORE_IMPORT_NO_RETRY',
                'reason': str(exc) if isinstance(exc, Stop) else type(exc).__name__,
                'import_entered': import_entered, 'commands': registry, 'models': 0,
                'workflow_changes': 0, 'automatic_retry': False, 'cleanup_required': private_input_may_remain, 'container_import_file': IMPORT_FILE}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--all14-off-confirmed', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({'status': 'PREPARED_NOT_RUN', 'credentials': 2, 'ids': sorted(EXPECTED),
                          'deadline_seconds': 45, 'network': 0, 'imports': 0, 'models': 0}))
        return
    need(args.all14_off_confirmed, 'ROOT_ALL14_OFF_PRECONDITION_REQUIRED')
    need(sys.platform == 'linux' and os.geteuid() == 0, 'VPS_ROOT_ONLY')
    need(not REGISTRY.exists() and not NEW.exists(), 'R07_ATTEMPT_ALREADY_CONSUMED')
    deployed = read_deployment_receipt()
    need(deployed.get('state') == 'R07_ISOLATED_FRESH_FIXTURE_ONLY_READY_IMPORT_AND_CONTROL_PENDING' and deployed.get('r12_case_archived') is True and deployed.get('model_block_closure_required') is False and deployed.get('sourcepack_manifest_sha256') == 'ff514ad7767879624c5ca35c829efda3b5984707aeb9fafe478f7cbf374bfc7e', 'ACTUAL_R07_DEPLOYMENT_RECEIPT')
    deadline = time.monotonic() + DEADLINE_SECONDS
    registry = {'status': 'STARTED_ONE_SHOT', 'all14_off_precondition': 'ROOT_ATTESTED', 'models': 0, 'workflow_changes': 0}
    write_exclusive(REGISTRY, encode(registry))
    old_sha = None
    binding_sha = None
    try:
        validate_input_stats(OLD.lstat(), BINDING.lstat(), BINDING.parent.lstat())
        old_bytes, binding_bytes = read_input_no_follow(OLD), read_input_no_follow(BINDING)
        old_sha, binding_sha = sha(old_bytes), sha(binding_bytes)
        need(binding_sha == deployed.get('private_binding_sha256'), 'DEPLOYED_BINDING_SHA_CHANGED')
        old, binding = json.loads(old_bytes), json.loads(binding_bytes)
        new = build_payload(old, binding)
        payload = encode(new)
        write_exclusive(NEW, payload)
        need(stat.S_IMODE(NEW.stat().st_mode) == 0o600, 'NEW_INPUT_MODE0600_REQUIRED')
        validate_source_snapshots(read_input_no_follow(OLD), read_input_no_follow(BINDING), NEW.read_bytes(), old_sha, binding_sha, sha(payload))
        def run_command(arguments, timeout):
            return subprocess.run(arguments, timeout=timeout, check=False, capture_output=True)
        registry = run_import(run_command, sha(payload), len(payload), deadline)
        registry.update({'old_file_sha256': old_sha, 'source_binding_sha256': binding_sha, 'new_file_sha256': sha(payload), 'all14_off_precondition': 'ROOT_ATTESTED'})
        validate_source_snapshots(read_input_no_follow(OLD), read_input_no_follow(BINDING), NEW.read_bytes(), old_sha, binding_sha, sha(payload))
        registry['old_file_unchanged'] = True
        registry['source_binding_unchanged'] = True
        registry['new_file_verified_exact'] = True
        registry['input_uids'] = {'old': 0, 'binding': 10001}
        registry['only_data_value_changed'] = True
    except Exception as exc:
        registry.update({'status': 'STOP_IMPORT_UNKNOWN_NO_RETRY' if registry.get('import_entered') else 'STOP_PREPARATION_NO_RETRY',
                         'reason': str(exc) if isinstance(exc, Stop) else type(exc).__name__,
                         'models': 0, 'workflow_changes': 0, 'automatic_retry': False})
        if old_sha is not None:
            registry['old_file_unchanged'] = OLD.is_file() and not OLD.is_symlink() and sha(OLD.read_bytes()) == old_sha
        if binding_sha is not None:
            registry['source_binding_unchanged'] = BINDING.is_file() and not BINDING.is_symlink() and sha(BINDING.read_bytes()) == binding_sha
    REGISTRY.write_bytes(encode(registry))
    print(json.dumps(registry, ensure_ascii=True))
    if registry.get('status') != 'SYNTHETIC_IMPORT_COMPLETED':
        raise SystemExit(1)

if __name__ == '__main__':
    main()
