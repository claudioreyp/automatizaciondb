# API deployment runbook

Canonical Render service: `escalar-ai-pos-api`.
Tracked branch: `agent/escalar-ai-pos-api`.

## Release

1. Require a clean test run and one Alembic head.
2. Confirm production is on the expected previous Alembic revision.
3. Push the reviewed commit to the tracked branch.
4. Let Render run `alembic upgrade head` as its pre-deploy command.
5. Verify health, OpenAPI, authentication, tenant isolation, and the new routes.

## Safe rollback

Render keeps the last healthy instance serving traffic when build, pre-deploy, or
startup health checks fail. Do not deploy a commit that predates an already applied
Alembic revision.

If a defect is found after a successful database migration, create a forward-fix
commit from the deployed revision. A rollback commit must retain every applied
migration and the tenant-scoped idempotency contract, even if it disables a new
endpoint or behavior.
