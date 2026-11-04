# Native journal benchmark R01 — source only

This pack prepares an isolated component benchmark. No Docker command, n8n
execution, SQLite provision, VPS operation, model or performance measurement has
been run while preparing it. Local tests use the actual captured compiled node
actions with a deliberately identified recording backend. They are not native
SQLite evidence.

The installed capture is 66 files / 232,128 bytes. Its manifest SHA is
`0d29a151537827799b1c00b1e1a192e693082bb2d0b24f840c9487bd209a4c03`.
The exact raw capture SHA supplied by ROOT is
`bd2b885d166af4cefd81974bbe705859cdc91287aa4441e278750d92d22164f4`.
`prepare.py` verifies every captured byte and records every actual resolved
installed path, five package hashes, the CLI 2.14.2 / node/core/workflow 2.14.1
versions, and the pinned base image
`sha256:4f448824ec99e1160e49eeb1c5bf2130a5d244fe9029e871a9f4d9f126dbfc98`.

## What is compared

Four cases: 4 and 13 synthetic effects, each starting with an exactly 7,168-byte
or 204,800-byte body. Each effect has checkpoint / intent / result phases. Every
phase performs the same official UPDATE followed by an independent official GET.
CAS predicate order is `id, head, state, body_hash`; readback is `id, run_id`, limit
2, no sort. The same eleven application columns, operation order, body hashes and
complete ordered history are verified across all variants.

* `r12-shaped`: 37 measured nodes per effect: three plan nodes, 24 pure state
  guards/copies, three UPDATEs, three GETs, three readback verifiers and one effect
  fence. These pure checks model topology and journal serialization. They do not
  copy R12 business logic or claim production equivalence.
* `pure-merge`: unchanged six official DataTable nodes per effect, combined pure
  verify/next-plan operations: `9 × effects + 1` measured nodes. This alternative
  needs no core authorization extension in a future production implementation.
* `native-loop`: one trusted `CUSTOM.escalarJournalLoop` node over the entire
  synthetic case. It repeatedly calls the installed
  `DataTable.prototype.execute`, including its unchanged official router,
  get/update actions, filter/column validation, provider, services and repository.
  It observes 12 proxy acquisitions, six `getColumns`, three `updateRows` and three
  `getManyRowsAndCount` calls per effect. Observation delegates every original
  helper/proxy call without caching, bypassing or changing a result.

The proxy counters instrument only the native variant. Baseline evidence is its
actual six DT-node visits and readbacks per effect; no claim of independently
instrumented equal baseline proxy counts is made.

The intent/result phases contain no POS operation. This isolates journal cost;
real POS boundaries, inter-node guard deadlines and full agent latency require a
separate integration review and proof. The node does not execute arbitrary code.

## Authorization and provisioning

The derivative adds exactly `'CUSTOM.escalarJournalLoop'` to the installed proxy
service's four-entry `ALLOWED_NODES` array. `apply-patch.cjs` verifies all original
files and packages before writing that one literal replacement; its inverse must
restore the captured bytes exactly. No source identity is spoofed. The same
derivative is used for all three measured variants.

Original helpers retain the actual CUSTOM node and owning workflow. Ordinary
operations supply no project override: the provider derives workflow ownership,
and every DataTable service operation retains `validateDataTableExists(tableId,
projectId)`. Native components explicitly require an unapproved CUSTOM type and
a wrong project to be rejected. The wrong-project override is used only for that
negative component.

The captured compiled helper function is actual evidence. Its caller
`execute-context.js` was not part of the capture. ROOT inspected the corresponding
official 2.14.2 TypeScript caller, which unconditionally spreads
`getDataTableHelperFunctions(additionalData, workflow, node)` into `this.helpers`:
[official execute-context.ts](https://raw.githubusercontent.com/n8n-io/n8n/n8n@2.14.2/packages/core/src/execution-engine/node-execution-context/execute-context.ts).
That caller statement is a qualified source inference, not an installed compiled
hash claim. Actual CUSTOM helper availability is required before any timing.

Each case gets a new network-none container, fresh tmpfs SQLite and fixed own
workflow `JournalBenchR01Workflow`. Standard CLI initialization/import provides
ownership; there is no guessed ownership row, direct schema creation or fallback.
Official `table:create` with `createIfNotExists:false`, official `row:insert` and
actual scoped lookup produce a fresh binding. Failure to obtain that owning
project stops before timing. All inter-run seed resets use an official UPDATE
guarded by the previously verified head/state/hash, then an independent GET.
They affect only this new disposable synthetic table.
This is a logical CAS reset of one row in the same case's SQLite database, not a
byte-exact SQLite snapshot/copy. System timestamps and execution records can grow;
variant order rotates, and complete application columns/history must match.

The installed SQLite repository preselects affected IDs before UPDATE. Its
returned row array is therefore not treated as an affected-row ownership count.
Ownership is accepted only from independent readback with exact nonce/body/hash.

## Components and measured evidence

Before timings, actual official operations test stale CAS0, filter validation,
wrong project, unapproved type, overlapping conflicting CAS calls, loser replay,
and another actual official writer between UPDATE and readback. Exactly one
concurrent payload must match the independent final readback. Database errors
remain visible; no conflict or timeout is retried.

Four explicitly labeled synthetic fault seams delegate actual writes/reads before
injecting abort-after-commit, lost update result, empty readback or duplicate
readback. Each proves one primary write/readback, preserved committed row and no
next write. Corrupt-body and pre-operation cancellation gates are also checked.
These injected faults do not claim a real network interruption or process kill.
The PoC conservatively rejects an update error even when the independent readback
matches the expected committed row. That is this benchmark's rule; it is not a
change to R12's existing recovery/adoption policy and must not be transplanted.

The future runner makes 36 timed executions: four cases × three variants × three
repetitions. Variant order rotates. Seed reset, imports, exact workflow readback,
source verification and provisioning are outside the measured bracket. Timing
includes all measured state nodes and final native oracle; native first-call
module pin validation/loading is conservatively included. CLI wall time and sum
of actual node execution times are retained separately. Every raw command/output,
graph and median is saved. There is no simulated speed claim or fixed speedup
threshold. `native_median_faster_than_r12_all_cases` is a separate observed result;
correctness PASS alone does not accept performance or authorize models.

The runner has a total 1,800-second deadline with 90 seconds reserved for owned
cleanup, per-workflow timeout 120 seconds, no retries, no credentials and no HTTP
nodes. Containers are UID1000, CPU1, memory1536MiB, pids128, read-only root,
capabilities dropped, no ports/mounts/shared volumes, network none; only two fresh
tmpfs paths are writable. Cleanup checks exact ID/image/nonce and removes only
the new containers/tmpfs and uniquely labeled derivative image. The base image,
production container, current fixture, workflows and ledger are never targets.
The derivative build explicitly uses the legacy builder (`DOCKER_BUILDKIT=0`),
an already present base image ID, `--pull=false` and `--network=none`. It fails if
that local builder is unavailable; there is no registry/BuildKit fallback.

## Commands

Local bounded source tests:

```text
node --test benchmark.test.cjs
python -m unittest test_sources.py -v
python prepare.py --output <fresh-source-pack-directory>
```

After separate review and staging, the concrete future Linux invocation is:

```text
python3 <exact-pack>/run-proof.py --run --manifest-sha256 <exact-manifest-SHA> --output <fresh-result-directory>
```

`prepare.py` only materializes public source. `run-proof.py` is inert on import
and requires both `--run` and the exact outer manifest pin. No remote invocation
was performed here. The generated pack's manifest pins the runner itself.
