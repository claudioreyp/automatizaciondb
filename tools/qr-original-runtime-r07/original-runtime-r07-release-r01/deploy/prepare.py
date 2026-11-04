"""Future VPS preparation only: inspect/archive/plan. No build, stop or start.

Do not run until ROOT supplies reviewed archived-R12 evidence and authorizes it.
"""
import argparse
import json
import os
import stat
from pathlib import Path
from deploy_common import *


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--closure-gate', required=True)
    parser.add_argument('--closure-sha256', required=True)
    args = parser.parse_args()
    root_only()
    manifest = verify_pack()
    gate = closure_gate(args.closure_gate, args.closure_sha256)
    need(not PRIVATE.exists() and not RUN.exists(), 'ONE_SHOT_PREPARATION_PATHS_MUST_BE_NEW')
    existing = names()
    need(not set(PRESERVED.values()) & existing, 'PRESERVED_NAMES_ALREADY_EXIST')
    need(not set(IMAGES.values()) & set(command(['docker', 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}}']).decode().splitlines()),
         'R07_IMAGE_TAG_ALREADY_EXISTS')
    base = base_image()
    old = {kind: inspect(kind) for kind in (BACKEND, PROXY)}
    for kind, obj in old.items():
        need(obj['State']['Running'] is True and not obj['State']['Paused'], 'CURRENT_FIXTURE_NOT_RUNNING')
        security(obj, kind)
    backend = old[BACKEND]
    need(backend['Image'] == BASE_ID and backend['Config']['Image'] == BASE_TAG
         and backend['Config']['Entrypoint'] == ENTRY
         and backend['Config']['Env'] == base['Config']['Env'], 'R05_CURRENT_BASE_CONFIG')
    need(len(backend['Mounts']) == 1, 'R05_EXACT_BINDING_MOUNT')
    mount = backend['Mounts'][0]
    need(mount['Type'] == 'bind' and mount['Source'] == str(OLD_RUN)
         and mount['Destination'] == '/run/eval' and mount['RW'] is True, 'R05_EXACT_BINDING_MOUNT')
    need(old[PROXY]['Mounts'] == [], 'PROXY_NO_EXISTING_MOUNTS')
    need(inspect(INTERNAL)['Internal'] is True, 'EXISTING_INTERNAL_NETWORK_REQUIRED')
    need(inspect(N8N)['Internal'] is False, 'EXISTING_N8N_NETWORK_REQUIRED')
    existing_networks(old)
    binding = regular(OLD_RUN / 'binding.json', 1024 * 1024)
    # This private file is archived, never printed or copied into the public pack.
    PRIVATE.mkdir(mode=0o700)
    need(PRIVATE.stat().st_uid == 0 and stat.S_IMODE(PRIVATE.stat().st_mode) == 0o700,
         'PRIVATE_ARCHIVE_DIRECTORY')
    archive = []
    def save(name, raw):
        private_bytes(PRIVATE / name, raw)
        archive.append({'archive_file': name, 'sha256': sha(raw), 'bytes': len(raw)})
    save('r05-binding.private.json', binding)
    save('r05-containers.private.json', (json.dumps(old, indent=2) + '\n').encode())
    save('archived-r12-gate.json', regular(args.closure_gate))
    save('archived-r12-local-artifact-references.json',
         (json.dumps(gate['artifacts'],indent=2)+'\n').encode())
    plan = {'schema': 'escalarai.original-r07-deployment-plan.v1', 'state': 'PREPARED_NOT_APPLIED',
            'sourcepack_manifest_sha256': MANIFEST_SHA, 'local_results_sha256': RESULTS_SHA,
            'base_image_id': BASE_ID, 'api_revision': REVISION, 'api_files': manifest['api_files'],
            'closure_gate_path': str(Path(args.closure_gate)), 'closure_gate_sha256': args.closure_sha256,
            'old_container_ids': {kind: old[kind]['Id'] for kind in old},
            'old_binding_sha256': sha(binding), 'archive': archive,
            'proposal_script_pins': source_script_pins(), 'new_images': IMAGES,
            'new_binding': str(RUN / 'binding.json'),
            'required_binding_identity': {'uid': 10001, 'gid': 10001, 'file_mode': '0600', 'parent_mode': '0700'},
            'old_fixture_memory_state_restorable': False,
            'snapshot_scope': gate['snapshot_scope'], 'database_backup': False,
            'archive_location':'user_workspace','payload_bytes_copied_to_vps':False,
            'reversal_scope': 'configuration_only_preserved_containers_stopped_no_old_case_replay',
            'build_flags': ['--pull=false', '--network=none'],
            'controller_or_credential_actions': 0, 'production_changes': 0}
    digest = private_json(PRIVATE / 'plan.json', plan)
    print(json.dumps({'state': plan['state'], 'plan_path': str(PRIVATE / 'plan.json'),
                      'plan_sha256': digest, 'archive_files': len(archive),
                      'runtime_changes': 0, 'builds': 0, 'credentials_changed': 0}))


if __name__ == '__main__':
    safe_main(main)
