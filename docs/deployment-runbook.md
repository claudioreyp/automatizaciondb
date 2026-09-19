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

## Published test release evidence

Completed 2026-09-19, without purchasing plans. The three custom domains serve
valid HTTPS. Only three CNAME records were added; root/www and other services were
left unchanged. Supabase Site URL is the HTTPS POS, with exact POS invitation/login
and Admins login redirects; existing loopback invitation redirects were retained
for local testing. Public signup remains disabled.

| App | Published application commit | Deployment |
| --- | --- | --- |
| CLIENTES | `a915c1352d4acf6efe22bdc585de99227ad69c7a` | Vercel `dpl_B66G49sXf7VHNydVB3pE4zNqqbc7` |
| Admins | `3a8d2e219d665973443b7293c2531b5c75951bcb` | Vercel `dpl_96492mondvcDRQTcKoRAHFT47idu` |
| API | `1d332cf2d12f56e4760fcd8f15d95a461f6f8514` | Render `dep-danb3v6gekts738ce53g` |

Subsequent documentation/test-script commits do not change application code.
The final Vercel Git builds are also READY and assigned to production: CLIENTES
`08eae200dfe4d7c108b4d6166a586ac29ce996f6` /
`dpl_ChTr8L93XkTTdV1RNFM7py6S5ts2`, Admins
`00cc18458745cb04b342d69250544ae0a344f74d` /
`dpl_GPXZzNfawXTyn8a9uLReD9Zb2kTM`. Deep links and API health were rechecked as HTTP
200 after this promotion. Render remains on the application commit above because
the later API commits contain only documentation and opt-in verification scripts.
Vercel production branches match the existing `agent/escalar-ai-*` branches.
Render remains manually deployed. Vercel's technical preview addresses may require
Vercel authentication; use the custom domains for POS testing.

Verification completed:

- API Pytest: 723 passed, 2 PostgreSQL tests skipped in the SQLite run. Both
  PostgreSQL tests passed separately in disposable schemas: concurrent folios and
  same-identity password renewal. The temporary migration-test role was dropped.
- CLIENTES: lint/build and 753 Vitest tests passed. Admins: lint/build and 22
  Vitest tests passed. Both Vercel remote builds and Render startup passed.
- Mocked Playwright regression: CLIENTES 227 passed/1 skipped, Admins 9 passed,
  across desktop/tablet/mobile. Orders, tables, kitchen, printing and PWA included.
- Live HTTPS: Admins login, two new restaurant/owner/token provisions, owner login,
  minimum scopes, copyable endpoint existence, cross-tenant denials, suspension /
  reactivation, password renewal, idempotent renewal replay, old-password and
  old-HTTP-session rejection. Integration token survived password renewal.
- Live deployed browser checks at 1440x1000, 834x1112 and 390x844: both logins,
  restaurant details, order/table/kitchen navigation, installation page, manifest
  and PNG icons. No development login or added Support/Tutorial links.
- Exact credentialed CORS for the two frontends; unrelated origins rejected.
  Direct authenticated WSS connected to the test branch. Real PIN pairing/login/
  logout used Secure + HttpOnly + SameSite=Lax cookies; cashier could not enter
  Admins. Test device/member were archived after checking.
- All four recovered images returned HTTP 200 through the deployed API and matched
  their source SHA-256. Storage stays private; public catalog/branding routes keep
  their existing authorization contracts. Actual Google Maps tiles rendered from
  the POS domain, without saving a location or changing the key's restrictions.
- QZ server identity and RSA/SHA512 signature verified without contacting a printer
  or claiming a job. One historical pending print and 23 unacknowledged events
  remain untouched. No real orders/payments were created in this live smoke test.
- Alembic remains `20260919_0023`. All public tables have RLS; anon/authenticated
  have zero direct public-table grants. `escalar_pos_api` is not superuser, cannot
  create roles/databases/tables, and cannot bypass RLS.
- Grouped visual review of deployed desktop/mobile screens preserved the existing
  interface. Targeted Impeccable detector returned no findings; this is not a full
  accessibility certification. Artifacts stay local and excluded from Git.

Test businesses 3 and 4 are explicitly labelled PRUEBA DESPLIEGUE and were left
suspended; their tokens were revoked. Their audit history remains. Temporary
passwords/tokens were removed from the local test receipt after verification.
Original restaurants, owners, integration tokens and operational records were not
reset by these checks.

Remaining limits (do not claim these as tested):

- Full physical printing from the public domain and physical mobile installation
  remain pending. Localhost installation/printing evidence is not cloud evidence.
- A diagnostic output exposed QZ private identity material during setup. It was
  not committed or placed in frontend assets. Treat it as exposed and arrange a
  supervised identity rotation and renewed trust on each printer workstation;
  do not silently rotate or disable QZ security. The current identity was retained
  to avoid breaking terminals pending that coordinated action.
- Render wake-up after a full idle/sleep cycle has not been measured. Reconnection
  logic passed isolated tests, but Free is not continuous commercial hosting.
- SMTP, server route-calculation and vision credentials remain unconfigured.
  Browser Maps loading does not establish server route/vision availability.
- npm audit reports dependency advisories. The production React Router advisory
  GHSA-qwww-vcr4-c8h2 affects unstable RSC APIs, not these Vite BrowserRouter SPAs;
  dependency maintenance remains a separate tested upgrade, not a blind audit fix.

Opt-in reproduction scripts: `verify_cloud_test_release.py`,
`verify_cloud_transport.py`, `retire_cloud_test_release.py` and
`Admins/scripts/cloud-browser-smoke.mjs`. They require explicit private input and
labelled test tenants. Never rerun a creation after an uncertain response without
reconciling its ignored private receipt. Never publish those receipts.

Platform references: [Render custom domains](https://render.com/docs/custom-domains),
[Render Free](https://render.com/docs/free),
[Vercel Hobby](https://vercel.com/docs/plans/hobby),
[Supabase SMTP](https://supabase.com/docs/guides/auth/auth-smtp),
[React Router advisory](https://github.com/advisories/GHSA-qwww-vcr4-c8h2).

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
