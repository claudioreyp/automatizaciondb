"""Reviewed source proposal. No Docker or filesystem mutation on import."""
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import yaml

ROOT = Path('/opt/escalar-qr')
STATE = ROOT / 'private/gateway-owner-r14-rollout'
OLD_TAG = 'escalar-impulsa-qr:85d6792'
NEW_TAG = 'escalar-impulsa-qr:owner-r14-7c8424ac'
CONTAINER = 'impulsa-qr'
PROPOSAL_SHA = '7c8424ac7156dc4e7673b819e7b0a4a3b107b525b20268a3fd47f02d6640d96e'
COMMIT = '85d6792dade60cf69728e5a25ad162377094a9f5'
IMAGE_RE = r'sha256:[a-f0-9]{64}'


def need(ok, code):
    if not ok:
        raise RuntimeError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def regular(path, private=False):
    st = path.lstat()
    need(stat.S_ISREG(st.st_mode) and not path.is_symlink()
         and path.resolve(strict=True) == path, 'REGULAR_NO_SYMLINK')
    if private:
        need(st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o600, 'ROOT_PRIVATE_FILE')
    return st


def exclusive(path, raw, mode=0o600):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
        os.fchmod(f.fileno(), mode)


def private_directory(path):
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) and path.resolve(strict=True) == path
         and not path.is_symlink() and info.st_uid == 0
         and stat.S_IMODE(info.st_mode) == 0o700, 'ROOT_PRIVATE_DIRECTORY')


def run(argv, timeout=30):
    # Never print Docker stderr: inspect/config/build diagnostics may contain env.
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    need(result.returncode == 0, 'FIXED_COMMAND_FAILED')
    return result.stdout


def docker(*args, timeout=30):
    return run(['docker', *args], timeout)


def local_docker_only():
    need(os.name == 'posix' and os.geteuid() == 0, 'LINUX_ROOT_REQUIRED')
    need(not os.environ.get('DOCKER_HOST') and not os.environ.get('DOCKER_CONTEXT'), 'NO_REMOTE_DOCKER_OVERRIDE')
    context = json.loads(docker('context', 'inspect'))
    need(len(context) == 1 and context[0]['Endpoints']['docker']['Host'] == 'unix:///var/run/docker.sock', 'LOCAL_DOCKER_SOCKET_ONLY')


def inspect(kind, identifier):
    value = json.loads(docker(kind, 'inspect', identifier))
    need(isinstance(value, list) and len(value) == 1, 'UNIQUE_DOCKER_IDENTITY')
    return value[0]


def load_pack(directory, expected):
    directory = directory.resolve(strict=True)
    raw = (directory / 'manifest.json').read_bytes()
    need(re.fullmatch(r'[a-f0-9]{64}', expected or '') and sha(raw) == expected, 'PINNED_PUBLIC_PACK')
    m = json.loads(raw)
    need(m['schema'] == 'escalarai.gateway-owner-r14-source-pack.v1'
         and m['proposal_sha256'] == PROPOSAL_SHA and m['runtime_files'] == 10
         and m['source_payloads'] == 14 and m['network_build'] is False, 'PACK_CONTRACT')
    expected_files = {'manifest.json', *m['files']}
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}
    need(actual == expected_files, 'EXACT_PACK_INVENTORY')
    for name, entry in m['files'].items():
        need(not Path(name).is_absolute() and '..' not in Path(name).parts, 'PACK_RELATIVE_FILE')
        p = directory / name
        regular(p)
        b = p.read_bytes()
        need(sha(b) == entry['sha256'] and len(b) == entry['bytes'], 'PACK_FILE_PIN')
    return m


def validate_binding(value):
    keys = {'schema', 'state', 'image_id', 'container_id', 'compose_file', 'compose_sha256',
            'project', 'service', 'profile_sha256', 'source_commit', 'source_manifest_sha256'}
    need(isinstance(value, dict) and set(value) == keys, 'BINDING_EXACT_FIELDS')
    need(value['schema'] == 'escalarai.gateway-owner-r14-server-binding.v1'
         and value['state'] == 'PINNED_SERVER_OBSERVATION_REVIEWED', 'SERVER_BINDING_NOT_RELEASED')
    need(re.fullmatch(IMAGE_RE, value['image_id'] or '') and re.fullmatch(r'[a-f0-9]{64}', value['container_id'] or ''), 'OBSERVED_IMAGE_AND_CONTAINER_REQUIRED')
    for key in ('compose_sha256', 'profile_sha256', 'source_manifest_sha256'):
        need(re.fullmatch(r'[a-f0-9]{64}', value[key] or ''), 'OBSERVED_DIGEST_REQUIRED')
    compose = PurePosixPath(value['compose_file'])
    need(compose.is_absolute() and '..' not in compose.parts and compose.name.endswith(('.yaml', '.yml'))
         and (compose.is_relative_to('/opt/escalar-qr') or compose.is_relative_to('/docker')), 'OBSERVED_COMPOSE_PATH')
    need(value['source_commit'] == COMMIT and value['service'] == CONTAINER
         and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', value['project'] or ''), 'EXACT_GATEWAY_SOURCE_PROJECT')
    return value


def load_binding(path, expected):
    need(path.is_absolute() and path.is_relative_to(ROOT / 'private'), 'PRIVATE_BINDING_PATH')
    regular(path, True)
    raw = path.read_bytes()
    need(sha(raw) == expected, 'PINNED_SERVER_BINDING')
    return validate_binding(json.loads(raw))


def compose_candidate(raw, service):
    """Replace one YAML scalar by source offsets; every other byte survives."""
    text = raw.decode('utf8')
    original = yaml.safe_load(text)
    svc = original['services'][service]
    need(svc.get('image') == OLD_TAG, 'CURRENT_COMPOSE_IMAGE_TAG')
    tree = yaml.compose(text)
    def child(node, key):
        matches = [v for k, v in node.value if k.value == key]
        need(len(matches) == 1, 'UNIQUE_YAML_KEY')
        return matches[0]
    scalar = child(child(child(tree, 'services'), service), 'image')
    need(isinstance(scalar, yaml.ScalarNode) and scalar.value == OLD_TAG
         and scalar.style in (None, "'", '"'), 'LITERAL_IMAGE_SCALAR')
    replacement = json.dumps(NEW_TAG)
    changed = (text[:scalar.start_mark.index] + replacement + text[scalar.end_mark.index:]).encode()
    parsed = yaml.safe_load(changed)
    inverse = copy.deepcopy(parsed)
    inverse['services'][service]['image'] = OLD_TAG
    need(inverse == original, 'ONLY_IMAGE_COMPOSE_INVERSE')
    return changed


def verify_derived_image(base, candidate):
    need(candidate['Config'] == base['Config'], 'IMAGE_CONFIG_EXACT')
    before, after = base['RootFS']['Layers'], candidate['RootFS']['Layers']
    need(after[:len(before)] == before and len(after) == len(before) + 1, 'ONE_COPY_LAYER_ONLY')
    need(candidate['Architecture'] == base['Architecture'] and candidate['Os'] == base['Os'], 'SAME_IMAGE_PLATFORM')


def container_projection(info):
    value = copy.deepcopy(info)
    config = value['Config']
    config.pop('Image', None)
    if config.get('Hostname') == info['Id'][:12]:
        config['Hostname'] = '<docker-default-container-id>'
    labels = config.get('Labels') or {}
    for key in ('com.docker.compose.config-hash', 'com.docker.compose.image'):
        labels.pop(key, None)
    networks = {k: {field: v.get(field) for field in ('NetworkID', 'Aliases', 'Links', 'IPAMConfig', 'DriverOpts')}
                for k, v in value['NetworkSettings']['Networks'].items()}
    return {'Config': config, 'HostConfig': value['HostConfig'], 'Mounts': value['Mounts'], 'Networks': networks}


def verify_container(info, binding, image_id):
    need(info['Name'] == '/' + CONTAINER and info['Image'] == image_id and info['State']['Running'], 'LIVE_GATEWAY_IDENTITY')
    labels = info['Config'].get('Labels') or {}
    need(labels.get('com.docker.compose.project') == binding['project']
         and labels.get('com.docker.compose.service') == binding['service'], 'COMPOSE_OWNERSHIP')
    env = info['Config'].get('Env') or []
    need(sum(x.startswith('IMPULSA_NATIVE_ACTIVE=') for x in env) == 1
         and 'IMPULSA_NATIVE_ACTIVE=false' in env, 'QR_NATIVE_ENV_FALSE')
    need(info['Config']['User'] in ('node', '1000', '1000:1000')
         and info['Config']['Entrypoint'] == ['/bin/sh', '/app/scripts/start-qr.sh'], 'DIRECT_BAILEYS_ENTRY_USER')
    mounts = info['Mounts']
    need(not any(x['Destination'] == '/app' or x['Destination'].startswith('/app/') for x in mounts), 'NO_CODE_OVERLAY_MOUNT')
    session = [m for m in mounts if m['Destination'] == '/var/lib/impulsa/session']
    profile = [m for m in mounts if m['Destination'] == '/var/lib/impulsa/session/native-profile.json']
    need(len(session) == 1 and session[0]['RW'] is True and len(profile) == 1 and profile[0]['RW'] is False, 'PRESERVED_SESSION_AND_RO_PROFILE')
    profile_path = Path(profile[0]['Source'])
    regular(profile_path)
    raw = profile_path.read_bytes()
    need(sha(raw) == binding['profile_sha256'] and json.loads(raw).get('enabled') is False, 'QR_PROFILE_BYTES_OFF')


def isolated_runtime_check(image_id, manifest_sha256):
    name = 'escalar-owner-r14-check-' + manifest_sha256[:12]
    def owned_ids():
        return docker('container', 'ls', '-a', '--no-trunc', '--filter', 'name=^/' + name + '$',
                      '--filter', 'label=escalar.owner-r14=' + manifest_sha256, '--format', '{{.ID}}').decode().splitlines()
    need(owned_ids() == [], 'OWN_RUNTIME_NAME_INITIAL_ABSENCE')
    exclusive(STATE / 'runtime-check-intent.json', encoded({'name': name, 'image': image_id, 'manifest_sha256': manifest_sha256}))
    try:
        container_id = docker('create', '--name', name, '--network=none', '--read-only', '--user', '1000:1000',
                              '--cap-drop=ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '64',
                              '--memory', '256m', '--label', 'escalar.owner-r14=' + manifest_sha256,
                              '--entrypoint', '/bin/sh', image_id, '/app/scripts/start-qr.sh', '--check-runtime').decode().strip()
        need(re.fullmatch(r'[a-f0-9]{64}', container_id), 'OWN_RUNTIME_CHECK_ID')
        exclusive(STATE / 'runtime-check-container.json', encoded({'id': container_id, 'name': name, 'image': image_id}))
        raw = docker('start', '-a', container_id, timeout=25)
        observed = inspect('container', container_id)
        need(observed['State']['Running'] is False and observed['State']['ExitCode'] == 0, 'ISOLATED_RUNTIME_CHECK_EXIT')
        return raw
    finally:
        ids = owned_ids()
        need(len(ids) <= 1, 'UNIQUE_OWN_RUNTIME_CLEANUP')
        if ids:
            observed = inspect('container', ids[0])
            need(observed['Name'] == '/' + name and observed['Image'] == image_id
                 and observed['Config']['Labels'].get('escalar.owner-r14') == manifest_sha256, 'OWN_CLEANUP_IDENTITY')
            docker('rm', '-f', ids[0])
        need(owned_ids() == [], 'OWN_RUNTIME_CONTAINER_REMOVED')


HASH_DIST_JS = r"""const f=require('fs'),p=require('path'),c=require('crypto'),o={};function walk(d){for(const x of f.readdirSync(d,{withFileTypes:true})){const n=p.join(d,x.name);if(x.isDirectory())walk(n);else{if(!x.isFile())throw Error('REGULAR_DIST_ONLY');o[p.relative('/app/dist',n)]=c.createHash('sha256').update(f.readFileSync(n)).digest('hex')}}}walk('/app/dist');process.stdout.write(JSON.stringify(o))"""


def dist_hashes(container):
    return json.loads(docker('exec', container, 'node', '-e', HASH_DIST_JS, timeout=20))


def expected_dist(pack, candidate=False):
    result = dict(pack['baseline_dist'])
    if candidate:
        result.update(pack['runtime_dist'])
    return result


def compose_command(binding, *args):
    return ['compose', '--project-directory', str(Path(binding['compose_file']).parent),
            '-p', binding['project'], '-f', binding['compose_file'], *args]


def verify_compose_config_hash(binding, container):
    raw = docker(*compose_command(binding, 'config', '--hash', binding['service'])).decode().split()
    need(len(raw) == 2 and raw[0] == binding['service']
         and re.fullmatch(r'[a-f0-9]{64}', raw[1])
         and container['Config']['Labels'].get('com.docker.compose.config-hash') == raw[1],
         'COMPOSE_CONFIG_MATCHES_RUNNING_CONTAINER')


def atomic_compose(path, old_hash, raw, identity):
    regular(path)
    need(sha(path.read_bytes()) == old_hash, 'COMPOSE_CAS_BYTES')
    temp = path.parent / (path.name + '.owner-r14-new')
    exclusive(temp, raw, identity['mode'])
    os.chown(temp, identity['uid'], identity['gid'])
    need(sha(path.read_bytes()) == old_hash, 'COMPOSE_CAS_RECHECK')
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def public_failure(code):
    # Exception strings and subprocess payloads are private. Stable local codes only.
    return code if re.fullmatch(r'[A-Z0-9_]{1,100}', code or '') else 'PREPARATION_OR_APPLY_FAILURE'
