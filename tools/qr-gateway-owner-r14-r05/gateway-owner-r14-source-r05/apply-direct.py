"""R04 future direct-container cutover. No image build and no compose assumptions."""
import argparse
import copy
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
import common as c
import direct_api as d


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args(); here = Path(__file__).resolve().parent
    pack = c.load_pack(here, args.manifest_sha256); c.local_docker_only(); c.private_directory(d.STATE)
    for name in ('prepared.json', 'container.before.json', 'binding.json'): c.regular(d.STATE / name, True)
    prepared = json.loads((d.STATE / 'prepared.json').read_bytes())
    binding = json.loads((d.STATE / 'binding.json').read_bytes())
    before = json.loads((d.STATE / 'container.before.json').read_bytes())
    c.need(prepared['state'] == 'OWNER_R14_DIRECT_PREPARED_ISOLATED_PROOF_PASS_NOT_APPLIED'
           and prepared['manifest_sha256'] == args.manifest_sha256
           and c.sha((d.STATE / 'container.before.json').read_bytes()) == prepared['before_sha256']
           and c.sha((d.STATE / 'binding.json').read_bytes()) == prepared['binding_state_sha256'], 'PINNED_DIRECT_PREPARATION')
    d.verify_module(pack); current = d.inspect(c.CONTAINER); d.verify_original(current, binding)
    c.need(all(current[k] == before[k] for k in ('Config', 'HostConfig', 'Mounts', 'NetworkSettings')), 'FRESH_DIRECT_CONFIG_EXACT')
    c.need(c.dist_hashes(c.CONTAINER) == c.expected_dist(pack), 'FRESH_BASELINE_DIST')
    d.absence({d.CANDIDATE, d.BACKUP, d.FAILED})
    payload = d.create_payload(before, pack, args.manifest_sha256)
    c.exclusive(d.STATE / 'apply-start.json', c.encoded({'state': 'CLAIMED_ONCE', 'manifest_sha256': args.manifest_sha256}))
    c.exclusive(d.STATE / 'candidate-create.private.json', c.encoded(payload))
    candidate_id = None; stop_attempted = False
    try:
        result = d.api('POST', '/containers/create?name=' + d.CANDIDATE, payload)
        candidate_id = result['Id']; c.need(d.re.fullmatch(r'[a-f0-9]{64}', candidate_id), 'CANDIDATE_ID')
        c.exclusive(d.STATE / 'candidate-created.json', c.encoded({'id': candidate_id}))
        stopped = d.inspect(candidate_id); d.verify_candidate(stopped, before, pack, args.manifest_sha256, False)
        d.verify_profile(binding)
        # Candidate remains STOPPED until the original has stopped. One owner.
        stop_attempted = True; d.action(before['Id'], 'stop')
        old = d.inspect(before['Id']); c.need(not old['State']['Running'], 'OLD_DIRECT_STOP_CONFIRMED')
        d.rename(before['Id'], d.BACKUP); d.rename(candidate_id, c.CONTAINER); d.action(candidate_id, 'start')
        after = d.inspect(c.CONTAINER); c.need(after['Id'] == candidate_id, 'EXACT_NEW_DIRECT_ID')
        d.verify_candidate(after, before, pack, args.manifest_sha256, True)
        d.verify_profile(binding); c.need(c.dist_hashes(c.CONTAINER) == c.expected_dist(pack, True), 'ENTIRE_CANDIDATE_DIST_EXACT')
        d.health(); old = d.inspect(d.BACKUP)
        c.need(old['Id'] == before['Id'] and not old['State']['Running']
               and all(old[k] == before[k] for k in ('Config', 'HostConfig', 'Mounts')), 'OLD_CONTAINER_PRESERVED_STOPPED')
        c.exclusive(d.STATE / 'container.after.json', c.encoded(after))
        answer = {'state': 'OWNER_R14_DIRECT_APPLIED_HEALTHY_PAUSED', 'manifest_sha256': args.manifest_sha256,
                  'image_id_unchanged': d.IMAGE, 'container_before': before['Id'], 'container_after': candidate_id,
                  'old_container_preserved_stopped': True, 'ten_additive_read_only_code_binds': True,
                  'env_argv_security_ports_network_session_and_credential_mounts_preserved': True,
                  'profile_sha256': binding['profile_sha256'], 'profile_enabled': False, 'health_http': 200,
                  'image_builds': 0, 'models': 0, 'provider_sends': 0, 'native_ack_acceptance': False}
        c.exclusive(d.STATE / 'apply-result.json', c.encoded(answer)); print(json.dumps(answer))
    except Exception as error:
        rollback = {'state': 'NO_LIVE_CHANGE', 'attempted': False}
        # Resolve lost create response from the exclusive name+label, without
        # ever starting an unknown container or repeating create/start/send.
        discovery = {'state': 'NOT_REQUIRED_KNOWN_CANDIDATE_ID'}
        if candidate_id is None:
            discovery = {'state': 'UNKNOWN_CREATE_PRESERVED_NO_RETRY'}
            try:
                rows = d.api('GET', '/containers/json?all=1')
                candidates = [r for r in rows if '/' + d.CANDIDATE in r['Names']]
                c.need(len(candidates) <= 1, 'UNIQUE_CANDIDATE_CREATE_DISCOVERY')
                if candidates:
                    own = d.inspect(candidates[0]['Id'])
                    if own['Image'] == d.IMAGE and own['Config']['Labels'].get(d.LABEL) == args.manifest_sha256:
                        candidate_id = own['Id']
                        discovery['state'] = 'OWN_CREATE_DISCOVERED_NOT_STARTED'
                else:
                    discovery['state'] = 'CREATE_NAME_CONFIRMED_ABSENT'
            except Exception:
                # Discovery must never suppress the result or known-ID rollback.
                discovery['state'] = 'UNKNOWN_CREATE_PRESERVED_NO_RETRY'
        if stop_attempted:
            rollback = {'state': 'PRESERVED_FOR_REVIEW', 'attempted': True}
            try:
                old = d.inspect(before['Id'])
                c.need(old['Image'] == d.IMAGE and all(old[k] == before[k] for k in ('Config', 'HostConfig', 'Mounts')), 'ROLLBACK_ORIGINAL_IDENTITY')
                if candidate_id:
                    own = d.inspect(candidate_id)
                    c.need(own['Id'] == candidate_id and own['Name'] in ('/' + d.CANDIDATE, '/' + c.CONTAINER), 'ROLLBACK_OWN_CANDIDATE_ID_NAME')
                    # This exact ID already passed STOPPED validation before any
                    # old stop. Require image/label/Config/HostConfig/mounts now,
                    # but not the network condition that may have caused failure.
                    d.verify_candidate(own, before, pack, args.manifest_sha256, own['State']['Running'], verify_network=False)
                    if own['State']['Running']: d.action(candidate_id, 'stop')
                    if own['Name'] == '/impulsa-qr': d.rename(candidate_id, d.FAILED)
                old = d.inspect(before['Id'])
                if old['Name'] == '/' + d.BACKUP: d.rename(before['Id'], c.CONTAINER)
                c.need(d.inspect(c.CONTAINER)['Id'] == before['Id'], 'ROLLBACK_OWN_ORIGINAL_NAME')
                if not d.inspect(before['Id'])['State']['Running']: d.action(before['Id'], 'start')
                restored = d.inspect(c.CONTAINER); d.verify_original(restored, binding)
                c.need(all(restored[k] == before[k] for k in ('Config', 'HostConfig', 'Mounts'))
                       and c.dist_hashes(c.CONTAINER) == c.expected_dist(pack), 'ROLLBACK_BASELINE_EXACT')
                d.health(); rollback['state'] = 'ORIGINAL_CONTAINER_ID_RESTORED_HEALTHY_PAUSED'
            except Exception:
                rollback['state'] = 'ROLLBACK_UNCONFIRMED_NO_RETRY'
        answer = {'state': 'OWNER_R14_DIRECT_APPLY_FAILED', 'code': c.public_failure(str(error)),
                  'rollback': rollback, 'candidate_id': candidate_id, 'candidate_discovery': discovery, 'no_retry': True}
        c.exclusive(d.STATE / 'apply-result.json', c.encoded(answer)); print(json.dumps(answer)); raise SystemExit(1)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'state': 'OWNER_R14_DIRECT_APPLY_PREFLIGHT_FAILED', 'code': c.public_failure(str(error)), 'no_retry': True}))
        raise SystemExit(1)
