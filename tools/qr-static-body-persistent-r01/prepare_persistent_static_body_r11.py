"""Prepare the additive R11 module only; never apply, restart or activate.

Run on the VPS only after the isolated native probe passes. Private compose
copies remain root-only. This file is also the shared verifier for the apply
script. All printed output is metadata; service environment values stay private.
"""
import argparse
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

COMPOSE = Path('/docker/n8n/docker-compose.yml')
COMPOSE_SHA = '6de30e441dd41e1ab53aac39c8d7f809bfd20b51d41ec28edcc6569aeed9122e'
STATE = Path('/opt/escalar-qr/private/static-body-r11-rollout')
R03_STATE = Path('/opt/escalar-qr/private/module-r03-rollout')
R03_MODULE = Path('/opt/escalar-qr/modules/qr-native-r03')
MODULE = Path('/opt/escalar-qr/modules/qr-static-body-r11')
RELEASE = Path('/opt/escalar-qr/release/tools/qr-static-body-native-r01')
SOURCE = RELEASE / 'package'
TARGET = '/usr/local/lib/node_modules/n8n/node_modules/@escalar/static-code-body-poc-r01'
R03_TARGET = '/usr/local/lib/node_modules/n8n/node_modules/@escalar/qr-native-r03'
OLD_ALLOW = '@escalar/qr-native-r03'
NEW_ALLOW = OLD_ALLOW + ',@escalar/static-code-body-poc-r01'
IMAGE = 'sha256:4f448824ec99e1160e49eeb1c5bf2130a5d244fe9029e871a9f4d9f126dbfc98'
PACK_SHA = 'b182497a1555a22fcde030d51d6ddfc919f00c4eca575b6459ceee3067f8aca1'
PINS = {'index.cjs': '1bb60b6fd4b06d781a69029bfaf6c51d73e3f3287a65564b8eb971e7c5e86c25',
        'package.json': '9cd6c0419caf525325314a42013f858057f4f6898ec5bc2532ba449a2f1aa2a5'}
R03_PINS = {'index.cjs': 'cc64011e21b074ae1fcad83909294fb8e200ab70712d872a898404a5fbf6ec92',
            'package.json': '7c3bc2f85ae6fe16a20eefa74569cdbdaf3eb319c8056ecf0b04b3e8607d5ea9'}
CASE_IDS = [kind + ':' + variant for kind in ('text', 'png', 'ambient', 'nominal', 'kernel')
            for variant in ('original', 'module')]


def require(ok, code):
    if not ok:
        raise RuntimeError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


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
        file = path / name
        regular(file, (0, 0, 0o444))
        require(sha(file.read_bytes()) == expected, 'MODULE_FILE_PIN')


def verify_release():
    directory(RELEASE)
    manifest_path = RELEASE / 'manifest.json'
    regular(manifest_path)
    raw = manifest_path.read_bytes()
    require(sha(raw) == PACK_SHA, 'EXACT_NATIVE_PACK_MANIFEST')
    manifest = json.loads(raw)
    require(manifest['module_package'] == NEW_ALLOW.split(',')[1]
            and manifest['module_sha256'] == PINS['index.cjs']
            and manifest['expected_image_id'] == IMAGE, 'PACK_MODULE_IMAGE_BINDING')
    require(manifest['expected_case_ids'] == CASE_IDS and manifest['expected_cases'] == 10, 'PACK_CASE_BINDING')
    directory(SOURCE)
    require(sorted(p.name for p in SOURCE.iterdir()) == sorted(PINS), 'EXACT_SOURCE_PACKAGE_INVENTORY')
    for name, expected in PINS.items():
        file = SOURCE / name
        regular(file)
        raw = file.read_bytes()
        require(sha(raw) == expected == manifest['files']['package/' + name], 'SOURCE_PACKAGE_PIN')
        require(len(raw) == manifest['file_bytes']['package/' + name], 'SOURCE_PACKAGE_SIZE')
    package = json.loads((SOURCE / 'package.json').read_bytes())
    require(package == {'name': '@escalar/static-code-body-poc-r01', 'version': '0.0.0-poc-r01',
                        'private': True, 'type': 'commonjs', 'main': 'index.cjs'}, 'PACKAGE_METADATA')
    return manifest


def native_gate(result_path, expected_sha):
    require(re.fullmatch(r'[a-f0-9]{64}', expected_sha) is not None, 'NATIVE_RESULT_DIGEST_REQUIRED')
    require(result_path.name == 'result.json' and result_path.parent.parent == Path('/opt/escalar-qr')
            and re.fullmatch(r'static-body-native-r01-[a-z0-9][a-z0-9-]{0,47}', result_path.parent.name),
            'REVIEWED_NATIVE_OUTPUT_PATH_REQUIRED')
    directory(result_path.parent)
    regular(result_path)
    result_raw = result_path.read_bytes()
    require(sha(result_raw) == expected_sha, 'NATIVE_RESULT_PIN')
    result = json.loads(result_raw)
    require(result.get('state') == 'NATIVE_STATIC_BODY_COMPONENTS_PASS'
            and result.get('failure') is None and result.get('cleanup_error') is None
            and type(result.get('exit_code')) is int and result['exit_code'] == 0,
            'NATIVE_PASS_AND_EXIT_REQUIRED')
    require(result.get('cleanup') in ('REMOVED_SYNTHETIC_CONTAINER_AND_TMPFS', 'CONFIRMED_CREATED_CONTAINER_ALREADY_ABSENT')
            and result.get('image_id') == IMAGE and result.get('manifest_sha256') == PACK_SHA
            and result.get('response_saved') is True, 'NATIVE_CLEANUP_IMAGE_PACK_BINDING')
    require(result.get('models') == 0 and result.get('production_changes') == 0
            and result.get('network') == 'none' and result.get('agent_latency_acceptance') is False
            and result.get('whole_agent_acceptance') is False, 'NATIVE_ZERO_IO_SCOPE')
    response_path = result_path.parent / 'response.json'
    regular(response_path)
    response_raw = response_path.read_bytes()
    final = json.loads(response_raw)
    require(final.get('schema') == 'escalarai.static-body-native-result.v1' and final.get('status') == 'PASS'
            and final.get('workflow_id') == 'QrStaticBodyNativeR0120261104'
            and final.get('module_package') == '@escalar/static-code-body-poc-r01'
            and final.get('expected_cases') == 10 and final.get('case_ids') == CASE_IDS, 'NATIVE_RESPONSE_SCOPE')
    require(all(final.get(key) is True for key in ('physical_storage_acceptance', 'nominal_pairing_acceptance',
                'ambient_absence_acceptance', 'kernel_disabled_acceptance')), 'NATIVE_ACCEPTANCE_FLAGS')
    require(all(type(final.get(key)) is int and final[key] == 0 for key in ('real_model_calls', 'real_pos_requests',
                'data_table_requests', 'external_requests', 'production_changes')), 'NATIVE_RESPONSE_ZERO_IO')
    require(final.get('acceptance_scope') == 'ISOLATED_COMPONENTS_ONLY' and final.get('agent_latency_acceptance') is False
            and final.get('whole_agent_acceptance') is False, 'NATIVE_RESPONSE_ACCEPTANCE_SCOPE')
    rows = final.get('cases')
    require(isinstance(rows, list) and len(rows) == 10 and [r.get('case_id') for r in rows] == CASE_IDS, 'NATIVE_CASE_ROWS')
    for kind in ('text', 'png', 'ambient', 'nominal', 'kernel'):
        pair = [r for r in rows if r.get('case_name') == kind]
        require(len(pair) == 2 and [r.get('variant') for r in pair] == ['original', 'module']
                and all(r.get('status') == 'PASS' for r in pair)
                and pair[0].get('result') == pair[1].get('result'), 'NATIVE_CASE_PAIR_EQUALITY')
    return {'result_path': str(result_path), 'result_sha256': expected_sha,
            'response_sha256': sha(response_raw), 'pack_sha256': PACK_SHA}


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
        # Compose's own hash binds a recreated container to the intended service.
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
    require(before['services']['n8n']['environment']['NODE_FUNCTION_ALLOW_EXTERNAL'] == OLD_ALLOW, 'EXACT_PRIOR_ALLOWLIST')
    require(before['services']['n8n']['environment'].get('N8N_RUNNERS_ENABLED') == 'true'
            and before['services']['n8n']['environment'].get('N8N_RUNNERS_INSECURE_MODE') in (None, 'false'), 'EXISTING_RUNNER_SECURITY')
    one_mount(before['services']['n8n'], R03_TARGET, R03_MODULE)
    require(not any(v.get('target') == TARGET or v.get('source') == str(MODULE)
                    for v in before['services']['n8n'].get('volumes', [])), 'NEW_MODULE_NOT_ALREADY_MOUNTED')
    check = copy.deepcopy(after)
    service = check['services']['n8n']
    require(service['environment']['NODE_FUNCTION_ALLOW_EXTERNAL'] == NEW_ALLOW, 'EXACT_APPEND_ONLY_ALLOWLIST')
    service['environment']['NODE_FUNCTION_ALLOW_EXTERNAL'] = OLD_ALLOW
    added = one_mount(service, TARGET, MODULE)
    service['volumes'].remove(added)
    require(check == before, 'ONLY_ALLOWLIST_APPEND_AND_ONE_RO_MOUNT_ALLOWED')
    return added


def container_identity(container, config_hash):
    labels = container['Config']['Labels']
    require(container['Name'] == '/n8n-n8n-1' and container['Image'] == IMAGE
            and labels.get('com.docker.compose.project') == 'n8n'
            and labels.get('com.docker.compose.service') == 'n8n'
            and labels.get('com.docker.compose.project.working_dir') == '/docker/n8n'
            and labels.get('com.docker.compose.project.config_files') == str(COMPOSE)
            and labels.get('com.docker.compose.config-hash') == config_hash, 'CONTAINER_ID_IMAGE_PROJECT_SERVICE_HASH')
    require(set(container['NetworkSettings']['Networks']) == {'n8n_default'}, 'EXACT_N8N_NETWORK')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-result', required=True, type=Path)
    parser.add_argument('--native-result-sha256', required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0, 'ROOT_REQUIRED')
    verify_release()
    gate = native_gate(args.native_result, args.native_result_sha256)
    regular(R03_STATE / 'apply-result.json', (0, 0, 0o600))
    prior = json.loads((R03_STATE / 'apply-result.json').read_bytes())
    require(prior.get('state') == 'MODULE_R03_APPLIED_HEALTHY' and prior.get('compose_sha256') == COMPOSE_SHA
            and prior.get('image_id') == IMAGE and prior.get('module_sha256') == R03_PINS['index.cjs']
            and prior.get('allow_external') == OLD_ALLOW, 'TRUSTED_R03_APPLY_RESULT_BINDING')
    verify_module(R03_MODULE, R03_PINS)
    regular(COMPOSE, (1001, 996, 0o644))
    directory(COMPOSE.parent, 0o755)
    raw = COMPOSE.read_bytes()
    require(sha(raw) == COMPOSE_SHA, 'EXACT_CURRENT_COMPOSE_PIN')
    docker = Docker(60)
    before = docker.config(COMPOSE)
    current = docker.inspect()
    source_config_hash = docker.config_hash(COMPOSE)
    container_identity(current, source_config_hash)
    require(current['State']['Running'] is True and current['Id'] == prior['container_id'], 'EXACT_PRIOR_RUNNING_CONTAINER')
    image = json.loads(docker.run(['docker', 'image', 'inspect', before['services']['n8n']['image']]))[0]
    require(image['Id'] == IMAGE, 'NO_IMAGE_UPGRADE_ALLOWED')
    require([v for v in current['Config']['Env'] if v.startswith('NODE_FUNCTION_ALLOW_EXTERNAL=')]
            == ['NODE_FUNCTION_ALLOW_EXTERNAL=' + OLD_ALLOW], 'CURRENT_CONTAINER_ALLOWLIST')
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
    if isinstance(environment, yaml.SequenceNode):
        found = [n for n in environment.value if isinstance(n, yaml.ScalarNode)
                 and n.value.split('=', 1)[0] == 'NODE_FUNCTION_ALLOW_EXTERNAL']
        require(len(found) == 1 and found[0].value == 'NODE_FUNCTION_ALLOW_EXTERNAL=' + OLD_ALLOW, 'ONE_LITERAL_ALLOWLIST_SCALAR')
    else:
        found = [field(environment, 'NODE_FUNCTION_ALLOW_EXTERNAL')]
        require(isinstance(found[0], yaml.ScalarNode) and found[0].value == OLD_ALLOW, 'LITERAL_ALLOWLIST_VALUE')
    scalar = found[0]
    old_scalar = text[scalar.start_mark.index:scalar.end_mark.index]
    require('\n' not in old_scalar and old_scalar.count(OLD_ALLOW) == 1, 'SINGLE_LINE_EXACT_ALLOWLIST')
    new_scalar = old_scalar.replace(OLD_ALLOW, NEW_ALLOW, 1)
    newline = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines(keepends=True)
    insertion = sum(len(line) for line in lines[:volumes.end_mark.line])
    mount_line = ' ' * volumes.start_mark.column + '- ' + str(MODULE) + ':' + TARGET + ':ro' + newline
    operations = [(scalar.start_mark.index, scalar.end_mark.index, old_scalar, new_scalar),
                  (insertion, insertion, '', mount_line)]
    candidate_text = text
    for start, end, old, new in sorted(operations, reverse=True):
        require(candidate_text[start:end] == old, 'EXACT_YAML_EDIT_SITE')
        candidate_text = candidate_text[:start] + new + candidate_text[end:]
    candidate = candidate_text.encode('utf-8')
    # Prove the two exact text operations invert; no YAML serialization.
    delta, positioned = 0, []
    for start, end, old, new in sorted(operations):
        positioned.append((start + delta, old, new)); delta += len(new) - (end - start)
    restored = candidate_text
    for at, old, new in reversed(positioned):
        require(restored[at:at + len(new)] == new, 'YAML_INVERSE_SITE')
        restored = restored[:at] + old + restored[at + len(new):]
    require(restored == text, 'EXACT_ORIGINAL_YAML_INVERSE')
    directory(STATE.parent, 0o700)
    directory(MODULE.parent, 0o755)
    require(not STATE.exists() and not MODULE.exists(), 'FRESH_PREPARATION_ONLY')
    STATE.mkdir(mode=0o700)
    os.chmod(STATE, 0o700)
    exclusive(STATE / 'original.yml', raw)
    exclusive(STATE / 'candidate.yml', candidate)
    added = compare_configs(before, docker.config(STATE / 'candidate.yml'))
    candidate_config_hash = docker.config_hash(STATE / 'candidate.yml')
    require(candidate_config_hash != source_config_hash, 'NEW_SERVICE_HASH_REQUIRED')
    MODULE.mkdir(mode=0o755)
    os.chmod(MODULE, 0o755)
    for name in PINS:
        content = (SOURCE / name).read_bytes()
        require(sha(content) == PINS[name], 'SOURCE_RECHECK_BEFORE_COPY')
        exclusive(MODULE / name, content, 0o444)
    verify_module(MODULE, PINS)
    verify_module(R03_MODULE, R03_PINS)
    require(COMPOSE.read_bytes() == raw and docker.inspect()['Id'] == current['Id'], 'PRODUCTION_UNCHANGED_DURING_PREPARE')
    manifest = {'schema': 'escalarai.static-body-persistent-r11-preparation.v1', 'state': 'PREPARED_NOT_APPLIED_NO_RESTART',
                'source_sha256': COMPOSE_SHA, 'candidate_sha256': sha(candidate), 'compose_path': str(COMPOSE),
                'compose_identity': [1001, 996, 0o644], 'container_id': current['Id'], 'image_id': IMAGE,
                'compose_image': before['services']['n8n']['image'], 'source_config_hash': source_config_hash,
                'candidate_config_hash': candidate_config_hash, 'module_path': str(MODULE), 'module_target': TARGET,
                'pins': PINS, 'r03_pins': R03_PINS, 'pack_manifest_sha256': PACK_SHA, 'native_gate': gate,
                'prepare_script_sha256': sha(Path(__file__).read_bytes()),
                'changes': {'allow_external_before': OLD_ALLOW, 'allow_external_after': NEW_ALLOW, 'read_only_mount': added},
                'other_services_and_configuration_identical': True, 'production_configuration_changed': False,
                'models': 0, 'workflow_activation': 0, 'ledger_writes': 0,
                'external_snapshot_boundary': 'ROOT captures all14 OFF workflows and complete ledger via API before/after separately.'}
    exclusive(STATE / 'manifest.json', json_bytes(manifest))
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
