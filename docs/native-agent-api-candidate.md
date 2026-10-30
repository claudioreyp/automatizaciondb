# Native restaurant agent API candidate

This proposal supplies the API side required by the inactive native n8n restaurant agent. It does not activate n8n or WhatsApp and does not deploy the API.

The changes include durable conversation input/media reservations and source inspection, worker/outbox freshness checks, customer order operations with current version and quote checks, staff review of payment evidence, and printing rules for additions. Evidence uploaded through the integration stays under human review and does not make an additional provider call in the API. The existing model fields and product dependency files are unchanged.

New additions from the `restaurant_agent` profile require an explicit `cash` or `yape` choice. Missing/null choices return `409 ADDITION_PAYMENT_METHOD_REQUIRED` before any new addition mutation. Previously recorded idempotent results are returned before this guard. The generic profile retains its prior behavior. An existing client using the restaurant agent profile must send the actual addition choice explicitly.

## Validation

- Three new ASGI boundary tests, nine requests: missing/null choice rejection with unchanged business tables, explicit choices different from the original payment method with stable duplicate results, and a historical omitted-method result replay without new effects.
- Python 3.12.14, dependencies resolved from a pinned offline test environment, SQLite in memory, injected authentication and event broadcasts, no provider/printer/external traffic. The script does not verify production authentication or PostgreSQL concurrency.
- The 18 changed application files were applied to the exact base commit in an isolated file tree and matched the tested candidate byte for byte. A first local byte check failed due to Git automatic CRLF conversion; the corrected command used `core.autocrlf=false` and `core.eol=lf`. No global Git setting was changed.

The standalone verification script accepts `--source <repository-root>` and `--output <new-directory>`. Its contents match the already executed new boundary tests; it has not been rerun solely for packaging. Older partial test suites are not represented as integrated acceptance of this proposal.

## Before merge or deployment

Verify the commit actually served by Render, the existing database schema and active 449 client payload compatibility. Confirm the nine missing contracts and media reservation inspection after an approved deployment. Complete integrated n8n conversations, channel integration and POS visual verification. Current public OpenAPI still lacks those ten operations. Production deployment remains subject to the owner's approval.

This is a draft for review. No migration, active workflow replacement, production configuration change or automatic deploy enablement is included.
