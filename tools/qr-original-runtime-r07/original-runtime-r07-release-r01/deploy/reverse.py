"""Future explicit configuration-only reversal; no old case/process restart."""
import argparse
import json
from deploy_common import *


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--reversal-closure-gate', required=True)
    parser.add_argument('--reversal-closure-sha256', required=True)
    parser.add_argument('--execute-reviewed-reversal', action='store_true', required=True)
    args = parser.parse_args()
    root_only()
    plan = read_plan(args.plan, args.plan_sha256)
    need((PRIVATE / 'apply-started.json').is_file(), 'APPLY_NEVER_STARTED')
    need(not (PRIVATE / 'explicit-reversal-started.json').exists(), 'NO_REVERSAL_RETRY')
    gate = closure_gate(args.reversal_closure_gate, args.reversal_closure_sha256, phase='r07')
    private_json(PRIVATE / 'explicit-reversal-started.json', {'state': 'ONE_SHOT_REVERSAL_STARTED'})
    private_bytes(PRIVATE / 'reversal-closed-r07-gate.private.json', regular(args.reversal_closure_gate))
    private_json(PRIVATE / 'reversal-local-artifact-references.json',gate['artifacts'])
    if (RUN / 'binding.json').exists():
        private_bytes(PRIVATE / 'reversal-r07-binding.private.json', regular(RUN / 'binding.json'))
    private_json(PRIVATE / 'reversal-current-containers.private.json',
                 {kind: inspect(kind) for kind in (BACKEND, PROXY)})
    result = restore_stopped(plan)
    private_json(PRIVATE / 'explicit-reversal-result.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    safe_main(main)
