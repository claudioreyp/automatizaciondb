# Security audit and exact cash history

This change is backend-only. It introduces no tables or migrations and does not
rewrite existing audit records.

## Read contracts

- `GET /api/v1/settings/audit` retains the existing fields and pagination. Each
  entry adds `summary` (action only; the client adds the actor) and `categories`.
  `branch_id` is the proven branch, including recovered legacy records; the
  stored legacy event remains unchanged.
- Optional `category` accepts `order_cancellation`, `item_cancellation`,
  `amount_reduction`, or `cash_withdrawal`. A mixed revision may belong to both
  product categories but remains one event. Existing `action` filtering remains
  compatible and intersects the category filter.
- `from` and `to` are inclusive calendar dates in America/Lima. Ordering is
  `created_at DESC, id DESC`. Semantic filtering and complete counting occur on
  the server by streaming scoped candidate actions before selecting the page;
  there is no 200-record cap. Unfiltered pagination/counts remain SQL queries.
- `GET /api/v1/settings/audit/{id}?branch_id=...` returns exactly `id`,
  `branch_id`, `actor_name`, `occurred_at`, `summary`, `fields`, `sections`, and
  `target`. Fields contain only `{label, value}`; missing values are null. The
  timestamp includes Lima's UTC offset. No raw payload is included in detail.
- Targets are null or `{kind: "order", branch_id, label, order_id}` /
  `{kind: "cash_movement", branch_id, label, register_id, movement_id}`. Order
  labels use the complete code and folio. IDs in the target are internal route
  identifiers, not replacements for the human-readable code.
- List and detail enforce the existing audit permissions and tenant scope.
  Explicit branches must match branch-scoped users, rather than being silently
  ignored. Owners without a fixed branch can request one allowed branch.
  Superadmin retains the existing explicit `business_id` requirement.
- `GET /api/v1/cash/registers/{register_id}/movements` additionally accepts
  `movement_id` and `branch_id`. Explicit branch mismatch is 404, including for
  business-wide owners. The response adds `branch_id` and
  `register: {id, name, active}`. Archived registers are readable and all their
  sessions are searched. A missing or wrong-register movement yields an empty
  page, never a substitute register or movement. Other filters intersect the
  exact ID filter. Reads never open a cash session or invoke a cut preview.

The register name in the cash history header is the current register label;
the security event's historical register name is a separate stored snapshot.

## Writes and historical evidence

- `order.cancelled` stores branch, actor, order code/folio, active item versions,
  cancellation reason, total and confirmed paid amount in its existing audit
  event. Confirmed payments are not altered. Repeating the transition does not
  replace the original snapshot or create another cancellation event.
- `order.items_revised` stores one event with all operations and their exact
  before/after item values, including quantity, unit price, discounts and net
  line totals. Revisions and reductions follow the affected item version.
- Both manual cash movement routes record the exact movement entity, branch,
  actor, register name and note. The modern route keeps required idempotency;
  the legacy session route accepts an optional `Idempotency-Key`, preserving
  compatibility for callers without one.
- Snapshots, operational writes and idempotency responses use the same existing
  transaction. No audit is committed for a failed operation.
- Legacy null-branch records are recovered only via their canonical entity ID
  and matching business/order or business/cash-session relationship. Payload
  claims alone cannot establish scope. Ambiguous session-only cash events have
  no movement target, even when the session currently has only one movement.
- Legacy product details prefer the uniquely matching stored kitchen revision.
  Retained item versions can supply their saved names, quantities and prices,
  but cannot prove past recalculated promotion amounts. Those amounts stay null
  without a matching snapshot. Names are never read from the current catalog.
- A missing historical register name or reason stays null. Known non-security
  action codes receive human descriptions without exposing arbitrary payload
  fields; unknown actions use a neutral description.

## Verification

`tests/test_security_audit.py` covers scope and permissions, complete semantic
pagination, Lima boundaries, legacy recovery, exact revision pairs, immutable
snapshots, simultaneous cancellations/reductions, repeated writes, rollback,
archived register reads and strict branch/movement deep-link filters.
`tests/test_table_history.py` retains cancellation/payment invariants while
allowing the newly expanded audit snapshot.
