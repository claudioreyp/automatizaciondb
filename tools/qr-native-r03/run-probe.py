"""One disposable native n8n test, with no production data or network.

Run only on the VPS from the reviewed release. Raw output is saved before
evaluation. Every path, image, workflow and package hash is fixed or verified.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import time

RELEASE = Path('/opt/escalar-qr/release/tools/qr-native-r03')
OUT = Path('/opt/escalar-qr/cjs-cli-r03-result')
NAME = 'escalar-cjs-cli-r03-20261104'
WORKFLOW_ID = 'QrCjsR03Cli20261104'
PINS = {'probe/workflow.OFF.json':'9fe540d773ff159562639fa13fbbd83ca56abb8252dd9285376f19a281ee739f',
        'package/index.cjs':'cc64011e21b074ae1fcad83909294fb8e200ab70712d872a898404a5fbf6ec92',
        'package/package.json':'7c3bc2f85ae6fe16a20eefa74569cdbdaf3eb319c8056ecf0b04b3e8607d5ea9'}

def main():
    OUT.mkdir(mode=0o700,exist_ok=False)
    deadline = time.monotonic()+210
    def command(args,limit=25):
        remaining = min(limit,deadline-time.monotonic())
        if remaining <= 0:
            raise TimeoutError('DEADLINE_NO_NEW_COMMAND')
        return subprocess.run(args,capture_output=True,timeout=remaining)
    def checked(args,limit=25):
        result = command(args,limit)
        if result.returncode:
            raise RuntimeError('COMMAND_EXIT_'+str(result.returncode))
        return result.stdout
    def save(name,data):
        (OUT/name).write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
    failure = None
    creation_requested = False
    created_id = None
    final = None
    raw_result = None
    exit_code = None
    image = None
    cleanup = 'NOT_CREATED'
    try:
        for name,digest in PINS.items():
            assert hashlib.sha256((RELEASE/name).read_bytes()).hexdigest() == digest
        graph = json.loads((RELEASE/'probe/workflow.OFF.json').read_bytes())
        assert graph['id'] == WORKFLOW_ID and graph['active'] is False
        assert not any(node.get('credentials') for node in graph['nodes'])
        assert all(node['type'].split('.')[-1] in ('manualTrigger','code','splitInBatches','if') for node in graph['nodes'])
        prod = json.loads(checked(['docker','inspect','n8n-n8n-1']))[0]
        assert prod['State']['Running'] is True
        image = prod['Image']
        assert image.startswith('sha256:') and len(image) == 71
        absent = command(['docker','container','inspect',NAME])
        assert absent.returncode != 0
        # These three commands use only this disposable SQLite tmpfs.
        script = 'n8n user-management:reset && n8n import:workflow --input /probe/workflow.OFF.json && exec n8n execute --id '+WORKFLOW_ID+' --rawOutput'
        env = {'DB_TYPE':'sqlite','DB_SQLITE_DATABASE':'/home/node/.n8n/database.sqlite',
               'N8N_USER_FOLDER':'/home/node','N8N_DEFAULT_BINARY_DATA_MODE':'filesystem',
               'N8N_BINARY_DATA_STORAGE_PATH':'/home/node/.n8n/binaryData',
               'N8N_RUNNERS_ENABLED':'true','N8N_RUNNERS_MODE':'internal',
               'N8N_RUNNERS_INSECURE_MODE':'false','N8N_RUNNERS_MAX_CONCURRENCY':'1',
               'N8N_RUNNERS_TASK_TIMEOUT':'60','NODE_FUNCTION_ALLOW_EXTERNAL':'@escalar/qr-native-r03',
               'N8N_DIAGNOSTICS_ENABLED':'false','N8N_VERSION_NOTIFICATIONS_ENABLED':'false',
               'N8N_PERSONALIZATION_ENABLED':'false','N8N_LICENSE_AUTO_RENEW_ENABLED':'false'}
        args = ['docker','create','--name',NAME,'--label','escalar.probe='+NAME,'--network','none','--read-only','--user','1000:1000',
                '--memory','768m','--cpus','1','--pids-limit','128','--cap-drop','ALL',
                '--security-opt','no-new-privileges:true',
                '--tmpfs','/home/node/.n8n:rw,uid=1000,gid=1000,mode=700',
                '--tmpfs','/tmp:rw,uid=1000,gid=1000,mode=700',
                '--mount','type=bind,src='+str(RELEASE/'probe')+',dst=/probe,readonly',
                '--mount','type=bind,src='+str(RELEASE/'package')+',dst=/usr/local/lib/node_modules/n8n/node_modules/@escalar/qr-native-r03,readonly']
        for key,value in env.items():
            args.extend(['--env',key+'='+value])
        args.extend(['--entrypoint','sh',image,'-c',script])
        creation_requested = True
        create_output = checked(args).decode().strip()
        assert len(create_output) == 64 and all(c in '0123456789abcdef' for c in create_output)
        created_id = create_output
        own = json.loads(checked(['docker','inspect',NAME]))[0]
        assert own['Image'] == image and own['Id'] == created_id
        assert own['HostConfig']['NetworkMode'] == 'none' and own['HostConfig']['ReadonlyRootfs'] is True
        assert own['HostConfig']['PortBindings'] in (None,{})
        binds = [m for m in own['Mounts'] if m['Type']=='bind']
        assert len(binds) == 2 and all(m['RW'] is False for m in binds)
        assert {m['Destination'] for m in binds} == {'/probe','/usr/local/lib/node_modules/n8n/node_modules/@escalar/qr-native-r03'}
        assert all(m['Type']=='bind' or (m['Type']=='tmpfs' and m['Destination'] in {'/home/node/.n8n','/tmp'}) for m in own['Mounts'])
        assert set(own['HostConfig']['Tmpfs']) == {'/home/node/.n8n','/tmp'}
        assert own['Config']['User'] == '1000:1000'
        save('preflight.json',{'image_id':image,'pins':PINS,'network':'none','mounts_readonly':True,
             'production_volumes':0,'production_environment_copied':False,'models':0})
        try:
            result = command(['docker','start','-a',NAME],170)
            stdout,stderr,exit_code = result.stdout,result.stderr,result.returncode
        except subprocess.TimeoutExpired as error:
            stdout,stderr = error.stdout or b'',error.stderr or b''
            exit_code = 'TRANSPORT_TIMEOUT_UNKNOWN'
        (OUT/'stdout.raw').write_bytes(stdout)
        (OUT/'stderr.raw').write_bytes(stderr)
        assert len(stdout) <= 32*1024*1024 and len(stderr) <= 4*1024*1024
        # CLI emits startup text followed by one IWorkflowExecutionData object.
        decoder = json.JSONDecoder()
        decoded = []
        text = stdout.decode('utf-8')
        for offset in (i for i,c in enumerate(text) if c=='{' and (i==0 or text[i-1]=='\n')):
            try:
                value,_ = decoder.raw_decode(text,offset)
            except ValueError:
                continue
            if isinstance(value,dict) and isinstance(value.get('data'),dict) and 'resultData' in value['data']:
                decoded.append(value)
        assert exit_code == 0 and len(decoded) == 1
        raw_result = decoded[0]
        data = raw_result['data']['resultData']
        assert not data.get('error')
        rows = data['runData']['Resultado de cinco casos']
        assert len(rows) == 1
        items = rows[0]['data']['main'][0]
        assert len(items) == 1
        final = items[0]['json']
        save('response.json',final)
        assert final['status'] == 'PASS' and final['expected_cases'] == 5
        assert final['workflow_id'] == WORKFLOW_ID and final['physical_storage_acceptance'] is True
        assert final['real_model_calls'] == final['real_pos_requests'] == final['data_table_requests'] == final['external_requests'] == 0
    except Exception as error:
        failure = {'type':type(error).__name__}
    finally:
        if created_id:
            own_check = subprocess.run(['docker','container','inspect',created_id],capture_output=True,timeout=5)
            if own_check.returncode == 0:
                owned = json.loads(own_check.stdout)[0]
                assert owned['Id'] == created_id and owned['Config']['Labels'].get('escalar.probe') == NAME and owned['Image'] == image
                # Remove only this newly created, labelled synthetic container.
                removed = subprocess.run(['docker','rm','-f',owned['Id']],capture_output=True,timeout=10)
                verify = subprocess.run(['docker','container','inspect',created_id],capture_output=True,timeout=5)
                cleanup = 'REMOVED_SYNTHETIC_CONTAINER_AND_TMPFS' if removed.returncode==0 and verify.returncode!=0 else 'CLEANUP_UNCERTAIN'
            else:
                cleanup = 'CONFIRMED_CREATED_CONTAINER_ALREADY_ABSENT'
        elif creation_requested:
            cleanup = 'CREATE_OUTCOME_UNCERTAIN_NO_DELETE_BY_NAME'
        report = {'state':'NATIVE_CJS_FIVE_CASES_PASS' if final and final.get('status')=='PASS' and not failure and cleanup in ('REMOVED_SYNTHETIC_CONTAINER_AND_TMPFS','CONFIRMED_CREATED_CONTAINER_ALREADY_ABSENT') else 'FAIL_PRESERVED_NO_RETRY',
                  'failure':failure,'exit_code':exit_code,'cleanup':cleanup,'image_id':image,
                  'response_saved':final is not None,'production_changes':0,'models':0,'network':'none'}
        save('result.json',report)
        print(json.dumps(report))

if __name__ == '__main__':
    main()
