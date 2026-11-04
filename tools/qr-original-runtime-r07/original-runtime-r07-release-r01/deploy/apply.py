"""Future one-shot VPS apply. Requires exact prepared-plan SHA and ROOT flag.

This file is a proposal, not authorization to run it. It never activates n8n or
imports credentials. A failure retains evidence and attempts configuration-only
reversal; old SQLite memory cannot be restored or replayed by container restart.
"""
import argparse
import json
import os
import stat
import time
from deploy_common import *

LABEL = ['--label', 'escalar.qr.original.r07=' + MANIFEST_SHA]


def verify_image_api(image, manifest):
    # An isolated, bounded stdlib file-hash process only. No app import/listener,
    # binding, credential, database, model or external network is available.
    checker = """import hashlib,json,pathlib
p=pathlib.Path('/sandbox/api/app')
out=[{'file':'api/'+x.relative_to(p.parent).as_posix(),'sha256':hashlib.sha256(x.read_bytes()).hexdigest()} for x in sorted(p.rglob('*')) if x.is_file()]
r=pathlib.Path('/sandbox/requirements.txt')
out.append({'file':'requirements.txt','sha256':hashlib.sha256(r.read_bytes()).hexdigest()})
print(json.dumps(out))
"""
    result = json.loads(command(['docker', 'run', '--rm', '--network', 'none', '--read-only',
        '--user', '10001:10001', '--memory', '128m', '--cpus', '1', '--pids-limit', '32',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--entrypoint', 'python', image, '-c', checker], timeout=30))
    expected = {x['file']: x['sha256'] for x in manifest['api_files']}
    need(len(result) == 70 and {x['file']: x['sha256'] for x in result} == expected,
         'ACTUAL_IMAGE_API70_MISMATCH')


def launch(kind, image_id):
    args = ['docker', 'run', '-d', '--name', kind, '--network', INTERNAL,
            '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--restart', 'no', *LABEL]
    if kind == BACKEND:
        # Preserve the exact original argv, adding only an explicit restrictive
        # creation umask before exec; reviewed wrapper/helper bytes stay intact.
        starter = 'import os,sys;os.umask(0o077);os.execv(sys.executable,[sys.executable]+' + repr(ENTRY[1:]) + ')'
        args += ['--memory', '512m', '--cpus', '1', '--pids-limit', '128',
                 '--mount', 'type=bind,src=' + str(RUN) + ',dst=/run/eval',
                 '--mount', 'type=bind,src=' + str(PACK / 'sandbox.py') + ',dst=/sandbox/api-sandbox.py,readonly',
                 '--mount', 'type=bind,src=' + str(PACK / 'notifier_fixture.py') + ',dst=/sandbox/notifier_fixture.py,readonly',
                 '--mount', 'type=bind,src=' + str(PACK / 'control_router.py') + ',dst=/sandbox/control_router.py,readonly',
                 '--mount', 'type=bind,src=' + str(PACK / 'visual_snapshot.py') + ',dst=/sandbox/visual_snapshot.py,readonly',
                 '--mount', 'type=bind,src=' + str(PACK / 'originals') + ',dst=/sandbox/originals,readonly',
                 '--mount', 'type=bind,src=' + str(PACK / 'security') + ',dst=/sandbox/security,readonly',
                 '--entrypoint', 'python', IMAGES[kind], '-c', starter]
    else:
        args += ['--memory', '128m', '--pids-limit', '32', '--read-only', '--init',
                 '--mount', 'type=bind,src=' + str(PACK / 'proxy.py') + ',dst=/proxy/fixed-api-proxy.py,readonly',
                 IMAGES[kind]]
    cid = command(args).decode().strip()
    need(re.fullmatch('[0-9a-f]{64}', cid), 'CREATED_CONTAINER_ID')
    private_json(PRIVATE / ('created-' + kind + '.json'),
                 {'container_id': cid, 'image_id': image_id})
    obj = identity(kind, cid)
    need(obj['Image'] == image_id and obj['State']['Running'], 'CREATED_CONTAINER_IMAGE_OR_STATE')
    if kind == PROXY:
        command(['docker', 'network', 'connect', '--alias', 'impulsa-qr-api-eval', N8N, cid])
    return cid


def binding_ready():
    path = RUN / 'binding.json'
    until = time.monotonic() + 20
    previous = None
    while time.monotonic() < until:
        if path.exists():
            st = path.stat()
            current = (st.st_size, st.st_mtime_ns)
            if current == previous and st.st_size > 0:
                break
            previous = current
        time.sleep(0.25)  # File completion observation only; no action/API retry.
    else:
        raise RuntimeError('BINDING_CREATION_TIMEOUT')
    raw = regular(path, 1024 * 1024)
    for target, mode in ((RUN, 0o700), (path, 0o600)):
        st = target.stat()
        need(st.st_uid == 10001 and st.st_gid == 10001 and stat.S_IMODE(st.st_mode) == mode,
             'PRIVATE_BINDING_OWNER_OR_MODE')
    b = json.loads(raw)
    need(b.get('synthetic_only') is True and b.get('database') == 'sqlite_memory'
         and b.get('api_version') == '1.1.1' and b.get('notifier_scope') == SCOPE
         and isinstance(b.get('notifier_token'), str) and b['notifier_token'].startswith('smoke-notice-2.'),
         'PRIVATE_BINDING_SCOPE')
    need(b.get('base_url') == 'http://impulsa-qr-api-eval:8769' and b.get('container_isolation') is True,
         'PRIVATE_BINDING_ORIGIN')


def verify_mounts(obj, kind):
    expected = {('/run/eval', str(RUN), True),
                ('/sandbox/api-sandbox.py', str(PACK / 'sandbox.py'), False),
                ('/sandbox/notifier_fixture.py', str(PACK / 'notifier_fixture.py'), False),
                ('/sandbox/control_router.py', str(PACK / 'control_router.py'), False),
                ('/sandbox/visual_snapshot.py', str(PACK / 'visual_snapshot.py'), False),
                ('/sandbox/originals', str(PACK / 'originals'), False),
                ('/sandbox/security', str(PACK / 'security'), False)} if kind == BACKEND else {
                ('/proxy/fixed-api-proxy.py', str(PACK / 'proxy.py'), False)}
    need(all(m['Type'] == 'bind' for m in obj['Mounts'])
         and {(m['Destination'], m['Source'], m['RW']) for m in obj['Mounts']} == expected,
         'EXACT_R07_SOURCE_AND_BINDING_MOUNTS')


def apply(plan):
    manifest = verify_pack()
    old = old_configuration(plan)
    existing_networks(old)
    need(not RUN.exists() and not set(PRESERVED.values()) & names(), 'ONE_SHOT_APPLY_PATHS')
    for kind in old:
        current = identity(kind, old[kind]['Id'])
        need(current['State']['Running'] and not current['State']['Paused'], 'OLD_FIXTURE_STATE_CHANGED')
        for key in ('Config', 'HostConfig', 'Mounts', 'Image', 'NetworkSettings'):
            need(current[key] == old[kind][key], 'OLD_FIXTURE_CONFIG_CHANGED')
    need(sha(regular(OLD_RUN / 'binding.json')) == plan['old_binding_sha256'], 'OLD_BINDING_CHANGED')
    base_image()
    private_json(PRIVATE / 'apply-started.json', {'state': 'ONE_SHOT_STARTED', 'plan_source': MANIFEST_SHA})
    verify_image_api(BASE_ID, manifest)
    built = {}
    existing_images = set(command(['docker', 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}}']).decode().splitlines())
    need(not set(IMAGES.values()) & existing_images, 'R07_IMAGE_TAG_ALREADY_EXISTS')
    for kind, dockerfile in ((BACKEND, 'Dockerfile'), (PROXY, 'Dockerfile.proxy')):
        base_image()
        verify_pack()
        # Legacy local-image lookup avoids a BuildKit registry metadata lookup.
        # If this daemon no longer supports it, abort before touching R05.
        build_env = {**os.environ, 'DOCKER_BUILDKIT': '0'}
        command(['docker', 'build', '--pull=false', '--network=none', '--file', str(PACK / dockerfile),
                 '--tag', IMAGES[kind], str(PACK)], timeout=180, env=build_env)
        base_image()
        built[kind] = inspect(IMAGES[kind])['Id']
        verify_image_api(built[kind], manifest)
    private_json(PRIVATE / 'built-images.json', built)
    # Recheck closure evidence and frozen inputs after builds, before stopping
    # any existing process. ROOT must keep the separately closed workflows OFF.
    closure_gate(plan['closure_gate_path'], plan['closure_gate_sha256'])
    existing_networks(old)
    verify_pack()
    need(sha(regular(OLD_RUN / 'binding.json')) == plan['old_binding_sha256'], 'OLD_BINDING_CHANGED')
    for kind in (PROXY, BACKEND):
        current = identity(kind, old[kind]['Id'])
        command(['docker', 'stop', '--time', '10', current['Id']], timeout=20)
        obj = identity(kind, old[kind]['Id'])
        need(obj['State']['Running'] is False, 'R05_STOP_NOT_CONFIRMED')
        command(['docker', 'rename', obj['Id'], PRESERVED[kind]])
        disconnect_all(identity(PRESERVED[kind], old[kind]['Id']))
    RUN.mkdir(mode=0o700)
    os.chown(RUN, 10001, 10001)
    os.chmod(RUN, 0o700)
    ids = {BACKEND: launch(BACKEND, built[BACKEND])}
    binding_ready()  # No proxy/credential consumer starts before this passes.
    health = "import json,urllib.request;r=urllib.request.urlopen('http://127.0.0.1:8769/api/v1/health',timeout=5);b=json.load(r);assert r.status==200 and b['version']=='1.1.1';print(json.dumps({'health':200,'version':b['version']}))"
    proof = json.loads(command(['docker', 'exec', ids[BACKEND], 'python', '-c', health], timeout=10))
    ids[PROXY] = launch(PROXY, built[PROXY])
    for kind, cid in ids.items():
        obj = identity(kind, cid)
        security(obj, kind)
        verify_mounts(obj, kind)
        # Preserve daemon-dependent resource/security defaults as well as the
        # explicit limits. Bind mounts, entrypoint and topology are checked
        # separately because they are the reviewed R07 changes.
        for key in ('MemorySwap', 'MemoryReservation', 'MemorySwappiness', 'OomKillDisable',
                    'OomScoreAdj', 'ShmSize', 'Ulimits', 'CgroupnsMode', 'CgroupParent',
                    'Runtime', 'Isolation', 'LogConfig', 'MaskedPaths', 'ReadonlyPaths'):
            need(obj['HostConfig'].get(key) == old[kind]['HostConfig'].get(key),
                 'RESOURCE_SECURITY_DEFAULT_CHANGED')
        need(identity(PRESERVED[kind], old[kind]['Id'])['State']['Running'] is False,
             'PRESERVED_R05_NOT_STOPPED')
    binding_ready()
    verify_pack()
    need(sha(regular(OLD_RUN / 'binding.json')) == plan['old_binding_sha256'], 'OLD_BINDING_CHANGED')
    result = {'state': 'R07_ISOLATED_FRESH_FIXTURE_ONLY_READY_IMPORT_AND_CONTROL_PENDING',
              'container_ids': ids, 'image_ids': built, 'sourcepack_manifest_sha256': MANIFEST_SHA,
              'local_results_sha256': RESULTS_SHA, 'r12_case_archived': True,
              'archive_gate_sha256': plan['closure_gate_sha256'],
              'model_block_closure_required': False,
              'api_version': proof['version'], 'health': proof['health'], 'host_ports': [],
              'binding_permissions_verified': True, 'binding_uid': 10001, 'binding_gid': 10001,
              'binding_mode': '0600', 'binding_parent_mode': '0700',
              'preserved_r05_containers_stopped': PRESERVED, 'old_binding_exact': True,
              'old_fixture_memory_state_restorable': False, 'old_case_replayed': False,
              'n8n_changes': 0, 'credentials_imported': 0, 'models': 0,
              'notice_operation_invocations': 0, 'production_changes': 0, 'native_notifier_acceptance': False}
    private_json(PRIVATE / 'apply-result.json',
                 {**result, 'private_binding_sha256': sha(regular(RUN / 'binding.json'))})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--execute-reviewed-r07', action='store_true', required=True)
    args = parser.parse_args()
    root_only()
    plan = read_plan(args.plan, args.plan_sha256)
    need(not (PRIVATE / 'apply-started.json').exists(), 'NO_APPLY_RETRY')
    try:
        result = apply(plan)
    except Exception:
        # One bounded reversal attempt, no retry loop or old process restart.
        try:
            reversal = restore_stopped(plan)
            private_json(PRIVATE / 'apply-failure-reversal.json', reversal)
        except Exception as error:
            private_json(PRIVATE / 'apply-failure-reversal-blocked.json',
                         {'state': 'MANUAL_ROOT_REVIEW_REQUIRED', 'error_type': type(error).__name__,
                          'automatic_retry': False})
        raise
    print(json.dumps(result))


if __name__ == '__main__':
    safe_main(main)
