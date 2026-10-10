# Database credentials: per-service roles, rotation, burned values

Security findings **H-1** (repo-known DB passwords) and **H-3** (one shared DB superuser).
Owner of the design: `config/postgres/service-roles.yaml` (catalog) +
`scripts/db/service_roles.py` (renderer/reconciler) + `alembic/versions/0049_per_service_db_roles.py`.

## What changed

| Before | After |
|---|---|
| Every pod `envFrom`'d `waddlebot-secrets`, which carried the database **owner/superuser** (`waddlebot`) as `DATABASE_URL` / `DB_PASS` / `DB_PASSWORD` / `POSTGRES_PASSWORD`, and the shared ConfigMap carried `DB_USER=waddlebot` | The owner credential lives in its own Secret (`<release>-db-admin`), read **only** by the Postgres Deployment and the `db-migrate` hook Job. Every workload connects as its **own** least-privilege LOGIN role |
| `031_scoped_database_users.sql` and `config/postgres/init.sql` created ~35 LOGIN roles with repo-public `*_dev_changeme` passwords (re-applied by `0005`), including `hub_admin` | `031` has **no passwords**; roles are created `NOLOGIN`. `0049` strips LOGIN + password from every such role on existing databases |
| A pod compromise, or one leaked `envFrom`, was a full database takeover | A pod compromise yields one role confined to its own tables (below) |

The pre-fix chain was reproduced against a real fresh replay: connect as `hub_admin` with the
repo password, call the `SECURITY DEFINER` `provision_module_db_account(..., custom_grants =>
ARRAY['ALTER ROLE hub_admin SUPERUSER'])`, and you are superuser. `0049` removes the login and
revokes `EXECUTE` on the escalation functions (`test_hub_admin_escalation_chain_is_closed`).

## Role model

Roles come from the catalog; each is `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
NOBYPASSRLS`, has no `CREATE` on any schema, and owns nothing.

| Profile | Roles | Reach |
|---|---|---|
| Control plane | `waddles_hub_api` (hub-api, seeder, signing, sync Jobs) | DML on base tables + sequences; **no** DDL; `alembic_version`/`schema_migrations` read-only; `platform_integrations` & credential audit tables denied; matrix grants inherited from `hub_api` |
| Strict data plane | `waddles_svc_action`, `_svc_presentation`, `_svc_streaming`, `_reputation` | An explicit table list (e.g. svc-action: `INSERT,SELECT action_dispatch_log`, `SELECT tenants,communities`) |
| Zero-table | `waddles_svc_ingest`, `_svc_process`, `_svc_core` | `CONNECT` only (state is read via `waddles_bundle_reader` / hub gRPC) |
| Legacy pods | `waddles_legacy_<pod>` (25 roles, one per legacy Deployment) | DML on base tables **minus** the RBAC-matrix tables, views (privilege boundaries), credential stores, and the identity/authentication tables (`hub_users`, sessions, tokens, passkeys, `ephemeral_pseudonyms` -- only the legacy hub, which implements login, keeps those); `platform_integrations` rows only through the pod's designed `mod_*` RLS membership; `hub_users` only via the column grants of that membership |

Two details that the privilege matrix alone cannot show (both covered by functional tests):

* `resolve_identity_uuid()` is `EXECUTE`-restricted to the owner; hub-api calls it directly, so
  `waddles_hub_api` is granted it (`functions:` in the catalog) -- no other role is.
* The `community_members` BEFORE INSERT trigger (`community_members_set_user_uuid`, 0045) is
  invoker-rights and reads `hub_users` / `hub_user_identities` and mints `ephemeral_pseudonyms`.
  Left as-is every `community_members` writer (e.g. reputation) would need PII-table access, so
  `reconcile` makes that one trigger function `SECURITY DEFINER` with a pinned `search_path` (a
  trigger function cannot be called directly) and re-asserts it on every run.

Already-separate roles are unchanged: `waddles_bundle_reader` (RO active-set), `waddles_bundle_migrator`
/ `waddles_bundle_runtime` (app schemas), `waddles_connector_pii_reader` (PII views).

Add or change a role in **one** place: edit `config/postgres/service-roles.yaml`, mirror the name in
`values.yaml` `infrastructure.postgresql.serviceRoles.roles`, wire the Deployment with
`{{- include "waddlebot.dbEnv" (dict "root" . "role" "<role>") | nindent N }}`. The next migrate Job
run grants it; `alembic/tests/test_service_roles_catalog.py` fails if the three drift.

## Where each credential lives

| Credential | Secret / key | Read by |
|---|---|---|
| Database owner | `<release>-db-admin` / `POSTGRES_PASSWORD` (or `infrastructure.postgresql.admin.existingSecret`) | Postgres Deployment; copied into the `db-migrate` hook Secret `DATABASE_URL` |
| Service role passwords | `<release>-db-credentials` / `PW_<ROLE_UPPERCASE>` (or `serviceRoles.existingSecret`) | each pod, **only its own key**, via `secretKeyRef`; never `envFrom` |
| Same passwords, for provisioning | `db-migrate` hook Secret / `WADDLES_DB_SERVICE_ROLE_PASSWORDS` (JSON) + `WADDLES_DEPLOYMENT_TIER` | the `db-migrate` Job (0049 + the every-run reconcile) |

Resolution (same policy as every other chart secret): explicit value > existing Secret key (kept
across upgrades) > generated (**alpha/local only**) > **render fails** (beta/gamma/production).
Values must be >=16 chars of `[A-Za-z0-9._~-]` (they are embedded in `DATABASE_URL`) and may not look
like a placeholder; the same rules run at render time and in `service_roles.py`.

Beta/gamma/production: pre-populate `<release>-db-credentials` (ExternalSecret / SealedSecret) with
a `PW_<ROLE>` key for every role in the catalog, or set `serviceRoles.passwords` from an out-of-git
values file. Generate each with e.g. `openssl rand -hex 24`. Never commit a value.

## Rotating

Service role (any of them) -- no downtime beyond a rolling restart:

1. Set a new value for `PW_<ROLE>` in `<release>-db-credentials` (or your external store).
2. `helm upgrade` (or re-run the migrate hook). The `db-migrate` Job's `reconcile --strict` step
   `ALTER ROLE ... PASSWORD`s the role to the new value and repairs any grant drift.
3. Restart the role's workload(s) so they re-read the Secret
   (`kubectl rollout restart deploy/<name>`). Until then the pod keeps its existing pooled connections.
4. Verify: `kubectl logs job/<release>-db-migrate` shows `Reconciled N service roles`; the pod is Ready.

Database owner: change `POSTGRES_PASSWORD` in `<release>-db-admin`, then
`ALTER ROLE waddlebot PASSWORD '...'` out-of-band **before** `helm upgrade` (Postgres only reads
`POSTGRES_PASSWORD` at initdb), or the migrate Job cannot authenticate.

## Burned credentials

The values below are in git history and public to anyone with repo read access. **Treat them as
compromised**; they are neutralized by `0049` and must never be reused anywhere:

* every `<role>_dev_changeme` / `mod_<role>_dev_changeme` in `031_scoped_database_users.sql`, `init.sql`, `docker-compose.yml` (including `hub_admin_dev_changeme`)
* `dev123` (`waddlebot_dev`) and `kong_db_pass_change_me` (`kong`) from `init.sql`

For any database that was ever reachable beyond a developer machine: (1) rotate the owner password,
(2) rotate every `waddles_*` / bundle role password, (3) audit `pg_stat_activity` / server logs for
logins by the neutralized roles, (4) review `module_db_accounts` and `\du` for roles created through
`provision_module_db_account` (random, discarded passwords -- drop any you do not recognize), and
(5) check `pg_proc` for `SECURITY DEFINER` functions not shipped by a migration.

## Local development (docker-compose)

`docker-compose.yml`'s `db-migrations` service sets `WADDLES_DEV_DB_ROLE_PW_SUFFIX=_dev_changeme` and
`WADDLES_DEPLOYMENT_TIER=dev`, which re-enables the dev logins (`<role>_dev_changeme`) so the compose
stack keeps working, and derives the service-role passwords as `<role>_dev_changeme`. `alembic/env.py`
**refuses** to start if that suffix is set while the tier is alpha/beta/gamma/production, and the chart
never sets it. `config/postgres/init.sql` is mounted only by docker-compose (guarded by a test).

## Upgrading an existing cluster

1. Build/deploy the new migrations image (needs `0049`, `scripts/db/service_roles.py`, the catalog).
2. `helm upgrade`: the `pre-upgrade` migrate Job creates the roles and neutralizes the old logins
   **before** pods roll; old pods keep working on the unchanged owner password until they restart.
   The owner password is carried over from `waddlebot-secrets/POSTGRES_PASSWORD` into `<release>-db-admin`
   once (so the PVC's initdb password still matches); the keys are then absent from `waddlebot-secrets`.
3. Pods restart onto their own roles. If a workload fails with `permission denied for table X`, the
   catalog is missing a grant: add it to `service-roles.yaml` (never to a shared role) and re-run the Job.

Rollback needs the owner keys back in `waddlebot-secrets` (`POSTGRES_PASSWORD`, `DATABASE_URL`, ...)
because the previous chart revision reads them there:
`kubectl -n <ns> get secret <release>-db-admin -o jsonpath='{.data.POSTGRES_PASSWORD}'`.

## Residual risk

* **R1 -- legacy pods are still broad.** Each has its own revocable credential and no DDL/superuser,
  and credential rows / identity-authentication tables are already fenced off, but the rest of its DML
  reach is "all other base tables". Narrowing each to an explicit `tables:` list needs a per-module
  query audit (the legacy module stack is being replaced by the v3 pipeline). If a legacy pod fails with
  `permission denied`, fix it in `service-roles.yaml` for that pod only (`allow_tables` / group
  membership), never by widening a shared role.
* **R2** -- `DB_READER_PASSWORD` (SELECT-only on 8 tables) and `BUNDLE_MIGRATOR/RUNTIME_PASSWORD` still
  travel in `waddlebot-secrets`; moving them is a follow-up.
* **R3** -- `waddles_hub_api` can read/write every non-denied base table by design (it is the API
  server and PII boundary). It cannot alter schema, create roles, or touch `pg_authid`.
* **R4** -- the `SECURITY DEFINER` `provision_module_db_account` family still exists (legacy admin
  panel). `EXECUTE` is revoked from everyone but the owner outside dev; do not grant it.

## Verifying

* `python3 -m pytest alembic/tests/test_service_roles_catalog.py` -- catalog, password policy, no repo credential in shipped files.
* `python3 -m pytest alembic/tests/test_0049_per_service_db_roles.py` (needs docker) -- real fresh-replay Postgres: full privilege matrix, cross-service denial, RLS, escalation chain closed, rotation, downgrade round-trip, dev opt-in.
* `python3 -m pytest k8s/helm/waddlebot/tests/test_db_credential_separation_render.py` -- owner credential confined, one distinct role per workload.
* `python3 -m pytest alembic/tests/test_chart_credentials_roundtrip.py` (needs docker + helm) -- provisions a fresh DB from the chart-rendered hook Secret, then authenticates as every rendered workload identity.
