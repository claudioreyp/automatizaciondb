"""One finite, public, networkless native-node qualification; no production state."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import time

IMAGE = 'sha256:4f448824ec99e1160e49eeb1c5bf2130a5d244fe9029e871a9f4d9f126dbfc98'
NAME = 'escalar-native-static-node-operational-r01'
LABEL = 'escalar.native-static-proof'
PAYLOAD_SHA = '2af131367430e540d9401f5e7c115307a242347e8039aa1294a6a920048d819f'
RELEASE = Path('/opt/escalar-qr/release/tools/qr-native-static-node-operational-r01')
OUTPUT = Path('/opt/escalar-qr/native-static-node-operational-r01-result')
PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=='


def require(ok, code):
    if not ok:
        raise RuntimeError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def absent(result, target):
    message = result.stderr.decode('utf-8').strip().lower()
    return result.returncode == 1 and result.stdout.strip() in (b'', b'[]') and message in {
        'error: no such object: ' + target, 'error: no such container: ' + target,
        'error response from daemon: no such container: ' + target}



def execution(raw):
    text = raw.decode('utf-8')
    decoder, values = json.JSONDecoder(), []
    for match in re.finditer(r'(?m)^\{', text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
            if isinstance(obj, dict) and isinstance(obj.get('data'), dict) and isinstance(obj['data'].get('resultData'), dict):
                values.append(obj)
        except json.JSONDecodeError:
            pass
    require(len(values) == 1, 'ONE_SAVED_EXECUTION_REQUIRED')
    return values[0]


def rows_at(result, name):
    visits = result['data']['resultData'].get('runData', {}).get(name)
    require(isinstance(visits, list) and len(visits) > 0, 'PROOF_NODE_NOT_VISITED_' + name)
    return visits, [item for visit in visits for branch in visit.get('data', {}).get('main', []) if branch for item in branch]


def normalize(value):
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: ('OWN_WORKFLOW' if key == 'workflow_id' else 'OWN_EXECUTION' if key == 'execution_id' else normalize(item)) for key, item in value.items()}
    return value


def component(result, spec):
    visits, items = rows_at(result, spec['proof_node_name'])
    error = result['data']['resultData'].get('error')
    kind = spec['case_name']
    if kind == 'malformed':
        errors = [visit.get('error') for visit in visits if visit.get('error')]
        require(error and errors, 'EXPECTED_OUTPUT_VALIDATION_ERROR_REQUIRED')
        message = errors[-1].get('message', '')
        require("A 'json' property isn't an object" in message, 'EXACT_OFFICIAL_OUTPUT_ERROR_REQUIRED')
        return {'expected_error': message, 'description': errors[-1].get('description')}
    require(not error and all(not visit.get('error') for visit in visits), 'UNEXPECTED_COMPONENT_ERROR_' + kind)
    if kind == 'null':
        require(len(visits) == 1 and visits[0].get('data', {}).get('main') == [[]]
                and isinstance(visits[0].get('executionTime'), (int, float)) and items == [], 'NULL_COMPLETE_EMPTY_BRANCH_REQUIRED')
        return []
    require(items and all(isinstance(item.get('json'), dict) for item in items), 'COMPONENT_OUTPUT_OBJECTS')
    data = [item['json'] for item in items]
    if kind in ('text', 'ambient'):
        require(len(data) == 1 and data[0]['case_name'] == kind, 'CALLBACK_CASE')
        row = data[0]
        require(row['result']['text'] == 'Synthetic á / 2 items' and row['result']['workflow_id'] == spec['workflow_id'], 'CALLBACK_TEXT_OR_WORKFLOW')
        require(re.fullmatch(r'[1-9][0-9]*', row['result']['execution_id']) is not None, 'ACTUAL_EXECUTION_ID')
        require(row['types'] == {'URL': 'undefined', 'fetch': 'undefined', 'AbortSignal': 'undefined'}, 'AMBIENT_ABSENCE')
        require(set(row['reads']) == {'URL', 'fetch', 'AbortSignal'}, 'AMBIENT_READ_NAMES')
        require(all(value.get('kind') == 'throw' and value.get('name') == 'ReferenceError' for value in row['reads'].values()), 'AMBIENT_READ_REFERENCE_ERRORS')
    elif kind == 'isolation':
        require(data == [{'case_name': 'isolation', 'input_changed': 999, 'query_changed': 888}], 'ISOLATION_MUTATION_EXERCISED')
        _, verified = rows_at(result, 'Check source snapshot unchanged')
        require([row['json'] for row in verified] == data, 'SOURCE_ISOLATION_VERIFIER_NOT_COMPLETE')
    elif kind == 'nominal':
        require([row['slot'] for row in data] == [0, 1] and [row['linked_slot'] for row in data] == [0, 1], 'NOMINAL_PAIRED_CALLBACK')
        require(all(row['first_slot'] == 0 and row['all_slots'] == [0, 1] for row in data), 'NOMINAL_FIRST_ALL_CALLBACK')
        pairing = [item.get('pairedItem') for item in items]
        require(all(isinstance(value, (dict, list)) for value in pairing), 'NOMINAL_PAIRED_METADATA_REQUIRED')
        return {'data': data, 'paired_items': pairing}
    elif kind == 'png':
        require(len(data) == 1, 'ONE_PHYSICAL_PNG')
        row, descriptor = data[0], data[0]['descriptor']
        require(row['bytes'] == 70 and row['base64'] == PNG, 'EXACT_PHYSICAL_PNG_BYTES')
        require(row['workflow_id'] == spec['workflow_id'] and re.fullmatch(r'[1-9][0-9]*', row['execution_id']) is not None, 'BINARY_EXECUTION_SCOPE')
        require(descriptor.get('data') == 'filesystem-v2' and descriptor.get('mimeType') == 'image/png' and descriptor.get('fileName') == 'native-static.png', 'ACTUAL_FILESYSTEM_BINARY_DESCRIPTOR')
        pattern = r'filesystem-v2:workflows/' + re.escape(spec['workflow_id']) + r'/executions/' + row['execution_id'] + r'/binary_data/[a-f0-9-]{36}'
        require(re.fullmatch(pattern, descriptor.get('id', '')) is not None, 'BINARY_DESCRIPTOR_OWN_WORKFLOW_EXECUTION')
        # Normalize only after validating the entire physical descriptor scope.
        descriptor['id'] = 'OWN_FILESYSTEM_BINARY_ID'
    elif kind == 'kernel':
        require(len(data) == 1, 'ONE_DISABLED_KERNEL')
        row = data[0]
        require(row.get('status') == 'DISABLED_UNBOUND_NO_EFFECTS' and row.get('native_enabled') is False and row.get('http_status') == 503, 'ACTUAL_DISABLED_KERNEL')
        require(row.get('provider_calls') == row.get('pos_writes') == 0, 'KERNEL_ZERO_EFFECTS')
    else:
        raise RuntimeError('UNREVIEWED_COMPONENT')
    return normalize(data)


def run(args):
    require(re.fullmatch(r'[a-f0-9]{64}', args.manifest_sha256) is not None, 'MANIFEST_PIN_REQUIRED')
    require(Path(__file__).resolve(strict=True) == RELEASE / 'run-proof.py' and not RELEASE.is_symlink(), 'REVIEWED_RUNNER_PATH')
    require(not OUTPUT.exists(), 'FRESH_RESULT_DIRECTORY_REQUIRED')
    OUTPUT.mkdir(mode=0o700)
    started = time.monotonic()
    deadline, sequence, created_id = started + 180, 0, None
    cleanup, cleanup_error, failure = 'NOT_CREATED', None, None
    components, benchmarks, metadata = [], [], None
    creation_requested = False
    ownership_nonce = secrets.token_hex(16)

    def save(name, value):
        require('/' not in name and '\\' not in name, 'RESULT_BASENAME')
        (OUTPUT / name).write_bytes(encoded(value))

    def command(label, argv, seconds=25, final=False):
        nonlocal sequence
        sequence += 1
        require(re.fullmatch(r'[a-z0-9-]+', label) is not None, 'COMMAND_LABEL')
        prefix = f'{sequence:03d}-{label}'
        # Reserve forty seconds for the three owned-container cleanup commands.
        limit = min(seconds, (deadline if final else deadline - 40) - time.monotonic())
        require(limit > 0, 'TOTAL_DEADLINE_EXHAUSTED')
        save(prefix + '.command.json', {'argv': argv, 'timeout_seconds': limit, 'cleanup': final})
        start = time.monotonic()
        try:
            result = subprocess.run(argv, capture_output=True, timeout=limit)
        except subprocess.TimeoutExpired as error:
            (OUTPUT / (prefix + '.stdout.raw')).write_bytes(error.stdout or b'')
            (OUTPUT / (prefix + '.stderr.raw')).write_bytes(error.stderr or b'')
            save(prefix + '.exit.json', {'exit_code': 'TIMEOUT_UNKNOWN', 'wall_ms': (time.monotonic() - start) * 1000})
            raise
        (OUTPUT / (prefix + '.stdout.raw')).write_bytes(result.stdout)
        (OUTPUT / (prefix + '.stderr.raw')).write_bytes(result.stderr)
        wall_ms = (time.monotonic() - start) * 1000
        save(prefix + '.exit.json', {'exit_code': result.returncode, 'wall_ms': wall_ms})
        require(len(result.stdout) <= 64 * 1024 * 1024 and len(result.stderr) <= 4 * 1024 * 1024, 'OUTPUT_BOUND')
        return result, wall_ms

    def checked(label, argv, seconds=25):
        result, _ = command(label, argv, seconds)
        require(result.returncode == 0, 'COMMAND_FAILED_' + label)
        return result.stdout

    def owned(inspected):
        require(inspected['Id'] == created_id and inspected['Image'] == IMAGE, 'OWN_ID_IMAGE')
        labels = inspected['Config']['Labels']
        require(labels.get(LABEL) == NAME and labels.get('escalar.manifest-sha256') == args.manifest_sha256, 'OWN_LABELS')
        require(labels.get('escalar.ownership-nonce') == ownership_nonce, 'OWN_FRESH_NONCE')

    try:
        raw = (RELEASE / 'manifest.json').read_bytes()
        require(sha(raw) == args.manifest_sha256 and len(raw) < 128 * 1024, 'OUTER_MANIFEST_PIN')
        outer = json.loads(raw)
        require(outer['schema'] == 'escalarai.native-static-node-vps-pack.v1' and len(outer['files']) == 13, 'OUTER_MANIFEST_SCHEMA')
        actual = {str(p.relative_to(RELEASE)).replace('\\', '/') for p in RELEASE.rglob('*') if p.is_file()}
        require(actual == set(outer['files']) | {'manifest.json'} and not any(p.is_symlink() for p in RELEASE.rglob('*')), 'EXACT_PUBLIC_INVENTORY_NO_LINKS')
        for name, pin in outer['files'].items():
            require(re.fullmatch(r'(?:node|fixtures|static|r03|workflows)/[A-Za-z0-9_.-]+|payload-manifest.json|run-proof.py', name) is not None, 'PUBLIC_PATH')
            path = RELEASE / name
            require(path.resolve(strict=True).is_relative_to(RELEASE), 'PUBLIC_PATH_SCOPE')
            data = path.read_bytes()
            require(len(data) == pin['bytes'] and sha(data) == pin['sha256'], 'PUBLIC_FILE_PIN_' + name)
        payload_raw = (RELEASE / 'payload-manifest.json').read_bytes()
        require(sha(payload_raw) == PAYLOAD_SHA, 'FROZEN_REVIEWED_PAYLOAD')
        metadata = json.loads(payload_raw)
        require(metadata['expected_image_id'] == IMAGE and metadata['total_wall_deadline_seconds'] == 180 and metadata['total_workflow_executions'] == 2, 'FINITE_PROOF_MANIFEST')
        require(len(metadata['components']) == 2 and len(metadata['benchmark_runs']) == 0 and metadata['workflow_package_version'] == '2.14.1', 'PROOF_CASES_AND_DEPENDENCY')
        require(all(outer['files'][name] == pin for name, pin in metadata['files'].items()), 'INNER_OUTER_PINS')
        graphs = {}
        allowed = {'n8n-nodes-base.manualTrigger', 'n8n-nodes-base.code', 'n8n-nodes-base.if', 'n8n-nodes-base.splitInBatches', 'CUSTOM.escalarStaticBody'}
        for name in metadata['execution_order']:
            graph = json.loads((RELEASE / name).read_bytes())
            require(graph['active'] is False and graph['pinData'] == {} and graph['staticData'] == {}, 'OFF_PUBLIC_GRAPH')
            require(all(node['type'] in allowed or node.get('disabled') is True for node in graph['nodes']), 'NO_ACTIVE_EXTERNAL_NODE')
            require(all(not node.get('credentials') for node in graph['nodes']), 'NO_CREDENTIALS')
            require(0 < graph['settings']['executionTimeout'] <= 60, 'WORKFLOW_DEADLINE')
            graphs[name] = graph
        production = checked('production-image-only', ['docker', 'container', 'inspect', '--format', '{{.Image}}|{{.State.Running}}', 'n8n-n8n-1']).decode().strip()
        require(production == IMAGE + '|true', 'RUNNING_IMAGE_IDENTITY')
        missing, _ = command('own-name-absence', ['docker', 'container', 'inspect', '--format', '{{.Id}}', NAME])
        require(absent(missing, NAME), 'OWN_NAME_NOT_ABSENT')
        binds = {'/proof': str(RELEASE / 'workflows')}
        for directory, package in metadata['mounts'].items():
            binds['/usr/local/lib/node_modules/n8n/node_modules/' + package] = str(RELEASE / directory)
        env = {'DB_TYPE': 'sqlite', 'DB_SQLITE_DATABASE': '/home/node/.n8n/database.sqlite', 'N8N_USER_FOLDER': '/home/node',
               'N8N_DEFAULT_BINARY_DATA_MODE': 'filesystem', 'N8N_BINARY_DATA_STORAGE_PATH': '/home/node/.n8n/binaryData',
               'N8N_RUNNERS_ENABLED': 'true', 'N8N_RUNNERS_MODE': 'internal', 'N8N_RUNNERS_INSECURE_MODE': 'false',
               'N8N_RUNNERS_MAX_CONCURRENCY': '1', 'N8N_RUNNERS_TASK_TIMEOUT': '300',
               'NODE_FUNCTION_ALLOW_EXTERNAL': ','.join(metadata['baseline_external_allow']),
               'N8N_CUSTOM_EXTENSIONS': metadata['native_custom_extensions'],
               'N8N_DIAGNOSTICS_ENABLED': 'false', 'N8N_VERSION_NOTIFICATIONS_ENABLED': 'false',
               'N8N_PERSONALIZATION_ENABLED': 'false', 'N8N_LICENSE_AUTO_RENEW_ENABLED': 'false'}
        save('ownership-intent.json', {'name': NAME, 'nonce': ownership_nonce, 'manifest_sha256': args.manifest_sha256, 'image_id': IMAGE})
        argv = ['docker', 'create', '--name', NAME, '--label', LABEL + '=' + NAME, '--label', 'escalar.manifest-sha256=' + args.manifest_sha256,
                '--label', 'escalar.ownership-nonce=' + ownership_nonce,
                '--network', 'none', '--read-only', '--user', '1000:1000', '--memory', '768m', '--cpus', '1', '--pids-limit', '128',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '--tmpfs', '/home/node/.n8n:rw,uid=1000,gid=1000,mode=700', '--tmpfs', '/tmp:rw,uid=1000,gid=1000,mode=700']
        for dst, src in binds.items():
            argv += ['--mount', 'type=bind,src=' + src + ',dst=' + dst + ',readonly']
        for key, value in env.items():
            argv += ['--env', key + '=' + value]
        argv += ['--entrypoint', 'node', IMAGE, '-e', 'setInterval(()=>{},1000)']
        creation_requested = True
        created_id = checked('create-own', argv).decode().strip()
        require(re.fullmatch(r'[a-f0-9]{64}', created_id) is not None, 'CREATED_ID')
        inspection = json.loads(checked('inspect-own', ['docker', 'container', 'inspect', created_id]))[0]
        owned(inspection)
        host = inspection['HostConfig']
        require(host['NetworkMode'] == 'none' and host['ReadonlyRootfs'] is True and not host['PortBindings'] and not host.get('VolumesFrom'), 'ISOLATION')
        require(inspection['Config']['User'] == '1000:1000' and host['Memory'] == 768 * 1024 * 1024 and host['NanoCpus'] == 1000000000 and host['PidsLimit'] == 128, 'RESOURCE_LIMITS')
        require(host['CapDrop'] == ['ALL'] and 'no-new-privileges:true' in host['SecurityOpt'], 'SECURITY')
        mounted = [row for row in inspection['Mounts'] if row['Type'] == 'bind']
        require(len(mounted) == 4 and {row['Destination']: row['Source'] for row in mounted} == binds and all(row['RW'] is False for row in mounted), 'EXACT_READONLY_MOUNTS')
        require(all(row['Type'] == 'bind' or row['Type'] == 'tmpfs' and row['Destination'] in {'/tmp', '/home/node/.n8n'} for row in inspection['Mounts']), 'NO_EXISTING_VOLUMES')
        require(set(host['Tmpfs']) == {'/tmp', '/home/node/.n8n'}, 'FRESH_TMPFS_ONLY')
        actual_env = dict(row.split('=', 1) for row in inspection['Config']['Env'])
        require(all(actual_env.get(key) == value for key, value in env.items()) and actual_env.get('NODE_PATH') == '/opt/nodejs/node-v24.13.1/lib/node_modules', 'RUNTIME_ENV')
        save('preflight.json', {'manifest_sha256': args.manifest_sha256, 'image_id': IMAGE, 'network': 'none', 'binds': binds, 'models': 0, 'production_changes': 0})
        checked('start-own', ['docker', 'start', created_id])
        version = checked('installed-workflow-version', ['docker', 'exec', created_id, 'node', '-p', "require('/usr/local/lib/node_modules/n8n/node_modules/n8n-workflow/package.json').version"]).decode().strip()
        require(version == '2.14.1', 'ACTUAL_WORKFLOW_PACKAGE_VERSION')
        checked('initialize-fresh-sqlite', ['docker', 'exec', created_id, 'n8n', 'user-management:reset'], 40)
        for index, filename in enumerate(metadata['execution_order']):
            graph = graphs[filename]
            checked('import-' + str(index), ['docker', 'exec', created_id, 'n8n', 'import:workflow', '--input=/proof/' + Path(filename).name], 30)
            spec = next((row for row in metadata['components'] if row['file'] == filename), None)
            bench = next((row for row in metadata['benchmark_runs'] if row['file'] == filename), None)
            require((spec is None) != (bench is None), 'ONE_EXECUTION_SPEC')
            if bench:
                remote = '/tmp/benchmark-' + bench['variant'] + '.json'
                checked('export-' + bench['variant'], ['docker', 'exec', created_id, 'n8n', 'export:workflow', '--id=' + graph['id'], '--output=' + remote], 30)
                readback_raw = checked('readback-' + bench['variant'], ['docker', 'exec', created_id, 'cat', remote])
                exported = json.loads(readback_raw)
                require(len(exported) == 1, 'ONE_IMPORTED_BENCHMARK')
                for key in ('id', 'name', 'active', 'nodes', 'connections', 'settings', 'pinData', 'staticData'):
                    require(exported[0].get(key) == graph.get(key), 'IMPORTED_GRAPH_DIFFERENCE_' + key)
                save('benchmark-' + bench['variant'] + '-readback-pin.json', {'sha256': sha(readback_raw), 'source_sha256': sha((RELEASE / filename).read_bytes())})
            raw_result, wall_ms = command('execute-' + str(index), ['docker', 'exec', created_id, 'n8n', 'execute', '--id=' + graph['id'], '--rawOutput'], graph['settings']['executionTimeout'] + 15)
            result = execution(raw_result.stdout)
            save(f'execution-{index:02d}.json', result)
            if spec:
                require(raw_result.returncode == 0 or spec['case_name'] == 'malformed' and raw_result.returncode == 1, 'UNEXPECTED_COMPONENT_CLI_EXIT')
                proof = component(result, spec)
                components.append({**spec, 'status': 'PASS', 'proof': proof, 'cli_exit_code': raw_result.returncode, 'wall_ms': wall_ms})
                save('components-progress.json', components)
            else:
                require(not result['data']['resultData'].get('error') and raw_result.returncode == 0, 'BENCHMARK_EXECUTION_FAILED')
                _, final_items = rows_at(result, bench['final_node_name'])
                require([row['json'] for row in final_items] == [metadata['benchmark_final_schema']], 'FINITE_BENCHMARK_FINAL')
                measured = next(node for node in graph['nodes'] if node['parameters'].get('jsCode', '').startswith('return await require(') and not node.get('disabled'))
                visits, _ = rows_at(result, measured['name'])
                require(len(visits) == 180 and all(not row.get('error') and isinstance(row.get('executionTime'), (int, float)) for row in visits), 'EXACT_180_MEASURED_VISITS')
                benchmarks.append({'variant': bench['variant'], 'wall_ms': wall_ms, 'measured_hops': 180, 'execution_time_sum_ms': sum(row['executionTime'] for row in visits),
                                   'first_start_ms': min(row['startTime'] for row in visits), 'last_end_ms': max(row['startTime'] + row['executionTime'] for row in visits),
                                   'definition_bytes': len(json.dumps(graph, ensure_ascii=False, separators=(',', ':')).encode()), 'history_bytes': 204541, 'kind': 'SIZE_MATCHED_SYNTHETIC_METADATA'})
                save('benchmarks-progress.json', benchmarks)
        require(len(components) == 2 and len(benchmarks) == 0, 'FINITE_COUNTS')
        for kind in ('kernel',):
            pair = [row for row in components if row['case_name'] == kind]
            require(len(pair) == 2 and pair[0]['proof'] == pair[1]['proof'], 'CODE_NATIVE_DIFFERENCE_' + kind)
    except Exception as error:
        failure = {'type': type(error).__name__, 'message': str(error)}
    finally:
        if creation_requested:
            try:
                target = created_id or NAME
                inspected, _ = command('cleanup-inspect', ['docker', 'container', 'inspect', target], 10, final=True)
                if absent(inspected, target):
                    cleanup = 'CONFIRMED_ABSENT'
                else:
                    require(inspected.returncode == 0, 'CLEANUP_INSPECTION')
                    info = json.loads(inspected.stdout)[0]
                    if created_id is None:
                        created_id = info['Id']
                    owned(info)
                    removed, _ = command('cleanup-remove-owned-id', ['docker', 'rm', '-f', created_id], 15, final=True)
                    require(removed.returncode == 0, 'REMOVE_OWN_CONTAINER')
                    gone, _ = command('cleanup-absence', ['docker', 'container', 'inspect', created_id], 10, final=True)
                    require(absent(gone, created_id), 'OWN_CONTAINER_STILL_PRESENT')
                    cleanup = 'REMOVED_OWN_CONTAINER_AND_TMPFS'
            except Exception as error:
                cleanup_error = {'type': type(error).__name__, 'message': str(error)}
        final = {'schema': 'escalarai.native-static-node-two-library-result.v1', 'status': 'PASS' if failure is None and cleanup_error is None else 'FAIL',
                 'failure': failure, 'cleanup_error': cleanup_error, 'cleanup': cleanup, 'components': len(components), 'benchmarks': len(benchmarks),
                 'models': 0, 'production_changes': 0, 'network': 'none', 'image_id': IMAGE, 'manifest_sha256': args.manifest_sha256,
                 'benchmark_metrics': benchmarks, 'wall_ms_including_cleanup': (time.monotonic() - started) * 1000,
                 'execution_identity_scope': 'Manifest workflow IDs and own execution-context IDs; PNG descriptor matches both. No independent CLI execution-ID field or database identity read.',
                 'whole_agent_acceptance': False, 'agent_latency_acceptance': False}
        if len(benchmarks) == 2:
            original, native = benchmarks
            final['measured_node_time_ratio_native_over_code'] = native['execution_time_sum_ms'] / max(1, original['execution_time_sum_ms'])
        save('result.json', final)
        print(json.dumps(final, ensure_ascii=False))
    return 0 if final['status'] == 'PASS' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest-sha256', required=True)
    raise SystemExit(run(parser.parse_args()))
