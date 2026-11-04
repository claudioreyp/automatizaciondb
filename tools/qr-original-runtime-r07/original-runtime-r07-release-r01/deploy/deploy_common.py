"""Source-only R07 deployment proposal; inert on import. Linux ROOT execution only.

The separate prepare/apply/reverse CLIs are never called by this module.
No API, n8n, provider or production credential client is implemented here.
"""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from pathlib import PureWindowsPath
import subprocess

BASE = Path('/opt/escalar-qr')
PACK = BASE / 'original-runtime-r07-release-r01/runtime'
PRIVATE = BASE / 'private/original-r07-deploy-r01'
RUN = BASE / 'eval-run-r07'
OLD_RUN = BASE / 'eval-run-r05'
INTERNAL = 'impulsa-qr-eval-internal'
N8N = 'n8n_default'
BACKEND = 'api-eval-backend'
PROXY = 'impulsa-qr-api-proxy'
PRESERVED = {BACKEND: BACKEND + '-r05-r12-archived', PROXY: PROXY + '-r05-r12-archived'}
BASE_TAG = 'escalar-qr-api-eval:16240c5'
BASE_ID = 'sha256:93dc84393cbaa83f680830c2e53bc7567e1c73a5273f46a36bb72df46fa9f740'
MANIFEST_SHA = 'ff514ad7767879624c5ca35c829efda3b5984707aeb9fafe478f7cbf374bfc7e'
RESULTS_SHA = '9894ae4158ec41a1a4e5e6c76a216f1a0da73404716216969e5cb2d5973afd53'
ARCHIVE_INPUTS_SHA = '9828a0014628a3130e4fa6621fd5bc849ac33dc1ad7ee8f23ce0ec8e1f2ec1ae'
REVISION = '7f840536c16aea143c9d71264bd83866ee4731b7'
IMAGES = {BACKEND: 'escalar-qr-api-eval:original-r07-ff514ad7',
          PROXY: 'escalar-qr-api-proxy:original-r07-ff514ad7'}
ENTRY = ['python', '/sandbox/api-sandbox.py', '--serve', '--listen-host', '0.0.0.0',
         '--container-isolation', '--advertise-host', 'impulsa-qr-api-eval',
         '--port', '8769', '--binding-file', '/run/eval/binding.json']
SCOPE = {'business_id': 2, 'branch_id': 2, 'profile': 'generic',
         'scopes': ['events:deliver', 'events:read', 'menu:read', 'orders:read']}


def need(condition, code):
    if not condition:
        raise RuntimeError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def root_only():
    global _LOCK_FD
    need(os.name == 'posix' and os.geteuid() == 0, 'LINUX_ROOT_ONLY')
    os.umask(0o077)
    for path in (BASE, BASE / 'private'):
        no_symlinks(path)
        st = path.stat()
        need(stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and not stat.S_IMODE(st.st_mode) & 0o022,
             'ROOT_OWNED_STORAGE_REQUIRED')
    lock_path = BASE / 'private/notifier-r06-deployment.lock'
    no_symlinks(lock_path)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    st = os.fstat(fd)
    need(stat.S_ISREG(st.st_mode) and st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o600,
         'LOCK_FILE_OWNER_MODE')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError('ANOTHER_PREPARE_APPLY_OR_REVERSAL_IS_RUNNING')
    _LOCK_FD = fd  # Held for this CLI process lifetime; never unlink a live lock.
    need(not os.environ.get('DOCKER_HOST') and not os.environ.get('DOCKER_CONTEXT'), 'NO_REMOTE_DOCKER_OVERRIDE')
    context = docker_json('context', 'inspect')
    need(len(context) == 1 and context[0]['Endpoints']['docker']['Host'] == 'unix:///var/run/docker.sock',
         'LOCAL_DOCKER_SOCKET_ONLY')


def no_symlinks(path):
    path = Path(path)
    need(path.is_absolute() and '..' not in path.parts, 'ABSOLUTE_NORMAL_PATH_REQUIRED')
    for current in (path, *path.parents):
        if current.exists() or current.is_symlink():
            need(not current.is_symlink(), 'SYMLINK_REJECTED')
    return path


def regular(path, maximum=64 * 1024 * 1024):
    path = no_symlinks(path)
    st = path.stat()
    need(stat.S_ISREG(st.st_mode) and st.st_size <= maximum, 'REGULAR_BOUNDED_FILE_REQUIRED')
    return path.read_bytes()


def private_json(path, value):
    raw = (json.dumps(value, indent=2, ensure_ascii=True) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return sha(raw)


def private_bytes(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def command(args, timeout=25, env=None):
    # Capture all child output. Failure messages never include a child response,
    # environment, binding contents, command payload or private token.
    done = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    need(done.returncode == 0, 'SUBPROCESS_FAILED_' + args[0])
    need(len(done.stdout) <= 8 * 1024 * 1024 and len(done.stderr) <= 8 * 1024 * 1024,
         'SUBPROCESS_OUTPUT_BOUND')
    return done.stdout


def docker_json(*args):
    return json.loads(command(['docker', *args]))


def inspect(name):
    result = docker_json('inspect', name)
    need(isinstance(result, list) and len(result) == 1, 'EXACT_SINGLE_DOCKER_OBJECT_REQUIRED')
    return result[0]


def names():
    return set(command(['docker', 'ps', '-a', '--format', '{{.Names}}']).decode().splitlines())


def verify_pack():
    no_symlinks(PACK)
    for path in (PACK, *PACK.rglob('*')):
        no_symlinks(path)
        st = path.stat()
        need((stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))
             and st.st_uid == 0 and not stat.S_IMODE(st.st_mode) & 0o022, 'SOURCE_OWNER_MODE')
        if stat.S_ISDIR(st.st_mode):
            need(stat.S_IMODE(st.st_mode)==0o755,'SOURCE_DIRECTORY_0755_FOR_UID10001')
    mraw = regular(PACK / 'sourcepack-manifest.json')
    need(sha(mraw) == MANIFEST_SHA, 'FROZEN_MANIFEST_SHA')
    manifest = json.loads(mraw)
    need(len(manifest['files']) == 106 and manifest['api_blobs_exact'] == 70
         and manifest['api_revision'] == REVISION and manifest['fixed_original_contracts'] == 15
         and manifest['visual_registered_cases'] == 27, 'FROZEN_SOURCE_COUNTS_REVISION')
    expected = {'sourcepack-manifest.json', 'SOURCE-RESULTS.json'}
    for item in manifest['files']:
        rel = item['file']
        need(isinstance(rel,str) and not Path(rel).is_absolute() and '..' not in Path(rel).parts, 'SOURCE_RELATIVE_PATH')
        raw = regular(PACK / rel)
        need(sha(raw) == item['sha256'] and len(raw) == item['bytes'], 'FROZEN_SOURCE_BYTES')
        expected.add(rel)
    actual = {p.relative_to(PACK).as_posix() for p in PACK.rglob('*') if p.is_file()}
    need(actual == expected, 'SOURCEPACK_UNREVIEWED_MEMBER')
    need(sha(regular(PACK / 'SOURCE-RESULTS.json')) == RESULTS_SHA, 'LOCAL_RESULTS_PIN')
    results = load(PACK / 'SOURCE-RESULTS.json')
    need(results['status'] == 'PASS_LOCAL_ONLY_NOT_DEPLOYED'
         and results['source_manifest_sha256'] == MANIFEST_SHA
         and results['native_original_acceptance'] == 0, 'LOCAL_RESULTS_SCOPE')
    commit = load(PACK / 'provenance/api70-commit-check.json')
    need(commit['revision'] == REVISION and commit['result'] == 'PASS'
         and len(commit['files']) == 70, 'API70_PROVENANCE')
    files = {item['file']:item for item in manifest['files']}
    for item in commit['files']:
        need(item['api_commit_matches'] is True and files[item['file']]['sha256'] == item['sha256'], 'API70_COMMIT_PIN')
    need(b'server=HTTPServer(' in regular(PACK / 'sandbox.py')
         and b'ThreadingHTTPServer' not in regular(PACK / 'sandbox.py'), 'SERIAL_BACKEND_REQUIRED')
    for filename in ('sandbox.py','notifier_fixture.py','proxy.py','control_router.py','visual_snapshot.py',
                     'originals/original-fixture.py','originals/control-hook.py','originals/manifest.json',
                     'security/security-fixture.py','security/control-hook.py','security/manifest.json'):
        need(stat.S_IMODE((PACK / filename).stat().st_mode) == 0o644, 'RO_SOURCE_0644_FOR_UID10001')
    # The image checker consumes the unchanged API70 provenance records.
    return {**manifest, 'api_files':commit['files']}


def closure_gate(path, digest, phase='r12'):
    path = no_symlinks(path)
    need(path.is_relative_to(BASE / 'private') and re.fullmatch('[0-9a-f]{64}',digest), 'PRIVATE_ARCHIVE_PATH_SHA')
    raw = regular(path)
    need(sha(raw) == digest, 'REVIEWED_ARCHIVE_SHA')
    gate = json.loads(raw)
    need(phase in ('r12','r07') and gate['schema'] == 'escalarai.original-r07-archived-case-gate.v1'
         and gate['state'] == ('R12_CASE_ARCHIVED' if phase == 'r12' else 'R07_CASE_ARCHIVED')
         and gate['snapshot_scope'] == 'complete_current_synthetic_consumer_case_and_journal'
         and gate.get('database_backup') is False, 'CURRENT_CASE_ARCHIVE_SCOPE')
    for key in ('current_case_settled','all_14_workflows_off','current_case_models_settled',
                'no_active_coupon','no_runtime_change_in_progress','fresh_synthetic_phase_only'):
        need(gate.get(key) is True, 'CURRENT_CASE_ARCHIVE_' + key)
    # R12's financial block may remain OPEN. No settlement, CAS, refund,
    # budget/history reset or requirement to close that block is introduced.
    need(gate.get('model_block_closure_required') is False, 'NO_MODEL_BLOCK_CLOSURE_REQUIRED')
    if phase == 'r07':
        need(gate.get('notifier_and_control_consumers_off') is True, 'R07_CONSUMERS_MUST_BE_OFF')
    need(gate.get('archive_location') == 'user_workspace'
         and gate.get('workspace_root') == r'C:\codex\apps\Impulsa\APP POS'
         and gate.get('local_artifacts_verified_by_root') is True, 'ROOT_VERIFIED_LOCAL_ARCHIVE_RECEIPT')
    need(isinstance(gate.get('artifacts'),list) and len(gate['artifacts']) == 4, 'FOUR_COMPLETE_LOCAL_ARTIFACTS')
    roles = set()
    for item in gate['artifacts']:
        source = PureWindowsPath(item['path'])
        need(source.is_absolute() and source.is_relative_to(PureWindowsPath(gate['workspace_root']))
             and '..' not in source.parts and item['role'] in {'snapshot','journal','recovery','budget'}
             and isinstance(item['bytes'],int) and not isinstance(item['bytes'],bool) and 0 < item['bytes'] <= 64*1024*1024
             and re.fullmatch('[0-9a-f]{64}',item['sha256']), 'EXACT_LOCAL_ARTIFACT_METADATA')
        need(item['role'] not in roles, 'UNIQUE_LOCAL_ARTIFACT_ROLE')
        roles.add(item['role'])
    need(roles == {'snapshot','journal','recovery','budget'}, 'COMPLETE_LOCAL_ARCHIVE_ROLES')
    if phase == 'r12':
        references = regular(Path(__file__).parent / 'archive-inputs.json')
        need(sha(references) == ARCHIVE_INPUTS_SHA, 'ARCHIVE_REFERENCE_SPEC_PIN')
        expected = json.loads(references)['gate_receipt']['artifacts']
        need(gate['artifacts'] == expected and gate.get('budget_head') == 48
             and gate.get('budget_totals') == {'posts':3931,'tokens':77701710}, 'EXACT_ARCHIVED_R12_CASE_REFERENCES')
    # VPS validates this ROOT-generated receipt. Complete payloads stay at the
    # pinned absolute user-workspace paths; none is opened/copied on the VPS.
    return gate


def security(obj, kind):
    h, c = obj['HostConfig'], obj['Config']
    need(c['User'] == '10001:10001' and not h['Privileged'] and not h.get('CapAdd')
         and h['CapDrop'] == ['ALL'] and h['SecurityOpt'] == ['no-new-privileges:true'], 'SECURITY_ID_CAPS')
    need(not h.get('PortBindings') and not h.get('PublishAllPorts') and not h.get('Devices')
         and not h.get('DeviceRequests') and not h.get('VolumesFrom') and not h.get('Links'), 'PORT_DEVICE_MOUNT_POLICY')
    need(h['RestartPolicy']['Name'] in ('', 'no') and h['RestartPolicy']['MaximumRetryCount'] == 0,
         'NO_AUTOMATIC_CONTAINER_RESTART')
    need(not h.get('Tmpfs') and not h.get('ExtraHosts') and not h.get('Dns')
         and not h.get('DnsSearch') and not h.get('DnsOptions'), 'UNREVIEWED_HOSTCONFIG')
    need(h.get('PidMode', '') == '' and h.get('IpcMode') == 'private'
         and h.get('UTSMode', '') == '' and h.get('UsernsMode', '') == '', 'NAMESPACE_POLICY')
    need(not h.get('CpuShares') and not h.get('CpuPeriod') and not h.get('CpuQuota')
         and not h.get('CpusetCpus') and not h.get('CpusetMems'), 'CPU_POLICY')
    if kind == BACKEND:
        need(h['Memory'] == 512 * 1024 * 1024 and h['NanoCpus'] == 1000000000
             and h['PidsLimit'] == 128 and h['ReadonlyRootfs'] is False and h.get('Init') in (None, False),
             'BACKEND_RESOURCE_PROFILE')
        need(set(obj['NetworkSettings']['Networks']) == {INTERNAL}, 'BACKEND_INTERNAL_ONLY')
    else:
        need(h['Memory'] == 128 * 1024 * 1024 and h['NanoCpus'] == 0
             and h['PidsLimit'] == 32 and h['ReadonlyRootfs'] is True and h.get('Init') is True,
             'PROXY_RESOURCE_PROFILE')
        need(set(obj['NetworkSettings']['Networks']) == {INTERNAL, N8N}, 'PROXY_EXACT_NETWORKS')


def base_image():
    obj = inspect(BASE_TAG)
    need(obj['Id'] == BASE_ID and obj['Config']['User'] == '10001:10001'
         and obj['Config']['Entrypoint'] == ENTRY and obj['Config'].get('Cmd') is None, 'EXACT_BASE_IMAGE_REQUIRED')
    return obj


def source_script_pins():
    return {p.name: sha(p.read_bytes()) for p in sorted(Path(__file__).parent.glob('*.py'))}


def read_plan(path, digest):
    need(Path(path) == PRIVATE / 'plan.json' and re.fullmatch('[0-9a-f]{64}', digest), 'FIXED_PLAN_PATH_SHA')
    raw = regular(path)
    for target, mode in ((PRIVATE, 0o700), (Path(path), 0o600)):
        st = target.stat()
        need(st.st_uid == 0 and stat.S_IMODE(st.st_mode) == mode, 'PRIVATE_PLAN_OWNER_MODE')
    need(sha(raw) == digest, 'REVIEWED_PLAN_SHA')
    plan = json.loads(raw)
    need(plan['schema'] == 'escalarai.original-r07-deployment-plan.v1'
         and plan['state'] == 'PREPARED_NOT_APPLIED'
         and plan['sourcepack_manifest_sha256'] == MANIFEST_SHA
         and plan['proposal_script_pins'] == source_script_pins(), 'PLAN_SOURCE_BINDING')
    verify_pack()
    closure_gate(plan['closure_gate_path'], plan['closure_gate_sha256'])
    for item in plan['archive']:
        path = no_symlinks(PRIVATE / item['archive_file'])
        need(path.is_relative_to(PRIVATE), 'ARCHIVE_PATH')
        raw = regular(path)
        need(sha(raw) == item['sha256'] and len(raw) == item['bytes'], 'ARCHIVE_CHANGED')
        st = path.stat()
        need(st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o600, 'ARCHIVE_OWNER_MODE')
    return plan


def identity(name, expected_id):
    obj = inspect(name)
    need(obj['Id'] == expected_id, 'CONTAINER_IDENTITY_CHANGED')
    return obj


def old_configuration(plan):
    old = load(PRIVATE / 'r05-containers.private.json')
    need(set(old) == {BACKEND, PROXY}, 'ARCHIVED_CONTAINER_SET')
    for kind in old:
        need(old[kind]['Id'] == plan['old_container_ids'][kind], 'ARCHIVED_CONTAINER_ID')
    return old


def existing_networks(old):
    for name in (INTERNAL, N8N):
        net = inspect(name)
        need(net['Internal'] is (name == INTERNAL), 'EXISTING_NETWORK_INTERNAL_FLAG')
        for kind, obj in old.items():
            if name in obj['NetworkSettings']['Networks']:
                need(obj['NetworkSettings']['Networks'][name]['NetworkID'] == net['Id'],
                     'EXISTING_NETWORK_ID_CHANGED')


def disconnect_all(obj):
    allowed = {INTERNAL} if obj['Name'].lstrip('/').startswith(BACKEND) else {INTERNAL, N8N}
    need(set(obj['NetworkSettings']['Networks']) <= allowed, 'UNREVIEWED_NETWORK_DETACH')
    for network in sorted(obj['NetworkSettings']['Networks']):
        command(['docker', 'network', 'disconnect', network, obj['Id']])


def restore_stopped(plan):
    """Configuration reversal only. Never restart old in-memory processes."""
    old = old_configuration(plan)
    for kind in (PROXY, BACKEND):
        existing = names()
        if kind in existing:
            obj = inspect(kind)
            if obj['Id'] != old[kind]['Id']:
                built = load(PRIVATE / 'built-images.json')
                created = load(PRIVATE / ('created-' + kind + '.json'))
                need(obj['Config'].get('Labels', {}).get('escalar.qr.original.r07') == MANIFEST_SHA
                     and obj['Config']['Image'] == IMAGES[kind] and obj['Image'] == built[kind]
                     and obj['Id'] == created['container_id'] and built[kind] == created['image_id'],
                     'REFUSE_UNKNOWN_CURRENT_CONTAINER')
                failed_name = kind + '-r07-reversed'
                need(failed_name not in existing, 'REVERSED_CONTAINER_ALREADY_EXISTS')
                if obj['State']['Running']:
                    command(['docker', 'stop', '--time', '10', obj['Id']], timeout=20)
                obj = identity(kind, obj['Id'])
                need(obj['State']['Running'] is False, 'R07_STOP_NOT_CONFIRMED')
                disconnect_all(obj)
                command(['docker', 'rename', obj['Id'], failed_name])
        if PRESERVED[kind] in names():
            obj = identity(PRESERVED[kind], old[kind]['Id'])
            need(obj['State']['Running'] is False and kind not in names(), 'PRESERVED_OLD_CONTAINER_STATE')
            command(['docker', 'rename', obj['Id'], kind])
        obj = identity(kind, old[kind]['Id'])
        # The no-restart rule applies even when a preparation/build failed before
        # cutover: in that case the untouched old process may still be running.
        if obj['State']['Running']:
            need(obj['Config'] == old[kind]['Config'] and obj['HostConfig'] == old[kind]['HostConfig'],
                 'UNTOUCHED_OLD_CONTAINER_CHANGED')
            continue
        for network, endpoint in old[kind]['NetworkSettings']['Networks'].items():
            if network not in obj['NetworkSettings']['Networks']:
                args = ['docker', 'network', 'connect']
                for alias in endpoint.get('Aliases') or []:
                    args.extend(['--alias', alias])
                args.extend([network, obj['Id']])
                command(args)
        obj = identity(kind, old[kind]['Id'])
        need(obj['State']['Running'] is False
             and set(obj['NetworkSettings']['Networks']) == set(old[kind]['NetworkSettings']['Networks']),
             'OLD_CONFIGURATION_RESTORATION_NOT_CONFIRMED')
    need(sha(regular(OLD_RUN / 'binding.json')) == plan['old_binding_sha256'], 'OLD_BINDING_CHANGED')
    return {'state': 'R05_CONFIGURATION_PRESERVED_NO_RESTART',
            'old_fixture_memory_state_restored': False,
            'old_case_replayed': False, 'controller_or_credentials_changed': False,
            'old_running': {kind: identity(kind, old[kind]['Id'])['State']['Running'] for kind in old}}


def safe_main(main):
    try:
        main()
    except Exception as error:
        # Do not emit tracebacks, raw child output or private input values.
        code = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(json.dumps({'state': 'STOPPED_FAIL_CLOSED', 'error': code, 'automatic_retry': False}))
        raise SystemExit(1)
