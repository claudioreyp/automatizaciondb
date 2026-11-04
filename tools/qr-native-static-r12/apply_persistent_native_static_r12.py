"""Source proposal: apply one reviewed R12 native-node preparation.

ROOT must separately authorize preparation and application after source review,
14 OFF workflow/closed-head39 API snapshots and real registry/pack pin binding.
Never activates workflows, changes financial state or invokes providers.
"""
import copy
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
from prepare_persistent_native_static_r12 import (
    COMPOSE, COMPOSE_SHA, ORIGINAL_CONTAINER, STATE, MODULE, TARGET, PINS,
    STATIC_MODULE, STATIC_PINS, R03_MODULE, R03_PINS, IMAGE, ALLOW, PACK_SHA,
    R12_MANIFEST_SHA, R12_REVIEW_SHA, APPLY_NAME, PREPARE_NAME, RELEASE,
    Docker, sha, json_bytes, require, regular, directory, exclusive, release_gate,
    verify_module, registry_contract, verify_release, native_gate, prior_gate,
    compare_configs, container_identity,
)


def mounts(values):
    fields = ('Type', 'Name', 'Source', 'Destination', 'Driver', 'Mode', 'RW', 'Propagation')
    return sorted([{key: value.get(key) for key in fields} for value in values], key=lambda value: value['Destination'])


def compare_runtime(before, after, added):
    require(not any(value.startswith('N8N_CUSTOM_EXTENSIONS=') for value in before['Config']['Env']), 'ORIGINAL_CUSTOM_EXTENSIONS_ABSENT')
    expected_env = before['Config']['Env'] + ['N8N_CUSTOM_EXTENSIONS=' + TARGET]
    require(sorted(after['Config']['Env']) == sorted(expected_env), 'ONLY_CUSTOM_EXTENSIONS_ENV_ADDED')
    require([value for value in after['Config']['Env'] if value.startswith('NODE_FUNCTION_ALLOW_EXTERNAL=')]
            == ['NODE_FUNCTION_ALLOW_EXTERNAL=' + ALLOW], 'ALLOWLIST_UNCHANGED')
    old_config, new_config = copy.deepcopy(before['Config']), copy.deepcopy(after['Config'])
    for value in (old_config, new_config):
        value.pop('Env', None)
        value.pop('Hostname', None)
        value.get('Labels', {}).pop('com.docker.compose.config-hash', None)
    require(old_config == new_config, 'OTHER_CONTAINER_CONFIG_UNCHANGED')
    old_host, new_host = copy.deepcopy(before['HostConfig']), copy.deepcopy(after['HostConfig'])
    old_binds, new_binds = old_host.pop('Binds', None), new_host.pop('Binds', None)
    require(old_host == new_host, 'ALL_HOST_SECURITY_NETWORK_RESOURCE_SETTINGS_UNCHANGED')
    expected_bind = str(MODULE) + ':' + TARGET + ':ro'
    require(new_binds is not None and new_binds.count(expected_bind) == 1, 'ONE_EXACT_NEW_BIND')
    remaining = new_binds.copy()
    remaining.remove(expected_bind)
    require(sorted(remaining) == sorted(old_binds or []), 'ALL_EXISTING_BINDS_UNCHANGED')
    found = [value for value in after['Mounts'] if value['Destination'] == TARGET]
    require(len(found) == 1 and found[0]['Type'] == 'bind' and found[0]['Source'] == str(MODULE)
            and found[0]['RW'] is False, 'ONE_RUNTIME_RO_NODE_MOUNT')
    require(mounts([value for value in after['Mounts'] if value['Destination'] != TARGET]) == mounts(before['Mounts']),
            'ALL_EXISTING_VOLUMES_UNCHANGED')
    require(after['NetworkSettings']['Networks']['n8n_default']['NetworkID']
            == before['NetworkSettings']['Networks']['n8n_default']['NetworkID'], 'SAME_NETWORK_ID')
    require(added['source'] == str(MODULE) and added['target'] == TARGET and added['read_only'] is True, 'APPROVED_NODE_MOUNT_BOUND')


def replace_compose(raw, expected, suffix, transaction=None):
    regular(COMPOSE, (1001, 996, 0o644))
    require(COMPOSE.read_bytes() == expected, 'CONCURRENT_COMPOSE_CHANGE_NO_OVERWRITE')
    temp = COMPOSE.parent / ('.qr-native-static-r12-' + suffix + '.tmp')
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
        transaction['compose_replaced'] = True
    parent_fd = os.open(str(COMPOSE.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def health(docker):
    until = min(docker.deadline, time.monotonic() + 45)
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


def existing_modules():
    verify_module(R03_MODULE, R03_PINS)
    verify_module(STATIC_MODULE, STATIC_PINS)


def main():
    release_gate()  # Closed before filesystem, Docker, health checks or mutation.
    require(os.geteuid() == 0, 'ROOT_REQUIRED')
    require(Path(__file__).resolve() == RELEASE / APPLY_NAME, 'RUN_ONLY_REVIEWED_PUBLIC_APPLIER')
    directory(STATE, 0o700)
    directory(COMPOSE.parent, 0o755)
    for name in ('manifest.json', 'original.yml', 'candidate.yml'):
        regular(STATE / name, (0, 0, 0o600))
    require(not (STATE / 'apply-start.json').exists() and not (STATE / 'apply-result.json').exists(), 'ONE_APPLICATION_ONLY')
    manifest = json.loads((STATE / 'manifest.json').read_bytes())
    require(manifest.get('schema') == 'escalarai.native-static-node-persistent-r12-preparation.v1'
            and manifest.get('state') == 'PREPARED_NOT_APPLIED_NO_RESTART', 'REVIEWED_PREPARATION_REQUIRED')
    require(manifest['source_sha256'] == COMPOSE_SHA and manifest['image_id'] == IMAGE
            and manifest['container_id'] == ORIGINAL_CONTAINER and manifest['pins'] == PINS
            and manifest['r03_pins'] == R03_PINS and manifest['static_pins'] == STATIC_PINS
            and manifest['pack_manifest_sha256'] == PACK_SHA and manifest['r12_manifest_sha256'] == R12_MANIFEST_SHA
            and manifest['r12_independent_review_sha256'] == R12_REVIEW_SHA
            and manifest['compose_path'] == str(COMPOSE) and manifest['module_path'] == str(MODULE)
            and manifest['module_target'] == TARGET and manifest['compose_identity'] == [1001, 996, 0o644], 'MANIFEST_CONSTANT_BINDINGS')
    helper = Path(__file__).with_name(PREPARE_NAME)
    regular(helper)
    require(sha(helper.read_bytes()) == manifest['prepare_script_sha256'], 'PREPARER_UNCHANGED')
    regular(Path(__file__))
    require(sha(Path(__file__).read_bytes()) == manifest['apply_script_sha256'], 'APPLIER_UNCHANGED_SINCE_PREPARATION')
    verify_release()
    gate = native_gate()
    require(gate == manifest['native_gate'] and prior_gate() == manifest['prior_gate'], 'ACTUAL_PROOFS_AND_PRIOR_STATE_UNCHANGED')
    verify_module(MODULE, PINS)
    registry_contract((MODULE / 'registry.json').read_bytes())
    existing_modules()
    original, candidate = (STATE / 'original.yml').read_bytes(), (STATE / 'candidate.yml').read_bytes()
    require(sha(original) == COMPOSE_SHA and sha(candidate) == manifest['candidate_sha256'], 'PREPARED_COMPOSE_PINS')
    regular(COMPOSE, (1001, 996, 0o644))
    require(COMPOSE.read_bytes() == original, 'CURRENT_COMPOSE_IS_EXACT_ORIGINAL')
    docker = Docker(240)
    before_config, after_config = docker.config(COMPOSE), docker.config(STATE / 'candidate.yml')
    added = compare_configs(before_config, after_config)
    require(manifest['changes'] == {'custom_extensions_before': None, 'custom_extensions_after': TARGET,
                                   'read_only_mount': added}, 'EXACT_TWO_ADDITIVE_CHANGES')
    source_hash, candidate_hash = docker.config_hash(COMPOSE), docker.config_hash(STATE / 'candidate.yml')
    require(source_hash == manifest['source_config_hash'] and candidate_hash == manifest['candidate_config_hash']
            and source_hash != candidate_hash, 'OFFICIAL_SERVICE_HASHES_UNCHANGED')
    before = docker.inspect()
    container_identity(before, source_hash)
    require(before['Id'] == ORIGINAL_CONTAINER and before['State']['Running'] is True, 'EXACT_ORIGINAL_RUNNING_CONTAINER')
    image = json.loads(docker.run(['docker', 'image', 'inspect', manifest['compose_image']]))[0]
    require(image['Id'] == IMAGE, 'LOCAL_IMAGE_UNCHANGED')
    exclusive(STATE / 'apply-start.json', json_bytes({'state': 'ONE_APPLY_STARTED', 'source_sha256': COMPOSE_SHA,
              'candidate_sha256': sha(candidate), 'original_container_id': ORIGINAL_CONTAINER, 'image_id': IMAGE,
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
        require(after['Id'] != ORIGINAL_CONTAINER, 'RECREATED_CONTAINER_ID_REQUIRED')
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
        registry_contract((MODULE / 'registry.json').read_bytes())
        existing_modules()
        result = {'state': 'NATIVE_STATIC_R12_APPLIED_HEALTHY', 'container_id': after['Id'], 'image_id': IMAGE,
                  'health_http': status, 'compose_sha256': sha(candidate), 'runtime_sha256': PINS['trusted-runtime.cjs'],
                  'registry_sha256': PINS['registry.json'], 'registry_entries': 102, 'registry_libraries': 2,
                  'task_timeout_ms': 300000, 'pack_manifest_sha256': PACK_SHA, 'r12_manifest_sha256': R12_MANIFEST_SHA,
                  'r12_independent_review_sha256': R12_REVIEW_SHA,
                  'custom_extensions': TARGET, 'allow_external': ALLOW, 'module_read_only': True,
                  'existing_R03_and_static_unchanged': True, 'all_existing_mounts_security_and_configuration_unchanged': True,
                  'networks': ['n8n_default'], 'models': 0, 'workflow_activation': 0, 'ledger_writes': 0,
                  'external_snapshot_boundary': manifest['external_snapshot_boundary'],
                  'agent_latency_acceptance': False, 'whole_agent_acceptance': False}
    except Exception as error:
        result = {'state': 'APPLICATION_FAILED_PRESERVED', 'stage': stage, 'error_class': type(error).__name__,
                  'error_code': str(error) if isinstance(error, RuntimeError) else 'UNCLASSIFIED_PRIVATE_OUTPUT_NOT_PRINTED',
                  'compose_was_replaced': transaction['compose_replaced'], 'models': 0, 'workflow_activation': 0, 'ledger_writes': 0}
        if transaction['compose_replaced']:
            try:
                rollback = Docker(150)
                regular(COMPOSE, (1001, 996, 0o644))
                require(COMPOSE.read_bytes() == candidate, 'ROLLBACK_REFUSES_CONCURRENT_COMPOSE_CHANGE')
                own = rollback.inspect()
                if own['Id'] == ORIGINAL_CONTAINER:
                    container_identity(own, source_hash)
                    original_runtime(before, own)
                else:
                    container_identity(own, candidate_hash)
                    require(observed_candidate is None or own['Id'] == observed_candidate, 'ROLLBACK_REFUSES_DIFFERENT_CONTAINER_ID')
                require(rollback.inspect()['Id'] == own['Id'], 'ROLLBACK_IDENTITY_RECHECK')
                replace_compose(original, candidate, 'rollback')
                up(rollback)
                result['rollback_health_http'] = health(rollback)
                restored = rollback.inspect()
                container_identity(restored, source_hash)
                require(restored['State']['Running'] is True and COMPOSE.read_bytes() == original, 'EXACT_RESTORED_COMPOSE_RUNNING')
                original_runtime(before, restored)
                existing_modules()
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
