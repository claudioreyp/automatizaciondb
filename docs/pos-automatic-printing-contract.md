# Automatic local POS printing (v1)

All routes below have the `/api/v1` prefix and use ordinary authenticated POS
sessions, including paired-device member sessions and their existing CSRF checks.
Only `Apis` implements these contracts; no schema migration is required.

## Trigger and durability

- A newly confirmed POS command creates local jobs if `advanced_printing` is true
  and `printer_config.printer_name` is nonempty, unless
  `printer_config.automatic_printing` is explicitly false (legacy default: true).
  `auto_print_kitchen: true` enables
  kitchen jobs. Explicit `manual_customer_receipt: false` enables the initial
  customer receipt. Existing missing flags are not silently enabled.
- Without a table, the first command produces at most one customer receipt and
  one kitchen job. With a table, it produces only the kitchen job; empty table
  creation produces none. Additional commands produce kitchen jobs only.
  `/confirm-and-send`, the legacy
  `/confirm` then `/send-to-kitchen` path and confirmed item batches share the
  services; draft creation alone does not print.
- Existing confirm responses remain unchanged. The frontend uses
  `response.order.id` and `response.order.branch_id` after success and runs QZ
  asynchronously. Never report a confirmed order as failed because QZ is offline.
- At command creation, an internal intent is persisted in its existing JSON
  context. Queue inserts use a savepoint; failure retains the intent without
  rolling back the order. List/claim recover the original jobs and configuration.
  The existing unique business/idempotency key prevents duplicate queue entries.
  A failed order transaction commits neither command, intent nor jobs.
- Table receipts are the exception to savepoint recovery: `/table-checkout/start`
  inserts the full account in the same transaction as the actual close transition.
  Its key includes the order version, so concurrent/repeated closes do not create
  more receipts. Insertion failure returns 503 and rolls back that close, without
  altering previous orders or payments. Payments never create another receipt.
  Reopening invalidates unsent accounts of that cycle; a subsequent real close
  creates an updated account. Claimed documents still accept late ACKs, not another
  dispatch. No close receipt is backfilled into historical orders.
- No markers or jobs are backfilled for historical orders. Integrations are not
  enrolled into this local queue. Existing paired-device jobs/endpoints remain
  compatible. A POS configured with a local printer uses that transport instead
  of also scheduling the same command on the paired-device transport.

## List jobs

`GET /orders/{order_id}/printing` returns `Cache-Control: no-store`:

```ts
type PrintingResponse = {
  order_id: number;
  items: Job[];
  recoverable_error: boolean;
};
type Job = {
  id: string;
  order_id: number;
  branch_id: number;
  kitchen_ticket_id: number | null;
  job_type: "customer_receipt" | "kitchen_ticket";
  status: "pending" | "claimed" | "printed" | "failed" | "cancelled";
  attempts: number;
  retryable: boolean;
  error_message: string | null;
  created_at: string;
  payload: {
    snapshot_version: 1;
    printer_name: string;
    print_language: "pixel" | "escpos";
    paper_width_mm: 58 | 80;
    copies: number;
    template: Record<string, unknown>;
    business: { id: number; name: string; currency: string; timezone: string };
    branch: { id: number; name: string; address: string | null; phone: string | null };
    order: OrderDetail;
    ticket: KitchenTicket | null;
    created_by_name: string | null;
    table_name: string | null;
    paid_amount: number;
    remaining_amount: number;
  };
};
```

`OrderDetail` and `KitchenTicket` are renderer-compatible historical projections,
not fresh GET requests. They contain the confirmed order's identity, timestamps
in UTC, destination, monetary amounts, item names/quantities/variants/modifiers,
actual confirmed payments and table context. The receipt has no kitchen ticket.
Kitchen `context` contains explicit snapshot values (including nulls) for
`order_number`, `order_folio`, `channel`, `source`, `customer_name`,
`customer_phone`, `delivery_address`, `notes`, `table_name`, `area_id`, `area_name`,
`created_by_name`. New commands freeze these fields. Manual kitchen reprints use
the recorded context, including explicit null/empty values; a missing field may
use an earlier stored command payload, never the order's current destination.
Unrecorded author names stay null, never a user ID. `line_total` includes extras;
do not add modifier prices to it again. Render timestamps in America/Lima.
The complete typed schema is included in OpenAPI.
`copies` snapshots the configured integer, bounded to 1..5 as in the POS settings;
missing or malformed values default to 1. Later settings edits never change it.
`print_language` is explicit configuration: `pixel` is the compatible default,
`escpos` enables QZ raw HTML rasterization for ESC/POS printers. No inference from
printer names is made. Settings PATCH rejects other values; legacy jobs without
this field remain readable as `pixel`. No new database column is required.
Save the profile through `PATCH /settings/branches/{branch_id}/printing` with its
expected version and idempotency key, then confirm it through GET for that same
branch. Only newly created automatic jobs and explicitly requested manual jobs
snapshot the new language, including a manual reprint of an older order. Other
branches' profiles, existing job payloads and durable intents remain unchanged;
changing a profile does not repair, rewrite or redispatch historical jobs.

GET/PATCH printing retains the existing JSON contracts and optimistic version,
permissions, audit and idempotency. Additive fields are
`printer_config.automatic_printing`, `font_size` in both templates (small, normal,
large), and customer `header_enabled`, `header_text`, `footer_enabled`,
`footer_text`. Text is plain, max 500 characters per field. Disabled text remains
stored. Missing values default to normal font, false text switches and empty text;
PATCH from older clients preserves stored extension fields omitted from their
JSON. Existing customizable fields and the global 1..5 copy count remain intact.

List is scoped to business, branch, active member and operational roles
owner/manager/cashier/waiter (or superadmin). It does not return legacy-device jobs.
A queue recovery failure sets `recoverable_error: true` while retaining existing
confirmed jobs. Render a retry warning; do not substitute client-generated jobs.

## Explicit manual printing

`POST /orders/{order_id}/printing` requires `Idempotency-Key` and returns 201 `Job`:

```json
{
  "job_type": "customer_receipt",
  "expected_order_version": 4
}
```

For `job_type: "kitchen_ticket"`, also provide `kitchen_ticket_id` and
`expected_ticket_version`. Both are required; receipts accept neither field.
The ticket must belong to the exact order, business and branch. Missing/invalid
fields return 422, missing ticket 404, stale versions or cancelled kitchen work 409.

The request creates exactly one pending document with server snapshots and current
printer settings. Cashier/waiter/manager can request it without settings-write
access, including closed order history. It does not edit the order, its payments
or kitchen state. Advanced printing and a selected printer are required (409 if
missing); automatic toggles do not prevent a manual print. The legacy
`/kitchen/tickets/{ticket_id}/print` contract is unchanged.

An explicitly requested `customer_receipt` also supports cancelled order history.
It snapshots `order.status: "cancelled"` for the existing `PEDIDO CANCELADO` label,
real products, totals, confirmed payments and remaining amount without changing
them. It has no kitchen ticket, creates no command and sends nothing to kitchen.
Cancelled orders still reject kitchen printing, as do cancelled tickets of an
active order. A pending receipt captured before cancellation remains invalid;
only a manual receipt already captured as cancelled receives this exception.
Subsequent content/status changes still invalidate that unsent historical receipt.

Dispatch only the returned job through claim/complete. Reusing the same key and
body returns the same job in its current state, even after it was printed. A body
mismatch is 409. A new key explicitly requests a new reprint and creates its own
audit event. Concurrent requests using the same key produce just one job. Failure
rolls back job/audit, never another confirmed order operation.

## Claim before dispatch

`POST /orders/{order_id}/printing/{job_id}/claim` requires `Idempotency-Key`:

```json
{
  "terminal_id": "15bfc866-0b15-4489-8452-d636817b94d5",
  "claim_token": "72d2c996-5887-4a61-867b-3a9e513c0086",
  "retry_not_sent": false
}
```

Response: `{ "job": Job, "dispatch_allowed": true }` for the one new claim.
Generate terminal/token UUIDs locally and keep them in memory through ACK retries.
Validate QZ connection and the exact printer name before claiming; never substitute
another printer. For RAW, measure the isolated, stabilized thermal HTML before
claiming. Supply `pageHeight` in raster dots alongside width 384 (58 mm) or 576
(80 mm), not viewport height, millimeters or an item-count estimate. Invalid
measurement stops before claim. Each independent document/copy retains init and
feed/full-cut `0A1D564100`. Only `dispatch_allowed: true` authorizes one call to
`qz.print`.

Claims serialize on the order (PostgreSQL row lock, SQLite transaction write lock).
Ownership binds user, staff/device identity, terminal and hashed claim token.
Another terminal gets 409. An idempotent replay returns `dispatch_allowed: false`,
not a cached permit. Lost claim responses therefore cannot cause duplicate sends.
Claimed jobs have no automatic expiry/reset. Tokens never appear in responses,
audit or plaintext idempotency records; request hashes detect key/body mismatch.

Order cancellation, relevant destination/content changes or command revisions
invalidate pending and known-not-sent snapshots rather than printing stale content.
Claimed jobs preserve ownership and accept late completion for bookkeeping, even
after a concurrent cancellation or revision. Uncertain outcomes remain uncertain;
neither state authorizes another dispatch.
Later payments and catalog/business/printer configuration edits do not rewrite
historical documents. A new item batch does not invalidate an unchanged earlier
kitchen command. Nothing can retract a document already submitted to QZ; a claim
is the atomic authorization point, not a distributed transaction with the printer.

## Complete / known failure

`POST /orders/{order_id}/printing/{job_id}/complete` requires `Idempotency-Key`:

```json
{
  "terminal_id": "15bfc866-0b15-4489-8452-d636817b94d5",
  "claim_token": "72d2c996-5887-4a61-867b-3a9e513c0086",
  "outcome": "printed",
  "error_message": null
}
```

Response is the updated `Job`. Only its owning terminal/member can ACK.

- `printed`: QZ's promise resolved, indicating submission to the spooler. It does
  not prove physical paper delivery. On failed ACK, retry only this ACK in memory.
- `not_sent`: the client guarantees no `qz.print` call was made. Status is `failed`,
  `retryable: true`. An explicit retry can claim with `retry_not_sent: true`, a new
  claim token and new idempotency key. Ordinary reload/automatic dispatch skips it.
  If the order changed before this ACK, the outcome is recorded but the job becomes
  `cancelled`, `retryable: false`, so stale work cannot be dispatched.
- `unknown`: any rejection/connection loss after calling QZ, or uncertain delivery.
  Status is `failed`, `retryable: false`; no automatic or explicit claim replay.

Untrusted error text is accepted for compatibility but is not stored; stable
operational errors avoid persisting local paths/credentials. Successful ACK replay
is safe; conflicting outcomes return 409. Settings cannot reset uncertain claims.

## QZ authorization

`GET /orders/{order_id}/printing/qz` returns
`{mode: "signed" | "manual-approval", certificate: string | null}`. Cashier,
waiter and kitchen roles can use discovery without obtaining settings-write rights.
Incomplete signing configuration returns 503 rather than downgrading security.

`GET /settings/branches/{branch_id}/printing/qz` also allows scoped operational
discovery. `POST /settings/printing/qz/sign?branch_id=...` allows operational
printing/discovery, not arbitrary QZ file/USB/socket operations; inline HTML
printing is the supported operational format. The old unscoped signature and
certificate routes remain manager-only. Private signing keys never leave the API.
The signer also permits raw HTML with `options.language: "ESCPOS"` and exactly
three exact raw-command hexadecimal strings: `1B40` (initialize),
`0A0A0A1D5601` (legacy feed/cut) and `0A1D564100` (feed/full cut). Each requires
`type: "raw"`, `format: "command"`, `flavor: "hex"`; added bytes, whitespace or
different casing are not accepted. It does not authorize arbitrary raw commands,
file-based print data or cash-drawer pulses. Operational permissions, branch
scope and the manager-only unscoped route remain unchanged.

QZ's native approval dialog remains native and must not be simulated or bypassed.
Its promise semantics are documented at [QZ print](https://qz.io/api/qz).

## Verification scope

`tests/test_pos_printing.py` uses isolated in-memory SQLite plus a temporary
file-based database with two independent concurrent sessions. It covers snapshots,
flags, recovery/savepoint failure, rollback, idempotency, ownership, permissions,
stale/cancelled jobs, explicit retries, payment changes, additions and QZ signing.
It also verifies configured copies, late acknowledgements after concurrent
cancellation/revision/payment, and branch-scoped signing by cashier/waiter/kitchen.
Manual request/replay/reprint, closed history, ESC/POS snapshots and exact allowed
control commands are covered as well. Cancelled receipt tests cover unpaid,
partially paid and fully paid snapshots, unchanged domain records/events,
idempotent request/claim/ACK, and rejection of cancelled kitchen work.
The isolated branch-2 ESC/POS regression exercises settings PATCH/replay/GET,
new automatic jobs and manual reprints, branch-1 profile isolation for both
languages, and unchanged historical payloads/intents. Signer tests accept both
exact cut sequences and reject altered commands, file data and cash-drawer pulses
without invoking the signing function for rejected requests.
No real records, print services, hardware or migrations are touched by these tests.
