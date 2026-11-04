"""VPS-only, immutable two-file addition to the existing n8n installation.

No environment, compose, existing files, permissions, network or service restart
is changed. The native component probe is a separate operation.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import time

BASE = Path('/opt/escalar-qr/release/tools/qr-native-r02')
TARGET = '/usr/local/lib/node_modules/n8n/node_modules/@escalar/qr-native-r02'
CONTAINER = 'n8n-n8n-1'
INDEX_SHA = '935793891656e1d2c6b8afa0a73b7b3e7170cbed6bd79e2ccd32fd72b4954d00'

def main():
    deadline = time.monotonic() + 45
    def command(args):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('INSTALL_DEADLINE_EXHAUSTED_NO_NEW_COMMAND')
        result = subprocess.run(args, capture_output=True, timeout=remaining)
        if result.returncode:
            raise RuntimeError('COMMAND_FAILED_' + str(result.returncode))
        return result.stdout
    files = {name: (BASE/'package'/name).read_bytes() for name in ('index.cjs','package.json')}
    assert hashlib.sha256(files['index.cjs']).hexdigest() == INDEX_SHA
    meta = json.loads(files['package.json'])
    assert meta == {'name':'@escalar/qr-native-r02','version':'0.0.0-poc-r02',
                    'private':True,'type':'commonjs','main':'index.cjs'}
    inspect = json.loads(command(['docker','inspect',CONTAINER]))[0]
    assert inspect['State']['Running'] is True
    image = inspect['Config']['Image']
    assert image == 'docker.n8n.io/n8nio/n8n' or image.startswith('docker.n8n.io/n8nio/n8n:')
    env = dict(item.split('=',1) for item in inspect['Config']['Env'] if '=' in item)
    assert env.get('N8N_RUNNERS_ENABLED') == 'true'
    assert env.get('NODE_FUNCTION_ALLOW_EXTERNAL') == '*'
    assert env.get('N8N_RUNNERS_INSECURE_MODE','false') in ('false','')
    # An existing target always stops the operation; no overwrite or cache reuse.
    check = "const fs=require('fs');const p="+json.dumps(TARGET)+";if(fs.existsSync(p))process.exit(20);console.log('NEW_PATH_CONFIRMED')"
    assert command(['docker','exec','-u','0',CONTAINER,'node','-e',check]).strip() == b'NEW_PATH_CONFIRMED'
    command(['docker','exec','-u','0',CONTAINER,'mkdir','-p','/usr/local/lib/node_modules/n8n/node_modules/@escalar'])
    command(['docker','exec','-u','0',CONTAINER,'mkdir',TARGET])
    for name in files:
        command(['docker','cp',str(BASE/'package'/name),CONTAINER+':'+TARGET+'/'+name])
    command(['docker','exec','-u','0',CONTAINER,'chmod','755',TARGET])
    for name in files:
        command(['docker','exec','-u','0',CONTAINER,'chmod','444',TARGET+'/'+name])
    verify = "const fs=require('fs'),c=require('crypto'),p="+json.dumps(TARGET)+";const out={};for(const n of ['index.cjs','package.json']){const b=fs.readFileSync(p+'/'+n),s=fs.statSync(p+'/'+n);if(s.uid!==0||(s.mode&511)!==292)process.exit(21);out[n]=c.createHash('sha256').update(b).digest('hex');} console.log(JSON.stringify(out));"
    actual = json.loads(command(['docker','exec','-u','node',CONTAINER,'node','-e',verify]))
    expected = {name: hashlib.sha256(raw).hexdigest() for name,raw in files.items()}
    assert actual == expected
    result = {'state':'IMMUTABLE_MODULE_FILES_INSTALLED_NATIVE_PROOF_PENDING',
              'target':TARGET,'files':actual,'service_restarts':0,'environment_changes':0,
              'existing_files_overwritten':0,'models':0,'api_requests':0,
              'persistent_after_container_recreation':False}
    print(json.dumps(result))

if __name__ == '__main__':
    main()
