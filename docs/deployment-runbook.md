# API deployment runbook

Canonical Render service: `escalar-ai-pos-api`.
Tracked branch: `agent/escalar-ai-pos-api`.

## Test release: 2026-09-19

Personal, noncommercial tests only. Vercel Hobby is not the commercial hosting
plan. Render Free can sleep and needs time to wake; this is not a 24/7 SLA.
No purchases, n8n/gateway changes, or VPS changes are part of this release.

| Component | Destination | Public address |
| --- | --- | --- |
| CLIENTES | Vercel `escalar-ai-pos-clientes` | `https://pos.escalarai.tech` |
| Admins | Vercel `escalar-ai-pos-admins` | `https://admin.escalarai.tech` |
| API | Existing Render `escalar-ai-pos-api` | `https://api.escalarai.tech/api/v1` |
| DB/Auth/Storage | Supabase `vgxbymduddknaxczudxj` | Private runtime connection |

GitHub owner: `claudioreyp`. Each app retains its existing repository and branch.
Never use the unrelated GitHub connector identity or force-push these branches.

## Release

1. Require a clean test run and one Alembic head.
2. Back up database, private configuration and uploads. Confirm the applied Alembic
   revision; the recovered database already has `20260919_0023`.
3. Disable Render auto-deploy BEFORE pushing. Review ignored/private files and
   scan the release for credentials. Push only to each repository's `origin`.
4. Do NOT run Alembic in build, pre-deploy or start. If a future additive migration
   is needed, test a copy and apply it separately with an authorized migration
   identity. The API runtime user cannot perform DDL.
5. Render build: `pip install -r requirements.txt`. Start:
   `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log --log-config logging.json`.
   Health path: `/api/v1/health`. One process/instance preserves the current
   in-memory realtime hub. The formatter redacts credential query strings.
6. Configure production Auth, restricted PostgreSQL TLS, existing QZ identity,
   integration secret, Auth operation secret and device secret ONLY in Render.
   `ENVIRONMENT=production`, `AUTO_CREATE_SCHEMA=false`, no development token.
7. Compile Vite apps with the public Supabase key and HTTPS API address. Never
   upload `.env.local`, backups, test artifacts or server credentials to Vercel.
8. Deploy the reviewed commit manually. Add only `pos`, `admin`, and `api` DNS
   records. Preserve root/www, mail, `escalarai.cloud`, VPS and n8n records.
9. Verify HTTPS, deep links, manifest/icons, exact CORS, Auth redirects, secure
   same-site device cookies, direct WebSocket, auth and tenant isolation.

## Persistent media and secrets

`python -m scripts.prepare_test_deployment prepare` creates an ignored private
snapshot, configuration backup and uploads archive. Never commit that output.
`media` inventories local references; `media --apply` uploads to private Storage,
verifies downloaded SHA-256 and updates references in one transaction with
concurrency checks. Originals remain untouched. Render's disk is ephemeral.

The 2026-09-19 snapshot contains 51 tables / 756 rows. Four referenced local images
in three rows were transferred and verified. There were no paired devices and
no existing device secret; a new private device secret was initialized once.
QZ and integration identities were preserved. No migration was run by this step.

Never show secrets in screenshots, build logs, command output or support reports.
Do not replay pending print jobs or historical integration events during checks.

## Pending commercial activation

- Upgrade hosting for commercial use and continuous operation before real clients.
- Configure SMTP separately; built-in Auth email is not a production invitation
  service. Password login and Admins password renewal do not require SMTP.
- Confirm browser Maps key restrictions include the POS domain. Server route and
  vision API credentials are separate; do not substitute the browser key.
- Activate/test QZ on each printer workstation and confirm physical output. A
  cloud build or successful queue response is not proof of paper output.
- The existing localhost PWA does not move domains automatically. Install the
  HTTPS POS separately; mobile physical installation must be checked separately.

## Safe rollback

Render keeps the last healthy instance serving traffic when build or
startup health checks fail. Do not deploy a commit that predates an already applied
Alembic revision.

If a defect is found after a successful database migration, create a forward-fix
commit from the deployed revision. A rollback commit must retain every applied
migration and the tenant-scoped idempotency contract, even if it disables a new
endpoint or behavior.

Never restore an old database over new writes or return to SQLite after PostgreSQL
activation. Roll back only to compatible application code and reconcile uncertain
operations before retrying.
