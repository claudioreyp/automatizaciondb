"""R04 proposal for the observed direct container. No build or live work on import."""
import copy
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import time
from urllib.parse import quote
import common as c

IMAGE = 'sha256:5b3fccf054cefcfb10d984554ac82424f5a41e2a91dcd21ccaf67bc9c2e868f6'
CONTAINER_ID = '36253d0d4c36003f59382b81cc7db4049e397a408d7ea1089a9dc982ceb27f84'
STATE = c.ROOT / 'private/gateway-owner-r14-direct-rollout'
MODULE = c.ROOT / 'modules/gateway-owner-r14-runtime'
CANDIDATE = 'impulsa-qr-owner-r14-candidate'
BACKUP = 'impulsa-qr-pre-owner-r14'
FAILED = 'impulsa-qr-owner-r14-failed'
LABEL = 'escalar.owner-r14'
ENV_NAMES = {'IMPULSA_NATIVE_PROFILE', 'IMPULSA_NATIVE_ACTIVE', 'PATH', 'NODE_VERSION', 'YARN_VERSION',
             'NODE_ENV', 'IMPULSA_QR_LISTEN_HOST', 'IMPULSA_QR_SESSION_DIR'}
OLD_MOUNTS = [('/opt/escalar-qr/session', '/var/lib/impulsa/session', True),
              ('/opt/escalar-qr/private/gateway-credential.json', '/var/lib/impulsa/gateway-credential.json', False)]


class LocalConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def api(method, route, body=None, timeout=25, statuses=(200, 201, 204)):
    c.need(route.startswith('/') and not route.startswith('//') and '\r' not in route and '\n' not in route, 'LOCAL_FIXED_API_ROUTE')
    conn = LocalConnection('localhost', timeout=timeout)
    try:
        raw = None if body is None else json.dumps(body, separators=(',', ':')).encode()
        conn.request(method, route, body=raw, headers={'Content-Type': 'application/json'})
        response = conn.getresponse()
        data = response.read(16 * 1024 * 1024 + 1)
        c.need(len(data) <= 16 * 1024 * 1024 and response.status in statuses, 'LOCAL_DOCKER_API_FAILED')
        return json.loads(data) if data else None
    finally:
        conn.close()


def inspect(name):
    c.need(re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}', name), 'FIXED_CONTAINER_REFERENCE')
    return api('GET', '/containers/' + quote(name, safe='') + '/json')


def action(cid, verb, timeout=25):
    c.need(re.fullmatch(r'[a-f0-9]{64}', cid) and verb in ('start', 'stop'), 'OWN_CONTAINER_ACTION')
    return api('POST', '/containers/' + cid + '/' + verb + ('?t=10' if verb == 'stop' else ''), timeout=timeout, statuses=(204, 304))


def rename(cid, name):
    c.need(re.fullmatch(r'[a-f0-9]{64}', cid) and name in (c.CONTAINER, CANDIDATE, BACKUP, FAILED), 'FIXED_OWN_RENAME')
    return api('POST', '/containers/' + cid + '/rename?name=' + name, statuses=(204,))


def absence(names):
    rows = api('GET', '/containers/json?all=1')
    actual = {n.lstrip('/') for row in rows for n in row['Names']}
    c.need(not actual.intersection(names), 'OWN_NAMES_MUST_BE_ABSENT')


def load_binding(path, digest, pack):
    c.regular(path, True)
    c.need(path.is_absolute() and path.is_relative_to(c.ROOT / 'private') and c.sha(path.read_bytes()) == digest, 'PINNED_PRIVATE_DIRECT_BINDING')
    b = json.loads(path.read_bytes())
    c.need(set(b) == {'schema', 'state', 'image_id', 'container_id', 'profile_sha256', 'transport_manifest_sha256'}, 'DIRECT_BINDING_FIELDS')
    c.need(b['schema'] == 'escalarai.gateway-owner-r14-direct-binding.v1'
           and b['state'] == 'PINNED_SERVER_OBSERVATION_REVIEWED', 'DIRECT_BINDING_NOT_RELEASED')
    c.need(b['image_id'] == IMAGE and b['container_id'] == CONTAINER_ID
           and b['transport_manifest_sha256'] == pack['transport_manifest_sha256']
           and re.fullmatch(r'[a-f0-9]{64}', b['profile_sha256'] or ''), 'ACTUAL_SERVER_DIRECT_BINDING')
    return b


def mount_rows(info):
    return sorted((m['Source'], m['Destination'], m['RW']) for m in info['Mounts'])


def verify_profile(binding):
    profile = Path('/opt/escalar-qr/session/native-profile.json')
    c.regular(profile)
    raw = profile.read_bytes()
    c.need(c.sha(raw) == binding['profile_sha256'] and json.loads(raw).get('enabled') is False, 'EXACT_PROFILE_INSIDE_SESSION_OFF')


def verify_original(info, binding):
    c.need(info['Id'] == CONTAINER_ID and info['Name'] == '/impulsa-qr' and info['Image'] == IMAGE
           and info['State']['Running'] and not info['State']['Paused'], 'EXACT_OBSERVED_DIRECT_CONTAINER')
    config = info['Config']
    c.need(config['User'] == 'node' and config['Entrypoint'] == ['/bin/sh', '/app/scripts/start-qr.sh']
           and config.get('Cmd') is None and config.get('Labels') == {}, 'EXACT_DIRECT_ENTRY_CONFIG')
    env = config['Env']; names = [v.split('=', 1)[0] for v in env]
    c.need(len(names) == len(set(names)) and set(names) == ENV_NAMES
           and 'IMPULSA_NATIVE_ACTIVE=false' in env
           and 'IMPULSA_NATIVE_PROFILE=/var/lib/impulsa/session/native-profile.json' in env
           and 'IMPULSA_QR_SESSION_DIR=/var/lib/impulsa/session' in env, 'EXACT_OBSERVED_OFF_ENV_PATHS')
    c.need(mount_rows(info) == sorted(OLD_MOUNTS) and all(m['Type'] == 'bind' for m in info['Mounts']), 'EXACT_TWO_PRESERVED_BINDS')
    c.need(not info['HostConfig'].get('AutoRemove') and not info['HostConfig']['NetworkMode'].startswith('container:'), 'PRESERVABLE_DIRECT_LIFECYCLE')
    verify_profile(binding)


def runtime_binds(pack):
    names = sorted(pack['runtime_dist'])
    c.need(len(names) == 10 and all(re.fullmatch(r'[a-z0-9-]+\.(js|mjs)', n) for n in names), 'TEN_FIXED_RUNTIME_FILES')
    return [str(MODULE / n) + ':/app/dist/' + n + ':ro' for n in names]


def create_payload(before, pack, manifest_sha256):
    config = copy.deepcopy(before['Config'])
    config['Image'] = IMAGE  # Immutable object, independent of mutable tag lookup.
    config['Labels'] = {**(config.get('Labels') or {}), LABEL: manifest_sha256}
    host = copy.deepcopy(before['HostConfig'])
    host['Binds'] = [*(host.get('Binds') or []), *runtime_binds(pack)]
    endpoints = {name: {k: copy.deepcopy(endpoint[k]) for k in ('IPAMConfig', 'Links', 'Aliases', 'DriverOpts')
                        if endpoint.get(k) is not None}
                 for name, endpoint in before['NetworkSettings']['Networks'].items()}
    return {**config, 'HostConfig': host, 'NetworkingConfig': {'EndpointsConfig': endpoints}}


def verify_candidate(after, before, pack, manifest_sha256, running, verify_network=True):
    c.need(after['Image'] == IMAGE and after['State']['Running'] is running, 'SAME_IMAGE_CANDIDATE_STATE')
    config = copy.deepcopy(after['Config']); prior = copy.deepcopy(before['Config'])
    config.pop('Image', None); prior.pop('Image', None)
    c.need(config.get('Labels', {}).pop(LABEL, None) == manifest_sha256 and config == prior, 'ALL_ENV_ARGV_AND_CONFIG_EXACT_PLUS_LABEL')
    host = copy.deepcopy(after['HostConfig']); original = copy.deepcopy(before['HostConfig'])
    c.need(host.get('Binds') == [*(original.get('Binds') or []), *runtime_binds(pack)], 'TEN_ADDITIVE_RO_BIND_DECLARATIONS')
    host['Binds'] = original.get('Binds')
    c.need(host == original, 'ALL_OTHER_HOST_SECURITY_PORTS_RESTART_EXACT')
    expected = sorted([*OLD_MOUNTS, *((str(MODULE / n), '/app/dist/' + n, False) for n in pack['runtime_dist'])])
    c.need(mount_rows(after) == expected and all(m['Type'] == 'bind' for m in after['Mounts']), 'TWO_OLD_PLUS_TEN_RO_CODE_MOUNTS')
    if running and verify_network:
        c.need(set(after['NetworkSettings']['Networks']) == set(before['NetworkSettings']['Networks']), 'SAME_NETWORK_SET')
        for name, old in before['NetworkSettings']['Networks'].items():
            new = after['NetworkSettings']['Networks'][name]
            for key in ('NetworkID', 'IPAMConfig', 'Links', 'Aliases', 'DriverOpts'):
                c.need(new.get(key) == old.get(key), 'EXACT_NETWORK_SCOPE_ALIASES')


def verify_module(pack):
    c.need(MODULE.resolve(strict=True) == MODULE and not MODULE.is_symlink(), 'REAL_FIXED_MODULE_DIRECTORY')
    st = MODULE.stat()
    c.need(st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o555
           and {p.name for p in MODULE.iterdir()} == set(pack['runtime_dist']), 'EXACT_TEN_MODULE_INVENTORY')
    for name, digest in pack['runtime_dist'].items():
        p = MODULE / name; st = c.regular(p)
        c.need(st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o444 and c.sha(p.read_bytes()) == digest, 'READ_ONLY_CODE_FILE_PIN')


def isolated_proof(pack, manifest_sha256):
    """Public code/utility imports only. No session/env/private mount or network."""
    name = 'escalar-owner-r14-direct-proof'
    absence({name})
    config = {'Image': IMAGE, 'User': '1000:1000', 'Entrypoint': ['node'],
              'Cmd': ['--input-type=module', '-e', "await import('/app/dist/qr-direct.js');"],
              'Env': ['IMPULSA_NATIVE_ACTIVE=false'], 'Labels': {LABEL: manifest_sha256},
              'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Binds': runtime_binds(pack),
                             'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges'], 'PidsLimit': 64,
                             'Memory': 268435456, 'NanoCpus': 1000000000}}
    # Same reviewed --check-runtime branch, before provider/session construction.
    config['Entrypoint'] = ['/bin/sh', '/app/scripts/start-qr.sh']; config['Cmd'] = ['--check-runtime']
    c.exclusive(STATE / 'proof-intent.json', c.encoded({'name': name, 'image': IMAGE, 'manifest_sha256': manifest_sha256}))
    cid = None
    try:
        result = api('POST', '/containers/create?name=' + name, config)
        cid = result['Id']; c.need(re.fullmatch(r'[a-f0-9]{64}', cid), 'OWN_PROOF_ID')
        c.exclusive(STATE / 'proof-created.json', c.encoded({'id': cid}))
        action(cid, 'start')
        result = api('POST', '/containers/' + cid + '/wait?condition=not-running', timeout=25)
        c.need(result['StatusCode'] == 0, 'ISOLATED_CHECK_RUNTIME_FAILED')
        own = inspect(cid)
        c.need(own['Image'] == IMAGE and own['Config']['Labels'].get(LABEL) == manifest_sha256, 'OWN_PROOF_OUTPUT_IDENTITY')
        c.exclusive(STATE / 'proof.stdout', c.docker('logs', cid, timeout=10))
        c.exclusive(STATE / 'proof-result.json', c.encoded({'id': cid, 'exit_code': result['StatusCode'],
                                                         'image_id': IMAGE, 'no_session_or_credentials': True}))
    finally:
        # Recover an unknown create response only by the prechecked name plus
        # immutable image and this run's exact manifest label. Never restart.
        rows = api('GET', '/containers/json?all=1')
        matches = [r for r in rows if '/' + name in r['Names']]
        c.need(len(matches) <= 1, 'UNIQUE_OWN_PROOF_CLEANUP')
        if matches:
            own = inspect(matches[0]['Id'])
            c.need(own['Image'] == IMAGE and own['Config']['Labels'].get(LABEL) == manifest_sha256, 'OWN_PROOF_CLEANUP_IDENTITY')
            api('DELETE', '/containers/' + own['Id'] + '?force=1', statuses=(204,))
        absence({name})


def health():
    code = "fetch('http://127.0.0.1:3008/api/health',{headers:{Host:'127.0.0.1:3008'},signal:AbortSignal.timeout(5000),redirect:'error'}).then(async r=>{const j=await r.json();if(r.status!==200||j.consumerMessaging!=='paused'||j.n8nForwarding!==false||j.mode!=='direct_pairing_only')throw Error('ACTIVE');process.stdout.write('PAUSED_200')}).catch(()=>process.exit(1))"
    for _ in range(10):
        try:
            if c.docker('exec', c.CONTAINER, 'node', '-e', code, timeout=7) == b'PAUSED_200': return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('DIRECT_HEALTH_PAUSED_UNCONFIRMED')
