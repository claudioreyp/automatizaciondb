"""Source proposal: prepare the pinned R12 native node without restarting n8n.

ROOT must bind the pending release pins in a reviewed source revision first.
Only public hashes and status metadata are printed. Compose copies stay private.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import yaml

RELEASED = True
PACK_SHA = '9d772c3830b4118af9f344c480f11e4816467565cf47328387f68d84adbed56b'
REGISTRY_SHA = '152ad9c508bf880cc08c514322162f7dde7727d1224df2f133c705e6ff89d2fb'
R12_MANIFEST_SHA = '858e8914c935ae96fd26f70ed73725f5ef233ec9909738da54d90b3cb587b951'
R12_REVIEW_SHA = '6410739216572dce0894236502a145408febf4cde9f80a2f0369dc90f45111fb'

COMPOSE = Path('/docker/n8n/docker-compose.yml')
COMPOSE_SHA = '795f8c4dc03bae93f6898c1613045a8e891d7ba3726730994e7391c068b41123'
ORIGINAL_CONTAINER = 'a159f598e3ba2a9772ab3518824df2b7507c84e8e39eb313aa0b3707e7f53d43'
IMAGE = 'sha256:4f448824ec99e1160e49eeb1c5bf2130a5d244fe9029e871a9f4d9f126dbfc98'
STATE = Path('/opt/escalar-qr/private/native-static-r12-rollout')
PRIOR_STATE = Path('/opt/escalar-qr/private/static-body-r11-rollout')
MODULE = Path('/opt/escalar-qr/modules/qr-native-static-r12')
RELEASE = Path('/opt/escalar-qr/release/tools/qr-native-static-r12')
SOURCE = RELEASE / 'node'
PACKAGE = '@escalar/native-static-node-operational-r01'
TARGET = '/usr/local/lib/node_modules/n8n/node_modules/' + PACKAGE
ALLOW = '@escalar/qr-native-r03,@escalar/static-code-body-poc-r01'
STATIC_PACKAGE = '@escalar/static-code-body-poc-r01'
R03_PACKAGE = '@escalar/qr-native-r03'
STATIC_MODULE = Path('/opt/escalar-qr/modules/qr-static-body-r11')
R03_MODULE = Path('/opt/escalar-qr/modules/qr-native-r03')
STATIC_TARGET = '/usr/local/lib/node_modules/n8n/node_modules/' + STATIC_PACKAGE
R03_TARGET = '/usr/local/lib/node_modules/n8n/node_modules/' + R03_PACKAGE
PINS = {
    'trusted-runtime.cjs': 'c95877b63cbc6a4dec66d51afd6536f2941ee1ed2b355e1d3c56a16ae0703b52',
    'EscalarStaticBody.node.js': '197b97e7be40883d97b3780f086d81ad3268cc25aeb03b4895eac7432a3dc0ed',
    'code-output.cjs': '9fbea889c1639748c6b2e4dc1df13862cbf11f94a615444f971d7e4080d2715b',
    'package.json': '02f85a9671a93e24063f23d7d653a54759674644331144aded7ee0a9c1ae10ed',
    'registry.json': REGISTRY_SHA,
}
STATIC_PINS = {'index.cjs': '1bb60b6fd4b06d781a69029bfaf6c51d73e3f3287a65564b8eb971e7c5e86c25',
               'package.json': '9cd6c0419caf525325314a42013f858057f4f6898ec5bc2532ba449a2f1aa2a5'}
R03_PINS = {'index.cjs': 'cc64011e21b074ae1fcad83909294fb8e200ab70712d872a898404a5fbf6ec92',
            'package.json': '7c3bc2f85ae6fe16a20eefa74569cdbdaf3eb319c8056ecf0b04b3e8607d5ea9'}
WORKFLOW_IDS = ('yQYG1gk4GkYDrkBp', '0cqeI78HXXsW2Dqj')
NATIVE_RESULT = Path('/opt/escalar-qr/native-static-node-operational-r01-result/result.json')
NATIVE_RESULT_SHA = '7d3ba57020cab8354ca8db8ba87b66fd36eab5a27ba82042419ea44a3cc6ad7b'
NATIVE_PACK_SHA = 'fe3c0066a4030ef39b10f95edb7374f1cd382549859a56f56e1eb5e81bcf8ad1'
PREPARE_NAME = 'prepare_persistent_native_static_r12.py'
APPLY_NAME = 'apply_persistent_native_static_r12.py'


def require(ok, code):
    if not ok:
        raise RuntimeError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def release_gate():
    require(RELEASED is True, 'SOURCE_PROPOSAL_CLOSED_PENDING_R12_REGISTRY_AND_PACK_REVIEW')
    require(all(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value)
                for value in (PACK_SHA, REGISTRY_SHA, R12_MANIFEST_SHA, R12_REVIEW_SHA)), 'UNBOUND_RELEASE_PIN')
    require(PINS['registry.json'] == REGISTRY_SHA, 'REGISTRY_PIN_CONSTANT_BINDING')


def json_bytes(value):
    return (json.dumps(value, indent=2) + '\n').encode('utf-8')


def regular(path, identity=None):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink(), 'REGULAR_FILE_REQUIRED')
    require(path.resolve(strict=True) == path, 'SYMLINK_ANCESTOR_REJECTED')
    if identity is not None:
        require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == identity, 'FILE_IDENTITY_OR_MODE')
    return info


def directory(path, mode=None):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and not path.is_symlink(), 'DIRECTORY_REQUIRED')
    require(path.resolve(strict=True) == path and info.st_uid == 0, 'ROOT_OWNED_REAL_DIRECTORY_REQUIRED')
    if mode is not None:
        require(stat.S_IMODE(info.st_mode) == mode, 'DIRECTORY_MODE')


def exclusive(path, raw, mode=0o600):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), mode)
    regular(path, (0, 0, mode))


def verify_module(path, pins):
    directory(path, 0o755)
    require(sorted(p.name for p in path.iterdir()) == sorted(pins), 'EXACT_MODULE_FILE_INVENTORY')
    for name, expected in pins.items():
        regular(path / name, (0, 0, 0o444))
        require(sha((path / name).read_bytes()) == expected, 'MODULE_FILE_PIN')


def registry_contract(raw):
    require(sha(raw) == REGISTRY_SHA, 'R12_REGISTRY_PIN')
    registry = json.loads(raw)
    require(set(registry) == {'schema', 'task_timeout_ms', 'timeout_scope', 'entries', 'libraries'}, 'REGISTRY_EXACT_FIELDS')
    require(registry['schema'] == 'escalarai.native-static-node-registry.v1'
            and type(registry['task_timeout_ms']) is int and registry['task_timeout_ms'] == 300000
            and registry['timeout_scope'] == 'OBSERVED_ORIGINAL_OPERATIONAL_300S_UNCHANGED', 'ORIGINAL_TIMEOUT_UNCHANGED')
    expected_libs = [{'name': STATIC_PACKAGE, 'sha256': STATIC_PINS['index.cjs']},
                     {'name': R03_PACKAGE, 'sha256': R03_PINS['index.cjs']}]
    require(registry['libraries'] == expected_libs, 'ONLY_TWO_EXISTING_LIBRARIES')
    entries = registry['entries']
    require(isinstance(entries, list) and len(entries) == 102, 'ONLY_102_R12_MAIN_BINDINGS_NO_PROBE')
    seen, counts = set(), {workflow_id: 0 for workflow_id in WORKFLOW_IDS}
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {'workflow_id', 'node_id', 'wrapper_sha256', 'body_key', 'package_name', 'mode'}, 'REGISTRY_ENTRY_FIELDS')
        require(entry['workflow_id'] in counts and isinstance(entry['node_id'], str) and entry['node_id'], 'ONLY_EXISTING_MAIN_WORKFLOWS')
        require(isinstance(entry['wrapper_sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', entry['wrapper_sha256'])
                and isinstance(entry['body_key'], str) and re.fullmatch(r'body_[a-f0-9]{64}', entry['body_key'])
                and entry['package_name'] == STATIC_PACKAGE and entry['mode'] == 'runOnceForAllItems', 'EXACT_WRAPPER_BINDING_CONTRACT')
        key = (entry['workflow_id'], entry['node_id'])
        require(key not in seen, 'DUPLICATE_WRAPPER_BINDING')
        seen.add(key)
        counts[entry['workflow_id']] += 1
    require(set(counts.values()) == {51}, '51_MAIN_BINDINGS_PER_PROFILE')
    return registry


def verify_release():
    release_gate()
    directory(RELEASE)
    regular(RELEASE / 'manifest.json')
    raw = (RELEASE / 'manifest.json').read_bytes()
    require(sha(raw) == PACK_SHA, 'EXACT_REVIEWED_RELEASE_MANIFEST')
    manifest = json.loads(raw)
    require(manifest.get('schema') == 'escalarai.native-static-node-persistent-r12-pack.v1'
            and manifest.get('state') == 'PREPARED_NOT_APPLIED'
            and manifest.get('expected_image_id') == IMAGE and manifest.get('module_package') == PACKAGE
            and manifest.get('registry_sha256') == REGISTRY_SHA and manifest.get('r12_manifest_sha256') == R12_MANIFEST_SHA
            and manifest.get('r12_independent_review_sha256') == R12_REVIEW_SHA, 'RELEASE_CANDIDATE_BINDINGS')
    files = manifest['files']
    # This payload manifest intentionally excludes installer sources. Those are
    # pinned by ROOT's separate source/publication review: including this source
    # in a manifest whose digest is embedded here would create a hash cycle.
    expected_files = {'node/' + name for name in PINS}
    require(set(files) == expected_files, 'ONLY_FIVE_REVIEWED_NODE_PAYLOAD_FILES')
    directory(SOURCE)
    require(sorted(p.name for p in SOURCE.iterdir()) == sorted(PINS), 'EXACT_SOURCE_NODE_INVENTORY')
    for relative, pin in files.items():
        file = RELEASE / relative
        regular(file)
        content = file.read_bytes()
        require(set(pin) == {'sha256', 'bytes'} and sha(content) == pin['sha256']
                and type(pin['bytes']) is int and len(content) == pin['bytes'], 'RELEASE_FILE_PIN_AND_SIZE')
        if relative.startswith('node/'):
            require(pin['sha256'] == PINS[relative[5:]], 'REVIEWED_NODE_SOURCE_PIN')
    regular(RELEASE / PREPARE_NAME)
    regular(RELEASE / APPLY_NAME)
    require(Path(__file__).resolve() == RELEASE / PREPARE_NAME, 'RUN_ONLY_REVIEWED_PUBLIC_PREPARER')
    package = json.loads((SOURCE / 'package.json').read_bytes())
    require(package == {'name': PACKAGE, 'version': '0.0.0-r01', 'private': True,
                        'main': 'EscalarStaticBody.node.js'}, 'EXACT_REVIEWED_PACKAGE_METADATA')
    registry_contract((SOURCE / 'registry.json').read_bytes())
    return manifest


def native_gate():
    directory(NATIVE_RESULT.parent)
    regular(NATIVE_RESULT)
    raw = NATIVE_RESULT.read_bytes()
    require(sha(raw) == NATIVE_RESULT_SHA, 'EXACT_ACTUAL_TWO_LIBRARY_PROOF')
    result = json.loads(raw)
    require(result.get('schema') == 'escalarai.native-static-node-two-library-result.v1'
            and result.get('status') == 'PASS' and result.get('failure') is None and result.get('cleanup_error') is None
            and result.get('cleanup') == 'REMOVED_OWN_CONTAINER_AND_TMPFS', 'NATIVE_PASS_AND_CLEANUP_REQUIRED')
    require(type(result.get('components')) is int and result['components'] == 2
            and type(result.get('benchmarks')) is int and result['benchmarks'] == 0
            and result.get('benchmark_metrics') == [] and result.get('manifest_sha256') == NATIVE_PACK_SHA
            and result.get('image_id') == IMAGE, 'NATIVE_TWO_LIBRARY_IMAGE_PACK_BINDING')
    require(type(result.get('models')) is int and result['models'] == 0
            and type(result.get('production_changes')) is int and result['production_changes'] == 0
            and result.get('network') == 'none' and result.get('whole_agent_acceptance') is False
            and result.get('agent_latency_acceptance') is False, 'NATIVE_NO_MODEL_NO_PRODUCTION_SCOPE')
    return {'result_path': str(NATIVE_RESULT), 'result_sha256': NATIVE_RESULT_SHA,
            'manifest_sha256': NATIVE_PACK_SHA, 'components': 2, 'benchmarks': 0, 'models': 0}


def prior_gate():
    directory(PRIOR_STATE, 0o700)
    regular(PRIOR_STATE / 'apply-result.json', (0, 0, 0o600))
    raw = (PRIOR_STATE / 'apply-result.json').read_bytes()
    prior = json.loads(raw)
    require(prior.get('state') == 'STATIC_BODY_R11_APPLIED_HEALTHY' and prior.get('container_id') == ORIGINAL_CONTAINER
            and prior.get('compose_sha256') == COMPOSE_SHA and prior.get('image_id') == IMAGE
            and prior.get('module_sha256') == STATIC_PINS['index.cjs'] and prior.get('package_json_sha256') == STATIC_PINS['package.json']
            and prior.get('pack_manifest_sha256') == 'b182497a1555a22fcde030d51d6ddfc919f00c4eca575b6459ceee3067f8aca1'
            and prior.get('allow_external') == ALLOW and prior.get('module_read_only') is True
            and prior.get('existing_R03_unchanged') is True and prior.get('health_http') == 200
            and prior.get('all_existing_mounts_security_and_configuration_unchanged') is True
            and prior.get('networks') == ['n8n_default'], 'TRUSTED_STATIC_BODY_R11_PRIOR_RESULT')
    return {'result_path': str(PRIOR_STATE / 'apply-result.json'), 'result_sha256': sha(raw), 'container_id': ORIGINAL_CONTAINER}


class Docker:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def run(self, args, seconds=45):
        remaining = min(seconds, self.deadline - time.monotonic())
        require(remaining > 0, 'LOCAL_OPERATION_DEADLINE')
        return subprocess.check_output(args, stderr=subprocess.PIPE, timeout=remaining)

    def compose(self, path):
        return ['docker', 'compose', '--project-directory', '/docker/n8n', '-f', str(path)]

    def config(self, path):
        return json.loads(self.run(self.compose(path) + ['config', '--format', 'json']))

    def config_hash(self, path):
        raw = self.run(self.compose(path) + ['config', '--hash', 'n8n']).decode('ascii').strip()
        values = re.findall(r'(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])', raw)
        require(len(values) == 1, 'ONE_OFFICIAL_COMPOSE_SERVICE_HASH')
        return values[0]

    def inspect(self):
        rows = json.loads(self.run(['docker', 'container', 'inspect', 'n8n-n8n-1']))
        require(len(rows) == 1, 'ONE_N8N_CONTAINER')
        return rows[0]


def one_mount(service, target, source):
    found = [v for v in service.get('volumes', []) if v.get('target') == target]
    require(len(found) == 1 and found[0].get('type') == 'bind' and found[0].get('source') == str(source)
            and found[0].get('read_only') is True, 'EXACT_READ_ONLY_MODULE_MOUNT')
    return found[0]


def compare_configs(before, after):
    service = before['services']['n8n']
    require(service['environment']['NODE_FUNCTION_ALLOW_EXTERNAL'] == ALLOW, 'EXACT_EXISTING_ALLOWLIST')
    require(service['environment'].get('N8N_RUNNERS_ENABLED') == 'true'
            and service['environment'].get('N8N_RUNNERS_INSECURE_MODE') in (None, 'false'), 'EXISTING_RUNNER_SECURITY')
    require('N8N_CUSTOM_EXTENSIONS' not in service['environment'], 'NO_EXISTING_CUSTOM_EXTENSIONS_TO_OVERWRITE')
    one_mount(service, R03_TARGET, R03_MODULE)
    one_mount(service, STATIC_TARGET, STATIC_MODULE)
    require(not any(v.get('target') == TARGET or v.get('source') == str(MODULE)
                    for v in service.get('volumes', [])), 'NEW_NODE_NOT_ALREADY_MOUNTED')
    check = copy.deepcopy(after)
    updated = check['services']['n8n']
    require(updated['environment'].pop('N8N_CUSTOM_EXTENSIONS', None) == TARGET, 'ONLY_EXACT_CUSTOM_EXTENSION_PATH')
    added = one_mount(updated, TARGET, MODULE)
    updated['volumes'].remove(added)
    require(check == before, 'ONLY_CUSTOM_EXTENSIONS_ENV_AND_ONE_RO_BIND_ALLOWED')
    return added


def container_identity(container, config_hash):
    labels = container['Config']['Labels']
    require(container['Name'] == '/n8n-n8n-1' and container['Image'] == IMAGE
            and labels.get('com.docker.compose.project') == 'n8n' and labels.get('com.docker.compose.service') == 'n8n'
            and labels.get('com.docker.compose.project.working_dir') == '/docker/n8n'
            and labels.get('com.docker.compose.project.config_files') == str(COMPOSE)
            and labels.get('com.docker.compose.config-hash') == config_hash, 'CONTAINER_IMAGE_PROJECT_SERVICE_HASH')
    require(set(container['NetworkSettings']['Networks']) == {'n8n_default'}, 'EXACT_N8N_NETWORK')


def compose_candidate(raw):
    text = raw.decode('utf-8')
    ast = yaml.compose(text)

    def field(node, name):
        require(isinstance(node, yaml.MappingNode), 'YAML_MAPPING_REQUIRED')
        found = [value for key, value in node.value if key.value == name]
        require(len(found) == 1, 'ONE_YAML_FIELD_REQUIRED')
        return found[0]

    service = field(field(ast, 'services'), 'n8n')
    environment, volumes = field(service, 'environment'), field(service, 'volumes')
    require(isinstance(volumes, yaml.SequenceNode) and volumes.flow_style is False, 'BLOCK_VOLUME_LIST_REQUIRED')
    newline = '\r\n' if '\r\n' in text else '\n'
    if isinstance(environment, yaml.SequenceNode):
        require(environment.flow_style is False and len(environment.value) > 0, 'BLOCK_ENV_LIST_REQUIRED')
        require(all(isinstance(n, yaml.ScalarNode) for n in environment.value), 'LITERAL_ENV_LIST_REQUIRED')
        require(not any(n.value.split('=', 1)[0] == 'N8N_CUSTOM_EXTENSIONS' for n in environment.value), 'CUSTOM_EXTENSION_SCALAR_ALREADY_PRESENT')
        env_line = ' ' * environment.start_mark.column + '- N8N_CUSTOM_EXTENSIONS=' + TARGET + newline
    else:
        require(isinstance(environment, yaml.MappingNode) and environment.flow_style is False and len(environment.value) > 0, 'BLOCK_ENV_MAPPING_REQUIRED')
        require(not any(key.value == 'N8N_CUSTOM_EXTENSIONS' for key, _value in environment.value), 'CUSTOM_EXTENSION_KEY_ALREADY_PRESENT')
        env_line = ' ' * environment.start_mark.column + 'N8N_CUSTOM_EXTENSIONS: ' + TARGET + newline
    lines = text.splitlines(keepends=True)
    env_at = sum(len(line) for line in lines[:environment.end_mark.line])
    mount_at = sum(len(line) for line in lines[:volumes.end_mark.line])
    mount_line = ' ' * volumes.start_mark.column + '- ' + str(MODULE) + ':' + TARGET + ':ro' + newline
    require(env_at != mount_at, 'DISTINCT_ENV_AND_MOUNT_INSERTIONS')
    operations = [(env_at, env_line), (mount_at, mount_line)]
    candidate = text
    for at, inserted in sorted(operations, reverse=True):
        candidate = candidate[:at] + inserted + candidate[at:]
    delta, positioned = 0, []
    for at, inserted in sorted(operations):
        positioned.append((at + delta, inserted))
        delta += len(inserted)
    restored = candidate
    for at, inserted in reversed(positioned):
        require(restored[at:at + len(inserted)] == inserted, 'YAML_INVERSE_SITE')
        restored = restored[:at] + restored[at + len(inserted):]
    require(restored == text, 'EXACT_ORIGINAL_YAML_INVERSE')
    return candidate.encode('utf-8')


def main():
    release_gate()  # Closed before any filesystem, Docker or service operation.
    require(os.geteuid() == 0, 'ROOT_REQUIRED')
    verify_release()
    gate, prior = native_gate(), prior_gate()
    verify_module(R03_MODULE, R03_PINS)
    verify_module(STATIC_MODULE, STATIC_PINS)
    regular(COMPOSE, (1001, 996, 0o644))
    directory(COMPOSE.parent, 0o755)
    raw = COMPOSE.read_bytes()
    require(sha(raw) == COMPOSE_SHA, 'EXACT_CURRENT_COMPOSE_PIN')
    docker = Docker(60)
    before, current = docker.config(COMPOSE), docker.inspect()
    source_hash = docker.config_hash(COMPOSE)
    container_identity(current, source_hash)
    require(current['State']['Running'] is True and current['Id'] == ORIGINAL_CONTAINER, 'EXACT_PRIOR_RUNNING_CONTAINER')
    image = json.loads(docker.run(['docker', 'image', 'inspect', before['services']['n8n']['image']]))[0]
    require(image['Id'] == IMAGE, 'NO_IMAGE_UPGRADE_ALLOWED')
    require([v for v in current['Config']['Env'] if v.startswith('NODE_FUNCTION_ALLOW_EXTERNAL=')]
            == ['NODE_FUNCTION_ALLOW_EXTERNAL=' + ALLOW], 'CURRENT_CONTAINER_ALLOWLIST_UNCHANGED')
    require(not any(v.startswith('N8N_CUSTOM_EXTENSIONS=') for v in current['Config']['Env']), 'CURRENT_CONTAINER_HAS_NO_CUSTOM_EXTENSIONS')
    candidate = compose_candidate(raw)
    directory(STATE.parent, 0o700)
    directory(MODULE.parent, 0o755)
    require(not STATE.exists() and not MODULE.exists(), 'FRESH_PREPARATION_ONLY')
    STATE.mkdir(mode=0o700)
    os.chmod(STATE, 0o700)
    exclusive(STATE / 'original.yml', raw)
    exclusive(STATE / 'candidate.yml', candidate)
    added = compare_configs(before, docker.config(STATE / 'candidate.yml'))
    candidate_hash = docker.config_hash(STATE / 'candidate.yml')
    require(candidate_hash != source_hash, 'NEW_SERVICE_HASH_REQUIRED')
    MODULE.mkdir(mode=0o755)
    os.chmod(MODULE, 0o755)
    for name, expected in PINS.items():
        content = (SOURCE / name).read_bytes()
        require(sha(content) == expected, 'SOURCE_RECHECK_BEFORE_COPY')
        exclusive(MODULE / name, content, 0o444)
    verify_module(MODULE, PINS)
    registry_contract((MODULE / 'registry.json').read_bytes())
    verify_module(R03_MODULE, R03_PINS)
    verify_module(STATIC_MODULE, STATIC_PINS)
    require(COMPOSE.read_bytes() == raw and docker.inspect()['Id'] == ORIGINAL_CONTAINER, 'PRODUCTION_UNCHANGED_DURING_PREPARE')
    manifest = {'schema': 'escalarai.native-static-node-persistent-r12-preparation.v1', 'state': 'PREPARED_NOT_APPLIED_NO_RESTART',
                'source_sha256': COMPOSE_SHA, 'candidate_sha256': sha(candidate), 'compose_path': str(COMPOSE),
                'compose_identity': [1001, 996, 0o644], 'container_id': ORIGINAL_CONTAINER, 'image_id': IMAGE,
                'compose_image': before['services']['n8n']['image'], 'source_config_hash': source_hash,
                'candidate_config_hash': candidate_hash, 'module_path': str(MODULE), 'module_target': TARGET,
                'pins': PINS, 'r03_pins': R03_PINS, 'static_pins': STATIC_PINS, 'pack_manifest_sha256': PACK_SHA,
                'r12_manifest_sha256': R12_MANIFEST_SHA, 'r12_independent_review_sha256': R12_REVIEW_SHA,
                'native_gate': gate, 'prior_gate': prior, 'prepare_script_sha256': sha(Path(__file__).read_bytes()),
                'apply_script_sha256': sha((RELEASE / APPLY_NAME).read_bytes()),
                'changes': {'custom_extensions_before': None, 'custom_extensions_after': TARGET, 'read_only_mount': added},
                'other_services_and_configuration_identical': True, 'production_configuration_changed': False,
                'models': 0, 'workflow_activation': 0, 'ledger_writes': 0,
                'external_snapshot_boundary': 'ROOT captures all14 OFF workflows and complete closed head39 ledger via API before/after separately.'}
    exclusive(STATE / 'manifest.json', json_bytes(manifest))
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
