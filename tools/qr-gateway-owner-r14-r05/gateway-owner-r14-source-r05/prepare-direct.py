"""R04 future prepare: ten immutable files + isolated check; no live cutover."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.dont_write_bytecode = True
import common as c
import direct_api as d


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--binding-file', required=True, type=Path)
    parser.add_argument('--binding-sha256', required=True)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    pack = c.load_pack(here, args.manifest_sha256)
    binding = d.load_binding(args.binding_file, args.binding_sha256, pack)
    c.local_docker_only(); c.private_directory(d.STATE.parent)
    c.need(not d.STATE.exists() and not d.MODULE.exists(), 'DIRECT_PREPARE_CREATE_ONLY')
    c.need(d.MODULE.parent.resolve(strict=True) == d.MODULE.parent
           and d.MODULE.parent.stat().st_uid == 0, 'ROOT_REAL_MODULE_PARENT')
    d.absence({d.CANDIDATE, d.BACKUP, d.FAILED})
    before = d.inspect(c.CONTAINER); d.verify_original(before, binding)
    c.need(c.dist_hashes(c.CONTAINER) == c.expected_dist(pack), 'ACTUAL_IMAGE_DIST_EQUALS_COMPILED_BASELINE')
    d.STATE.mkdir(mode=0o700)
    c.exclusive(d.STATE / 'container.before.json', c.encoded(before))
    c.exclusive(d.STATE / 'binding.json', c.encoded(binding))
    d.MODULE.mkdir(mode=0o755)
    for name in sorted(pack['runtime_dist']):
        c.exclusive(d.MODULE / name, (here / 'runtime' / name).read_bytes(), mode=0o444)
    os.chmod(d.MODULE, 0o555); d.verify_module(pack)
    d.isolated_proof(pack, args.manifest_sha256)
    fresh = d.inspect(c.CONTAINER); d.verify_original(fresh, binding)
    c.need(fresh['Config'] == before['Config'] and fresh['HostConfig'] == before['HostConfig']
           and fresh['Mounts'] == before['Mounts'] and fresh['NetworkSettings'] == before['NetworkSettings'], 'LIVE_CONTAINER_UNTOUCHED_DURING_PROOF')
    prepared = {'state': 'OWNER_R14_DIRECT_PREPARED_ISOLATED_PROOF_PASS_NOT_APPLIED',
                'manifest_sha256': args.manifest_sha256, 'binding_sha256': args.binding_sha256,
                'container_before': before['Id'], 'image_id': d.IMAGE, 'profile_sha256': binding['profile_sha256'],
                'before_sha256': c.sha((d.STATE / 'container.before.json').read_bytes()),
                'binding_state_sha256': c.sha((d.STATE / 'binding.json').read_bytes()),
                'runtime_files': 10, 'new_ro_binds': 10, 'image_builds': 0, 'live_restarts': 0,
                'isolated_check_runtime': True, 'isolated_container_removed': True,
                'models': 0, 'session_or_credential_files_written': False}
    c.exclusive(d.STATE / 'prepared.json', c.encoded(prepared)); print(json.dumps(prepared))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'state': 'OWNER_R14_DIRECT_PREPARE_FAILED', 'code': c.public_failure(str(error)), 'no_retry': True}))
        raise SystemExit(1)
