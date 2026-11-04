"""Apply a reviewed preparation once, preserving image, storage and security.

This script is prepared locally only. ROOT must separately authorize its eventual
VPS invocation after the native proof and the14-flow/ledger API snapshot. It never
activates workflows or calls providers. On failure it restores only its own exact
candidate, bound to the original or official candidate compose container hash.
"""
import copy
import json
import os
from pathlib import Path
import stat
import time
import urllib.error
import urllib.request
from prepare_persistent_static_body_r11 import (
    COMPOSE, COMPOSE_SHA, STATE, MODULE, TARGET, R03_MODULE, R03_PINS, PINS,
    IMAGE, OLD_ALLOW, NEW_ALLOW, PACK_SHA, Docker, sha, json_bytes, require,
    regular, directory, exclusive, verify_module, verify_release, native_gate,
    compare_configs, container_identity,
)


def mounts(values):
    fields = ('Type', 'Name', 'Source', 'Destination', 'Driver', 'Mode', 'RW', 'Propagation')
    return sorted([{key: v.get(key) for key in fields} for v in values], key=lambda v: v['Destination'])


def compare_runtime(before, after, added):
    expected_env = ['NODE_FUNCTION_ALLOW_EXTERNAL=' + NEW_ALLOW if v == 'NODE_FUNCTION_ALLOW_EXTERNAL=' + OLD_ALLOW else v
                    for v in before['Config']['Env']]
    require(before['Config']['Env'].count('NODE_FUNCTION_ALLOW_EXTERNAL=' + OLD_ALLOW) == 1, 'ONE_ORIGINAL_ALLOWLIST')
    require(sorted(after['Config']['Env']) == sorted(expected_env), 'ONLY_ALLOWLIST_ENV_CHANGE')
    old_config, new_config = copy.deepcopy(before['Config']), copy.deepcopy(after['Config'])
    for value in (old_config, new_config):
        value.pop('Env', None)
        value.pop('Hostname', None)  # Docker assigns this from the new container ID.
        value.get('Labels', {}).pop('com.docker.compose.config-hash', None)
    require(old_config == new_config, 'OTHER_CONTAINER_CONFIG_UNCHANGED')
    # Preserve every HostConfig field after subtracting precisely the new bind.
    old_host, new_host = copy.deepcopy(before['HostConfig']), copy.deepcopy(after['HostConfig'])
    old_binds, new_binds = old_host.pop('Binds', None), new_host.pop('Binds', None)
    require(old_host == new_host, 'ALL_HOST_SECURITY_NETWORK_RESOURCE_SETTINGS_UNCHANGED')
    expected_bind = str(MODULE) + ':' + TARGET + ':ro'
    require(new_binds is not None and new_binds.count(expected_bind) == 1, 'ONE_EXACT_NEW_BIND')
    remaining = new_binds.copy(); remaining.remove(expected_bind)
    require(sorted(remaining) == sorted(old_binds or []), 'ALL_EXISTING_BINDS_UNCHANGED')
    found = [v for v in after['Mounts'] if v['Destination'] == TARGET]
    require(len(found) == 1 and found[0]['Type'] == 'bind' and found[0]['Source'] == str(MODULE)
            and found[0]['RW'] is False, 'ONE_RUNTIME_RO_MODULE_MOUNT')
    require(mounts([v for v in after['Mounts'] if v['Destination'] != TARGET]) == mounts(before['Mounts']),
            'ALL_EXISTING_VOLUMES_UNCHANGED')
    require(after['NetworkSettings']['Networks']['n8n_default']['NetworkID']
            == before['NetworkSettings']['Networks']['n8n_default']['NetworkID'], 'SAME_NETWORK_ID')
    require(added['source'] == str(MODULE) and added['target'] == TARGET and added['read_only'] is True, 'APPROVED_MOUNT_BOUND')


def replace_compose(raw, expected, suffix, transaction=None):
    regular(COMPOSE, (1001, 996, 0o644))
    require(COMPOSE.read_bytes() == expected, 'CONCURRENT_COMPOSE_CHANGE_NO_OVERWRITE')
    temp = COMPOSE.parent / ('.qr-static-body-r11-' + suffix + '.tmp')
    fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fchown(stream.fileno(), 1001, 996)
        os.fchmod(stream.fileno(), 0o644)
        os.fsync(stream.fileno())
    regular(COMPOSE, (1001, 996, 0o644))
    require(COMPOSE.read_bytes() == expected, 'CONCURRENT_COMPOSE_CHANGE_BEFORE_RENAME')
    os.replace(temp, COMPOSE)
    if transaction is not None:
        # Track the rename before a later directory-fsync failure can raise.
        transaction['compose_replaced'] = True
    parent_fd = os.open(str(COMPOSE.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def health(docker):
    until = min(docker.deadline, time.monotonic() + 45)
    # Never use a configured external HTTP proxy for the local health endpoint.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < until:
        try:
            with opener.open('http://127.0.0.1:5678/healthz', timeout=min(2, max(0.1, until - time.monotonic()))) as response:
                if response.status == 200:
                    return 200
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(min(1, max(0, until - time.monotonic())))
    raise RuntimeError('LOCAL_HEALTH_NOT_READY')


def up(docker):
    docker.run(docker.compose(COMPOSE) + ['up', '-d', '--no-deps', '--pull', 'never', 'n8n'], 60)


def original_runtime(before, restored):
    old_config, new_config = copy.deepcopy(before['Config']), copy.deepcopy(restored['Config'])
    for value in (old_config, new_config):
        value.pop('Hostname', None)
    require(old_config == new_config and before['HostConfig'] == restored['HostConfig'], 'RESTORED_FULL_CONTAINER_CONFIGURATION')
    require(mounts(before['Mounts']) == mounts(restored['Mounts']), 'RESTORED_ALL_MOUNTS')
    require(restored['NetworkSettings']['Networks']['n8n_default']['NetworkID']
            == before['NetworkSettings']['Networks']['n8n_default']['NetworkID'], 'RESTORED_SAME_NETWORK')


def main():
    require(os.geteuid() == 0, 'ROOT_REQUIRED')
    directory(STATE, 0o700)
    directory(COMPOSE.parent, 0o755)
    for name in ('manifest.json', 'original.yml', 'candidate.yml'):
        regular(STATE / name, (0, 0, 0o600))
    require(not (STATE / 'apply-start.json').exists() and not (STATE / 'apply-result.json').exists(), 'ONE_APPLICATION_ONLY')
    manifest = json.loads((STATE / 'manifest.json').read_bytes())
    require(manifest.get('schema') == 'escalarai.static-body-persistent-r11-preparation.v1'
            and manifest.get('state') == 'PREPARED_NOT_APPLIED_NO_RESTART', 'REVIEWED_PREPARATION_REQUIRED')
    require(manifest['source_sha256'] == COMPOSE_SHA and manifest['image_id'] == IMAGE
            and manifest['pins'] == PINS and manifest['r03_pins'] == R03_PINS
            and manifest['pack_manifest_sha256'] == PACK_SHA
            and manifest['compose_path'] == str(COMPOSE) and manifest['module_path'] == str(MODULE)
            and manifest['module_target'] == TARGET and manifest['compose_identity'] == [1001, 996, 0o644], 'MANIFEST_CONSTANT_BINDINGS')
    helper = Path(__file__).with_name('prepare_persistent_static_body_r11.py')
    regular(helper)
    require(sha(helper.read_bytes()) == manifest['prepare_script_sha256'], 'PREPARER_UNCHANGED')
    verify_release()
    gate = manifest['native_gate']
    require(native_gate(Path(gate['result_path']), gate['result_sha256']) == gate, 'NATIVE_PROOF_UNCHANGED')
    verify_module(MODULE, PINS)
    verify_module(R03_MODULE, R03_PINS)
    original, candidate = (STATE / 'original.yml').read_bytes(), (STATE / 'candidate.yml').read_bytes()
    require(sha(original) == COMPOSE_SHA and sha(candidate) == manifest['candidate_sha256'], 'PREPARED_COMPOSE_PINS')
    regular(COMPOSE, (1001, 996, 0o644))
    require(COMPOSE.read_bytes() == original, 'CURRENT_COMPOSE_IS_EXACT_ORIGINAL')
    docker = Docker(240)
    before_config, after_config = docker.config(COMPOSE), docker.config(STATE / 'candidate.yml')
    added = compare_configs(before_config, after_config)
    require(manifest['changes'] == {'allow_external_before': OLD_ALLOW, 'allow_external_after': NEW_ALLOW,
                                   'read_only_mount': added}, 'EXACT_TWO_APPROVED_CHANGES')
    source_hash, candidate_hash = docker.config_hash(COMPOSE), docker.config_hash(STATE / 'candidate.yml')
    require(source_hash == manifest['source_config_hash'] and candidate_hash == manifest['candidate_config_hash']
            and source_hash != candidate_hash, 'OFFICIAL_SERVICE_HASHES_UNCHANGED')
    before = docker.inspect()
    container_identity(before, source_hash)
    require(before['Id'] == manifest['container_id'] and before['State']['Running'] is True, 'EXACT_ORIGINAL_RUNNING_CONTAINER')
    image = json.loads(docker.run(['docker', 'image', 'inspect', manifest['compose_image']]))[0]
    require(image['Id'] == IMAGE, 'LOCAL_IMAGE_UNCHANGED')
    exclusive(STATE / 'apply-start.json', json_bytes({'state': 'ONE_APPLY_STARTED', 'source_sha256': COMPOSE_SHA,
              'candidate_sha256': sha(candidate), 'original_container_id': before['Id'], 'image_id': IMAGE,
              'apply_script_sha256': sha(Path(__file__).read_bytes()), 'native_gate': gate}))
    transaction, stage, observed_candidate = {'compose_replaced': False}, 'before_write', None
    try:
        stage = 'replace_compose'
        replace_compose(candidate, original, 'apply', transaction)
        stage = 'compose_up'
        up(docker)
        stage = 'bind_new_container_identity'
        after = docker.inspect()
        container_identity(after, candidate_hash)
        require(after['Id'] != before['Id'], 'RECREATED_CONTAINER_ID_REQUIRED')
        observed_candidate = after['Id']
        stage = 'health'
        status = health(docker)
        stage = 'postconditions'
        after = docker.inspect()
        container_identity(after, candidate_hash)
        require(after['Id'] == observed_candidate and after['State']['Running'] is True, 'SAME_NEW_RUNNING_CONTAINER')
        compare_runtime(before, after, added)
        regular(COMPOSE, (1001, 996, 0o644))
        require(COMPOSE.read_bytes() == candidate, 'EXACT_APPLIED_COMPOSE')
        verify_module(MODULE, PINS)
        verify_module(R03_MODULE, R03_PINS)
        result = {'state': 'STATIC_BODY_R11_APPLIED_HEALTHY', 'container_id': after['Id'], 'image_id': IMAGE,
                  'health_http': status, 'compose_sha256': sha(candidate), 'module_sha256': PINS['index.cjs'],
                  'package_json_sha256': PINS['package.json'], 'pack_manifest_sha256': PACK_SHA,
                  'allow_external': NEW_ALLOW, 'module_read_only': True, 'existing_R03_unchanged': True,
                  'all_existing_mounts_security_and_configuration_unchanged': True, 'networks': ['n8n_default'],
                  'models': 0, 'workflow_activation': 0, 'ledger_writes': 0,
                  'external_snapshot_boundary': manifest['external_snapshot_boundary'],
                  'agent_latency_acceptance': False, 'whole_agent_acceptance': False}
    except Exception as error:
        result = {'state': 'APPLICATION_FAILED_PRESERVED', 'stage': stage, 'error_class': type(error).__name__,
                  'error_code': str(error) if isinstance(error, RuntimeError) else 'UNCLASSIFIED_PRIVATE_OUTPUT_NOT_PRINTED',
                  'compose_was_replaced': transaction['compose_replaced'], 'models': 0, 'workflow_activation': 0, 'ledger_writes': 0}
        if transaction['compose_replaced']:
            try:
                # A fresh deadline is reserved for restoration even if application timed out.
                rollback = Docker(150)
                regular(COMPOSE, (1001, 996, 0o644))
                require(COMPOSE.read_bytes() == candidate, 'ROLLBACK_REFUSES_CONCURRENT_COMPOSE_CHANGE')
                own = rollback.inspect()
                if own['Id'] == before['Id']:
                    container_identity(own, source_hash)
                    original_runtime(before, own)
                else:
                    container_identity(own, candidate_hash)
                    require(observed_candidate is None or own['Id'] == observed_candidate, 'ROLLBACK_REFUSES_DIFFERENT_CONTAINER_ID')
                    observed_candidate = own['Id']
                require(rollback.inspect()['Id'] == own['Id'], 'ROLLBACK_IDENTITY_RECHECK')
                replace_compose(original, candidate, 'rollback')
                up(rollback)
                result['rollback_health_http'] = health(rollback)
                restored = rollback.inspect()
                container_identity(restored, source_hash)
                require(restored['State']['Running'] is True and COMPOSE.read_bytes() == original, 'EXACT_RESTORED_COMPOSE_RUNNING')
                original_runtime(before, restored)
                verify_module(R03_MODULE, R03_PINS)
                result['state'] = 'APPLICATION_FAILED_ROLLED_BACK_HEALTHY'
                result['restored_container_id'] = restored['Id']
                result['restored_compose_sha256'] = COMPOSE_SHA
            except Exception as rollback_error:
                result['rollback_error_class'] = type(rollback_error).__name__
                result['rollback_error_code'] = str(rollback_error) if isinstance(rollback_error, RuntimeError) else 'UNCLASSIFIED_PRIVATE_OUTPUT_NOT_PRINTED'
        exclusive(STATE / 'apply-result.json', json_bytes(result))
        print(json.dumps(result))
        raise SystemExit(1)
    exclusive(STATE / 'apply-result.json', json_bytes(result))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
