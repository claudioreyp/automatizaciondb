"""One isolated native static-body workflow, never a production workflow.

ROOT must first materialize and review the public pack. Invocation requires its
exact manifest-byte SHA256. All subprocess output is saved before evaluation.
Only the newly created, ID/image/label-verified disposable container is removed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

IMAGE = 'sha256:4f448824ec99e1160e49eeb1c5bf2130a5d244fe9029e871a9f4d9f126dbfc98'
WORKFLOW_ID = 'QrStaticBodyNativeR0120261104'
PACKAGE = '@escalar/static-code-body-poc-r01'
R03_PACKAGE = '@escalar/qr-native-r03'
R03_INDEX_SHA = 'cc64011e21b074ae1fcad83909294fb8e200ab70712d872a898404a5fbf6ec92'
R03_PACKAGE_SHA = '7c3bc2f85ae6fe16a20eefa74569cdbdaf3eb319c8056ecf0b04b3e8607d5ea9'
FINAL_NODE = 'Static body native result'
CASE_IDS = [kind + ':' + variant for kind in ('text', 'png', 'ambient', 'nominal', 'kernel') for variant in ('original', 'module')]
FILES = {'probe/workflow.OFF.json', 'package/index.cjs', 'package/package.json', 'r03/index.cjs', 'r03/package.json', 'run-probe.py'}
RELEASE_PARENT = Path('/opt/escalar-qr/release/tools')
OUTPUT_PARENT = Path('/opt/escalar-qr')
ALLOWED_TYPES = {'n8n-nodes-base.manualTrigger', 'n8n-nodes-base.code', 'n8n-nodes-base.splitInBatches', 'n8n-nodes-base.if'}
EXTERNAL_ALLOW = R03_PACKAGE + ',' + PACKAGE
ENV = {
    'DB_TYPE': 'sqlite', 'DB_SQLITE_DATABASE': '/home/node/.n8n/database.sqlite',
    'N8N_USER_FOLDER': '/home/node', 'N8N_DEFAULT_BINARY_DATA_MODE': 'filesystem',
    'N8N_BINARY_DATA_STORAGE_PATH': '/home/node/.n8n/binaryData',
    'N8N_RUNNERS_ENABLED': 'true', 'N8N_RUNNERS_MODE': 'internal',
    'N8N_RUNNERS_INSECURE_MODE': 'false', 'N8N_RUNNERS_MAX_CONCURRENCY': '1',
    'N8N_RUNNERS_TASK_TIMEOUT': '60', 'NODE_FUNCTION_ALLOW_EXTERNAL': EXTERNAL_ALLOW,
    'N8N_DIAGNOSTICS_ENABLED': 'false', 'N8N_VERSION_NOTIFICATIONS_ENABLED': 'false',
    'N8N_PERSONALIZATION_ENABLED': 'false', 'N8N_LICENSE_AUTO_RENEW_ENABLED': 'false',
}


def require(ok, code):
    if not ok:
        raise RuntimeError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def explicitly_absent(result):
    return result.returncode != 0 and (b'No such object:' in result.stderr or b'No such container:' in result.stderr)


def contained_regular(root, relative):
    require(relative in FILES, 'UNREVIEWED_ARTIFACT_PATH')
    candidate = root / relative
    require(not candidate.is_symlink() and candidate.is_file(), 'ARTIFACT_REGULAR_FILE_REQUIRED')
    require(candidate.resolve(strict=True).is_relative_to(root), 'ARTIFACT_ESCAPES_RELEASE')
    for ancestor in candidate.parents:
        if ancestor == root:
            break
        require(not ancestor.is_symlink(), 'ARTIFACT_SYMLINK_PARENT')
    return candidate


def validate_response(final, manifest):
    require(isinstance(final, dict) and final.get('schema') == 'escalarai.static-body-native-result.v1', 'FINAL_SCHEMA')
    require(final.get('status') == 'PASS' and final.get('workflow_id') == WORKFLOW_ID, 'FINAL_ID_OR_STATUS')
    require(final.get('expected_cases') == 10 and final.get('case_ids') == CASE_IDS, 'FINAL_FINITE_CASE_IDS')
    require(final.get('module_package') == PACKAGE, 'FINAL_PACKAGE')
    require(all(final.get(key) is True for key in ('physical_storage_acceptance', 'nominal_pairing_acceptance', 'ambient_absence_acceptance', 'kernel_disabled_acceptance')), 'FINAL_ACCEPTANCE_FLAGS')
    require(all(type(final.get(key)) is int and final[key] == 0 for key in ('real_model_calls', 'real_pos_requests', 'data_table_requests', 'external_requests', 'production_changes')), 'FINAL_ZERO_IO')
    require(final.get('acceptance_scope') == 'ISOLATED_COMPONENTS_ONLY' and final.get('agent_latency_acceptance') is False and final.get('whole_agent_acceptance') is False, 'FINAL_SCOPE')
    rows = final.get('cases')
    require(isinstance(rows, list) and len(rows) == 10 and [row.get('case_id') for row in rows] == CASE_IDS, 'FINAL_EXACT_CASE_ROWS')
    for kind in ('text', 'png', 'ambient', 'nominal', 'kernel'):
        pair = [row for row in rows if row.get('case_name') == kind]
        require(len(pair) == 2 and [row.get('variant') for row in pair] == ['original', 'module'] and all(row.get('status') == 'PASS' for row in pair), 'FINAL_CASE_VARIANTS')
        require(pair[0].get('result') == pair[1].get('result'), 'FINAL_ORIGINAL_MODULE_DIFFERENCE_' + kind)
        if kind == 'png':
            for row in pair:
                require(len(row['result']) == 1, 'FINAL_PNG_RESULT_COUNT')
                physical = row['result'][0]
                require(physical.get('prepared_with_actual_helper') is True and physical.get('read_with_actual_helper') is True and physical.get('physical_storage') is True, 'FINAL_ACTUAL_BINARY_HELPERS')
                require(physical.get('storage_kind') == 'filesystem-v2' and physical.get('bytes') == 70 and physical.get('mime_type') == 'image/png', 'FINAL_PHYSICAL_BINARY')
                descriptor = row.get('binary_descriptor')
                require(isinstance(descriptor, dict) and descriptor.get('data') == 'filesystem-v2' and isinstance(descriptor.get('id'), str), 'FINAL_BINARY_DESCRIPTOR')
                require(re.fullmatch(r'filesystem-v2:workflows/' + WORKFLOW_ID + r'/executions/[1-9][0-9]*/binary_data/[a-f0-9-]{36}', descriptor['id']) is not None, 'FINAL_BINARY_PATH_SCOPE')
        elif kind == 'nominal':
            for row in pair:
                require(row.get('paired_items') == [{'item': 0}, {'item': 1}], 'FINAL_NATIVE_PAIRING')
                require([r.get('slot') for r in row['result']] == [0, 1] and [r.get('linked_slot') for r in row['result']] == [0, 1], 'FINAL_NOMINAL_ITEM_CALLBACK')
                require(all(r.get('first_slot') == 0 and r.get('all_slots') == [0, 1] for r in row['result']), 'FINAL_NOMINAL_FIRST_ALL_CALLBACK')
        elif kind == 'ambient':
            require(pair[0]['result'][0].get('types') == {'URL': 'undefined', 'fetch': 'undefined', 'AbortSignal': 'undefined'}, 'FINAL_AMBIENT_ABSENCE')
            reads = pair[0]['result'][0].get('reads')
            require(isinstance(reads, dict) and set(reads) == {'URL', 'fetch', 'AbortSignal'}, 'FINAL_AMBIENT_READ_NAMES')
            require(all(isinstance(value, dict) and (value.get('kind') == 'return' and value.get('type') == 'undefined' or value.get('kind') == 'throw' and value.get('name') == 'ReferenceError') for value in reads.values()), 'FINAL_AMBIENT_READ_BEHAVIOR')
        elif kind == 'kernel':
            kernel = pair[0]['result'][0]
            require(kernel.get('status') == 'DISABLED_UNBOUND_NO_EFFECTS' and kernel.get('native_enabled') is False and kernel.get('http_status') == 503 and kernel.get('provider_calls') == kernel.get('pos_writes') == 0, 'FINAL_CURRENT_KERNEL_DISABLED')
    require(manifest['expected_case_ids'] == CASE_IDS, 'FINAL_MANIFEST_CASE_BINDING')


def run(args):
    require(re.fullmatch(r'[a-f0-9]{64}', args.manifest_sha256) is not None, 'MANIFEST_BYTE_DIGEST_REQUIRED')
    require(re.fullmatch(r'escalar-static-body-native-r01-[a-z0-9][a-z0-9-]{0,47}', args.container_name) is not None, 'OWN_CONTAINER_NAME_REQUIRED')
    release_input = Path(args.release_dir)
    require(release_input.is_absolute() and not release_input.is_symlink(), 'RELEASE_ABSOLUTE_DIRECTORY_REQUIRED')
    release = release_input.resolve(strict=True)
    require(release.is_dir() and release.parent == RELEASE_PARENT.resolve(strict=True) and release.name == 'qr-static-body-native-r01', 'REVIEWED_RELEASE_SCOPE')
    require(Path(__file__).resolve(strict=True) == release / 'run-probe.py', 'REVIEWED_RUNNER_PATH_REQUIRED')
    out_input = Path(args.output_dir)
    require(out_input.is_absolute() and out_input.parent.resolve(strict=True) == OUTPUT_PARENT.resolve(strict=True)
            and re.fullmatch(r'static-body-native-r01-[a-z0-9][a-z0-9-]{0,47}', out_input.name) is not None, 'OUTPUT_SCOPE')
    out_input.mkdir(mode=0o700, exist_ok=False)
    out = out_input.resolve(strict=True)
    deadline = time.monotonic() + 240
    sequence = 0

    def save(name, value):
        require('/' not in name and '\\' not in name, 'RESULT_BASENAME_REQUIRED')
        (out / name).write_bytes(json_bytes(value))

    def command(label, command_args, limit=25, cleanup=False):
        nonlocal sequence
        sequence += 1
        require(re.fullmatch(r'[a-z0-9-]+', label) is not None, 'RAW_COMMAND_LABEL')
        prefix = f'{sequence:02d}-{label}'
        remaining = limit if cleanup else min(limit, deadline - time.monotonic())
        require(remaining > 0, 'DEADLINE_NO_NEW_COMMAND')
        save(prefix + '.command.json', {'argv': command_args, 'timeout_seconds': remaining, 'cleanup': cleanup})
        try:
            result = subprocess.run(command_args, capture_output=True, timeout=remaining)
        except subprocess.TimeoutExpired as error:
            (out / (prefix + '.stdout.raw')).write_bytes(error.stdout or b'')
            (out / (prefix + '.stderr.raw')).write_bytes(error.stderr or b'')
            save(prefix + '.exit.json', {'exit_code': 'TIMEOUT_UNKNOWN'})
            raise
        (out / (prefix + '.stdout.raw')).write_bytes(result.stdout)
        (out / (prefix + '.stderr.raw')).write_bytes(result.stderr)
        save(prefix + '.exit.json', {'exit_code': result.returncode})
        require(len(result.stdout) <= 32 * 1024 * 1024 and len(result.stderr) <= 4 * 1024 * 1024, 'RAW_OUTPUT_BOUND')
        return result

    def checked(label, command_args, limit=25):
        result = command(label, command_args, limit)
        require(result.returncode == 0, 'COMMAND_EXIT_' + label + '_' + str(result.returncode))
        return result.stdout

    failure = None
    creation_requested = False
    created_id = None
    cleanup_state = 'NOT_CREATED'
    cleanup_error = None
    final = None
    exit_code = None
    manifest = None
    try:
        manifest_file = release / 'manifest.json'
        require(manifest_file.is_file() and not manifest_file.is_symlink(), 'REGULAR_MANIFEST_REQUIRED')
        manifest_bytes = manifest_file.read_bytes()
        require(len(manifest_bytes) <= 256 * 1024 and digest(manifest_bytes) == args.manifest_sha256, 'EXACT_REVIEWED_MANIFEST_REQUIRED')
        manifest = json.loads(manifest_bytes)
        save('reviewed-manifest.json', manifest)
        require(manifest.get('schema') == 'escalarai.static-body-native-pack.v1' and manifest.get('state') == 'PREPARED_NOT_EXECUTED', 'MANIFEST_SCHEMA')
        require(manifest.get('expected_image_id') == IMAGE and manifest.get('workflow_id') == WORKFLOW_ID and manifest.get('final_node_name') == FINAL_NODE, 'MANIFEST_IDENTITY')
        require(manifest.get('expected_cases') == 10 and manifest.get('expected_case_ids') == CASE_IDS, 'MANIFEST_FINITE_CASES')
        require(manifest.get('module_package') == PACKAGE and manifest.get('r03_package') == R03_PACKAGE, 'MANIFEST_PACKAGES')
        require(manifest.get('financial_ledger_fixture') is False and manifest.get('private_credentials') is False and manifest.get('production_changes') == 0, 'PUBLIC_FIXTURES_ONLY')
        require(manifest.get('body_copies') == 108 and type(manifest.get('unique_bodies')) is int and manifest['unique_bodies'] >= 91, 'CURRENT_AND_FIXTURE_BODY_COUNTS')
        require(isinstance(manifest.get('files'), dict) and set(manifest['files']) == FILES and set(manifest.get('file_bytes', {})) == FILES, 'EXACT_ARTIFACT_INVENTORY')
        for name, expected in manifest['files'].items():
            require(isinstance(expected, str) and re.fullmatch(r'[a-f0-9]{64}', expected) is not None, 'ARTIFACT_DIGEST')
            artifact = contained_regular(release, name)
            raw = artifact.read_bytes()
            require(len(raw) == manifest['file_bytes'][name] and len(raw) <= 16 * 1024 * 1024 and digest(raw) == expected, 'ARTIFACT_PIN_' + name)
        for directory, expected_names in {'probe': {'workflow.OFF.json'}, 'package': {'index.cjs', 'package.json'}, 'r03': {'index.cjs', 'package.json'}}.items():
            actual = list((release / directory).iterdir())
            require({p.name for p in actual} == expected_names and all(p.is_file() and not p.is_symlink() for p in actual), 'MOUNT_DIRECTORY_INVENTORY_' + directory)
        require(manifest['files']['r03/index.cjs'] == R03_INDEX_SHA and manifest['files']['r03/package.json'] == R03_PACKAGE_SHA, 'R03_EXACT_PUBLIC_PACKAGE')
        require(manifest['module_sha256'] == manifest['files']['package/index.cjs'] and manifest['module_bytes'] == manifest['file_bytes']['package/index.cjs'], 'STATIC_PACKAGE_HASH')
        for directory, package_name in [('r03', R03_PACKAGE), ('package', PACKAGE)]:
            package_json = json.loads((release / directory / 'package.json').read_bytes())
            require(package_json.get('name') == package_name and package_json.get('type') == 'commonjs' and package_json.get('main') == 'index.cjs' and package_json.get('private') is True, 'PACKAGE_ENTRYPOINT')
        graph = json.loads((release / 'probe/workflow.OFF.json').read_bytes())
        require(graph.get('id') == WORKFLOW_ID and graph.get('active') is False and graph.get('pinData') == {} and graph.get('staticData') == {}, 'PUBLIC_OFF_WORKFLOW')
        require(isinstance(graph.get('nodes'), list) and len(graph['nodes']) == manifest['node_count'] and len(graph['nodes']) <= 50, 'FINITE_NODE_COUNT')
        require(len({n['id'] for n in graph['nodes']}) == len(graph['nodes']) and len({n['name'] for n in graph['nodes']}) == len(graph['nodes']), 'UNIQUE_NODE_IDS_AND_NAMES')
        require(all(node.get('type') in ALLOWED_TYPES and not node.get('credentials') for node in graph['nodes']), 'NO_EXTERNAL_NODES_OR_CREDENTIALS')
        require(sum(n['type'] == 'n8n-nodes-base.manualTrigger' for n in graph['nodes']) == 1 and sum(n['name'] == FINAL_NODE for n in graph['nodes']) == 1, 'SINGLE_MANUAL_AND_FINAL_NODE')
        require(graph['settings'].get('executionTimeout') == 180, 'WORKFLOW_EXECUTION_BOUND')
        # Only immutable image identity and running state are requested. No
        # production environment, credentials, logs, database or volumes read.
        production = checked('production-image-only', ['docker', 'inspect', '--format', '{{.Image}}|{{.State.Running}}', 'n8n-n8n-1']).decode('ascii').strip()
        require(production == IMAGE + '|true', 'EXACT_RUNNING_PRODUCTION_IMAGE')
        absent = command('own-name-absence', ['docker', 'container', 'inspect', '--format', '{{.Id}}', args.container_name])
        require(explicitly_absent(absent), 'OWN_NAME_MUST_BE_CONFIRMED_ABSENT')
        script = 'n8n user-management:reset && n8n import:workflow --input /probe/workflow.OFF.json && exec n8n execute --id ' + WORKFLOW_ID + ' --rawOutput'
        binds = {
            '/probe': str(release / 'probe'),
            '/usr/local/lib/node_modules/n8n/node_modules/' + R03_PACKAGE: str(release / 'r03'),
            '/usr/local/lib/node_modules/n8n/node_modules/' + PACKAGE: str(release / 'package'),
        }
        docker = ['docker', 'create', '--name', args.container_name, '--label', 'escalar.probe=' + args.container_name,
                  '--label', 'escalar.manifest-sha256=' + args.manifest_sha256, '--network', 'none', '--read-only', '--user', '1000:1000',
                  '--memory', '768m', '--cpus', '1', '--pids-limit', '128', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                  '--tmpfs', '/home/node/.n8n:rw,uid=1000,gid=1000,mode=700', '--tmpfs', '/tmp:rw,uid=1000,gid=1000,mode=700']
        for destination, source in binds.items():
            docker += ['--mount', 'type=bind,src=' + source + ',dst=' + destination + ',readonly']
        for key, value in ENV.items():
            docker += ['--env', key + '=' + value]
        docker += ['--entrypoint', 'sh', IMAGE, '-c', script]
        creation_requested = True
        created_id = checked('create-own-container', docker).decode('ascii').strip()
        require(re.fullmatch(r'[a-f0-9]{64}', created_id) is not None, 'EXACT_CREATED_CONTAINER_ID')
        owned = json.loads(checked('inspect-own-before-start', ['docker', 'container', 'inspect', created_id]))[0]
        require(owned['Id'] == created_id and owned['Image'] == IMAGE and owned['Config']['Labels'].get('escalar.probe') == args.container_name
                and owned['Config']['Labels'].get('escalar.manifest-sha256') == args.manifest_sha256, 'OWN_CONTAINER_IDENTITY')
        host = owned['HostConfig']
        require(host['NetworkMode'] == 'none' and host['ReadonlyRootfs'] is True and host['PortBindings'] in (None, {}) and not host.get('VolumesFrom'), 'ISOLATED_CONTAINER_NETWORK_AND_ROOT')
        require(owned['Config']['User'] == '1000:1000' and host['Memory'] == 768 * 1024 * 1024 and host['NanoCpus'] == 1000000000 and host['PidsLimit'] == 128, 'CONTAINER_RESOURCE_AND_USER_LIMITS')
        require(host.get('CapDrop') == ['ALL'] and 'no-new-privileges:true' in host.get('SecurityOpt', []), 'CONTAINER_SECURITY_LIMITS')
        actual_binds = [mount for mount in owned['Mounts'] if mount['Type'] == 'bind']
        require(len(actual_binds) == 3 and all(m['RW'] is False for m in actual_binds)
                and {m['Destination']: m['Source'] for m in actual_binds} == binds, 'EXACT_THREE_READONLY_BIND_SOURCES')
        require(all(m['Type'] == 'bind' or m['Type'] == 'tmpfs' and m['Destination'] in {'/home/node/.n8n', '/tmp'} for m in owned['Mounts']), 'NO_OTHER_VOLUMES')
        require(set(host['Tmpfs']) == {'/home/node/.n8n', '/tmp'}, 'EXACT_FRESH_TMPFS')
        env_pairs = [entry.split('=', 1) for entry in owned['Config']['Env']]
        env = dict(env_pairs)
        require(len(env) == len(env_pairs) and all(env.get(key) == value for key, value in ENV.items()), 'EXACT_SYNTHETIC_RUNTIME_ENVIRONMENT')
        save('preflight.json', {'image_id': IMAGE, 'manifest_sha256': args.manifest_sha256, 'pins': manifest['files'], 'binds': binds,
                               'network': 'none', 'readonly': True, 'user': '1000:1000', 'fresh_sqlite_and_binary_tmpfs': True,
                               'production_volumes': 0, 'production_environment_copied': False, 'models': 0})
        native = command('native-one-execution', ['docker', 'start', '-a', created_id], 180)
        exit_code = native.returncode
        # Explicit stable raw names are useful when reviewing startup/JSON errors.
        (out / 'stdout.raw').write_bytes(native.stdout)
        (out / 'stderr.raw').write_bytes(native.stderr)
        require(exit_code == 0, 'NATIVE_EXECUTION_EXIT')
        text = native.stdout.decode('utf-8')
        decoder, decoded = json.JSONDecoder(), []
        for offset, character in enumerate(text):
            if character != '{' or offset != 0 and text[offset - 1] != '\n':
                continue
            try:
                value, _ = decoder.raw_decode(text, offset)
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get('data'), dict) and 'resultData' in value['data']:
                decoded.append(value)
        require(len(decoded) == 1, 'ONE_RAW_EXECUTION_OBJECT')
        raw_execution = decoded[0]
        save('execution.raw.json', raw_execution)
        result_data = raw_execution['data']['resultData']
        require(not result_data.get('error'), 'NATIVE_EXECUTION_ERROR')
        executions = result_data['runData'][FINAL_NODE]
        require(len(executions) == 1, 'ONE_FINAL_NODE_EXECUTION')
        items = executions[0]['data']['main'][0]
        require(len(items) == 1, 'ONE_FINAL_RESULT_ITEM')
        final = items[0]['json']
        save('response.json', final)
        validate_response(final, manifest)
    except Exception as error:
        failure = {'type': type(error).__name__, 'code': str(error) if isinstance(error, RuntimeError) else 'UNCLASSIFIED_PRESERVED_RAW'}
        if isinstance(error, subprocess.TimeoutExpired):
            exit_code = 'TRANSPORT_TIMEOUT_UNKNOWN'
            (out / 'stdout.raw').write_bytes(error.stdout or b'')
            (out / 'stderr.raw').write_bytes(error.stderr or b'')
    finally:
        try:
            if created_id and re.fullmatch(r'[a-f0-9]{64}', created_id):
                inspected = command('cleanup-inspect-own-id', ['docker', 'container', 'inspect', created_id], 5, cleanup=True)
                if inspected.returncode == 0:
                    own = json.loads(inspected.stdout)[0]
                    require(own['Id'] == created_id and own['Image'] == IMAGE and own['Config']['Labels'].get('escalar.probe') == args.container_name
                            and own['Config']['Labels'].get('escalar.manifest-sha256') == args.manifest_sha256, 'CLEANUP_ID_LABEL_IMAGE_REQUIRED')
                    removed = command('cleanup-remove-own-id', ['docker', 'rm', '-f', created_id], 10, cleanup=True)
                    verify = command('cleanup-confirm-own-absence', ['docker', 'container', 'inspect', created_id], 5, cleanup=True)
                    cleanup_state = 'REMOVED_SYNTHETIC_CONTAINER_AND_TMPFS' if removed.returncode == 0 and explicitly_absent(verify) else 'CLEANUP_UNCERTAIN'
                elif explicitly_absent(inspected):
                    cleanup_state = 'CONFIRMED_CREATED_CONTAINER_ALREADY_ABSENT'
                else:
                    cleanup_state = 'CLEANUP_INSPECTION_UNCERTAIN'
            elif creation_requested:
                cleanup_state = 'CREATE_OUTCOME_UNCERTAIN_NO_DELETE_BY_NAME'
        except Exception as error:
            cleanup_state = 'CLEANUP_FAILED_PRESERVED'
            cleanup_error = {'type': type(error).__name__, 'code': str(error) if isinstance(error, RuntimeError) else 'UNCLASSIFIED_PRESERVED_RAW'}
        report = {'state': 'NATIVE_STATIC_BODY_COMPONENTS_PASS' if final and final.get('status') == 'PASS' and not failure
                  and cleanup_state in ('REMOVED_SYNTHETIC_CONTAINER_AND_TMPFS', 'CONFIRMED_CREATED_CONTAINER_ALREADY_ABSENT') else 'FAIL_PRESERVED_NO_RETRY',
                  'failure': failure, 'cleanup_error': cleanup_error, 'exit_code': exit_code, 'cleanup': cleanup_state,
                  'image_id': IMAGE, 'manifest_sha256': args.manifest_sha256, 'response_saved': final is not None,
                  'production_changes': 0, 'models': 0, 'network': 'none', 'agent_latency_acceptance': False, 'whole_agent_acceptance': False}
        save('result.json', report)
        print(json.dumps(report))
    return 0 if report['state'] == 'NATIVE_STATIC_BODY_COMPONENTS_PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--container-name', required=True)
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as error:
        print(json.dumps({'state': 'PREPARATION_FAIL_BEFORE_CONTAINER', 'failure_type': type(error).__name__,
                          'code': str(error) if isinstance(error, RuntimeError) else 'PREPARATION_UNCLASSIFIED',
                          'models': 0, 'production_changes': 0, 'retry': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
