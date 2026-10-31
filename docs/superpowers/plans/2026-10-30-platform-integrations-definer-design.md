# Design: route hub-api through SECURITY DEFINER functions for `platform_integrations`

Issue: #969 (NOT #893/#894). Status: IMPLEMENTED (see "Implementation notes" at the end).
Decision (coordinator): keep `waddles_hub_api` DENIED the table (H-3); EXECUTE on functions only.

## 1. Call-site classification

Only two hub-api modules touch the table at runtime. The other `grep` hits are crypto helpers
(`platform_integrations_crypto`, no SQL), docstrings, or `erasure_registry` declarations.

| # | Site | R/W | Columns | Secret? | Group |
|---|------|-----|---------|---------|-------|
| 1 | `services/platform_config_service.py:94-105` `get_platform_configs` (superadmin GET, the 500) | R | all, but `_mask()` collapses tokens/secret to `*_set` booleans | NO (presence flags only) | **A metadata** |
| 2 | `services/community_connections.py:217` `_select_one` / `:243` `get_connection` | R | `_SELECT_COLUMNS` (incl. tokens) but result shaped to `ConnectionStatus` | NO (verify `_account_label` reads `config_data` only) | **A metadata** |
| 3 | `community_connections.py:223` `list_connections` | R | same | NO | **A metadata** |
| 4 | `platform_config_service.py:166-181` `test_platform_connection` (bot lane) | R | `access_token`, `is_encrypted`; decrypts then calls provider API | **YES - operator BOT token** | **C blocked** |
| 5 | `community_connections.py:535` `get_decrypted_tokens` (callers: `role_sync_service.py:1253` CronJob, `music_providers/youtube.py:309`) | R | tokens, client_id/secret, seal_version | **YES - community_oauth** | **B secrets** |
| 6 | `community_connections.py:263` `_sync_upsert` (OAuth callback `blueprints/v1/community_connections.py:317`) | R+W (FOR UPDATE) | writes sealed tokens | YES (ciphertext in) | **B secrets** |
| 7 | `community_connections.py:385` `_sync_delete` (DELETE route) | R+W | nulls tokens, `is_active=false` | no secret read | **B (write-only)** |
| 8 | `community_connections.py:471` `_sync_reseal_legacy` | R+W | reseal old/legacy ciphertext | YES | **B secrets** |
| 9 | `community_connections.py:590` `_sync_store_refreshed` (youtube refresh) | R+W | `access_token`, `expires_at` | YES | **B secrets** |
| 10 | `services/erasure_registry.py:427,475` | none at runtime | declarations only (`review()` gap: user_oauth rows not erasable by hub-api today) | n/a | follow-up |
| 11 | `scripts/reseal_connections.py:109-120` | R+W raw SQL | tokens | YES | out of scope: admin script, confirm it uses owner creds, not `waddles_hub_api` |

Not hub-api runtime: `core/credential_manager_module` (role `mod_credential_manager`, policy exists).

Crypto is app-side: AES-256-GCM with `CREDENTIAL_ENCRYPTION_KEY` + per-row AAD in hub-api. SQL functions
only ever move ciphertext. They never decrypt and never need the key.

## 2. SECURITY DEFINER surface

Common to ALL functions:
- `SECURITY DEFINER`, `SET search_path = pg_catalog, pg_temp`; every object schema-qualified `public.*`.
- Owner = the migration owner (`waddlebot`, DDL owner running the db-migrate Job). It is superuser/BYPASSRLS
  today, so FORCE RLS does not apply to the definer. Migration asserts
  `rolsuper OR rolbypassrls` for `current_user`; otherwise it creates a policy `TO <owner>` for the exact
  verb (test covers both).
- `REVOKE ALL ON FUNCTION ... FROM PUBLIC; GRANT EXECUTE ... TO waddles_hub_api` only (guarded `IF EXISTS role`).
- Every integration-type predicate is hard-coded in the function body, never a parameter.
- Parameters validated (non-null, `p_platform` in an allow-list, ids > 0); violations `RAISE ... ERRCODE 22023`.
- Group A never reads `access_token`/`refresh_token`/`client_secret` columns, so they cannot be returned.

### Group A: metadata (no secret columns exist in the return type)
| Function | Returns | Never returns |
|---|---|---|
| `pi_list_metadata(p_integration_type text DEFAULT NULL, p_platform text DEFAULT NULL)` | `id, platform, integration_type, community_id, user_id, access_token_set bool, refresh_token_set bool, client_id, client_secret_set bool, token_type, expires_at, scopes, config_data*, is_active, is_encrypted, created_at, updated_at, created_by_user_id, updated_by_user_id`; only `is_active` rows | `access_token`, `refresh_token`, `client_secret` (presence booleans only) |
| `pi_community_status(p_community_id int, p_platform text DEFAULT NULL)` | same shape, hard-coded `integration_type='community_oauth' AND is_active` | same |

`*` `config_data` is JSONB written by other components. Function strips keys matching
`(?i)(secret|token|password|key|credential)` recursively before return (defence in depth). Implementation
must first audit real `config_data` contents for keys the UI needs (e.g. account label).

### Group B: community_oauth secrets (ciphertext only, one row per call)
| Function | Behavior | Never |
|---|---|---|
| `pi_community_secret_get(p_community_id int, p_platform text)` | one active `community_oauth` row; returns ciphertext `access_token, refresh_token, client_id, client_secret?, token_type, expires_at, scopes, is_encrypted, seal_version`; appends `credential_access_log` row (`session_user`, platform, `community_oauth`; exact columns to confirm) | no set-returning/scan variant; never `bot`/`user_oauth` rows |
| `pi_community_upsert(p_community_id, p_platform, p_access, p_refresh, p_client_id, p_client_secret, p_token_type, p_expires_at, p_scopes, p_config, p_is_encrypted, p_seal_version, p_actor_user_id)` | locks row, insert-or-update, `integration_type='community_oauth'`, `user_id NULL`; refuses downgrade (`seal_version` decrease or `is_encrypted` true to false). Returns `(id, prior_seal_version, prior_is_encrypted)` | cannot touch bot/user rows (predicate + CHECK `platform_integrations_scope_check`) |
| `pi_community_disconnect(p_community_id, p_platform, p_actor_user_id) returns boolean` | nulls secrets, `is_active=false` (current `_sync_delete` semantics) | returns no secrets |
| `pi_community_set_sealed(p_community_id, p_platform, p_expected_access text, p_access, p_refresh, p_expires_at, p_seal_version, p_actor)` | compare-and-set on `p_expected_access` (race-safe refresh/reseal); serves sites 8 and 9 | returns only `boolean` updated |

Granted to `waddles_hub_api` only. The existing `credential_manager_module` does NOT provide a secret path:
`app.py` exposes only `/api/v1/credentials/status` and `/refresh-now`, and it is deployed only in beta values
(`values-beta.yaml`, kustomize base), not alpha. So a new SQL function (ciphertext only) is the route, as agreed.

### Group C: BLOCKED, flagged (do not widen)
Site 4 (`test_platform_connection`) needs the decrypted operator BOT token. A bot-lane secret function would
hand hub-api exactly the credentials H-3 protects. Options, my recommendation first:
1. Move the "test" into `credential_manager_module` (`POST /api/v1/credentials/<platform>/test`, machine JWT
   scope `connections:admin`); hub-api forwards the platform name only. Needs the module deployed on alpha;
   until then hub-api returns a loud 503 `credential-manager unavailable` (fail-loud, no stub).
2. Drop the test endpoint for now.
3. `pi_bot_secret_get` (REJECTED unless you explicitly approve: re-opens H-3 for bot tokens).

## 3. Why this does not re-open H-3
- Role still has zero table privileges: `deny_tables` is unchanged and
  `alembic/tests/test_0055_per_service_db_roles.py:275` stays green.
- Metadata callers (sites 1-3, the superadmin 500 fix) call functions whose return types contain no secret
  column. A bug in that code path cannot return a raw token.
- Operator BOT credentials are reachable by no hub-api function (Group C stays closed).
- Secret callers get ciphertext for a single named `(community_id, platform)` community_oauth row. There is
  no list/scan function and every read is audited. Bulk exfiltration needs one call per row, each logged.
- Residual risk, stated plainly: hub-api owns the OAuth flow and the `CREDENTIAL_ENCRYPTION_KEY`, so a fully
  compromised hub-api can still fetch and decrypt community OAuth tokens one at a time. That is inherent to
  hub-api doing OAuth; the boundary protects operator credentials and bulk access, not per-community tokens
  against hub-api compromise.
- SECURITY DEFINER hygiene: pinned `search_path`, schema-qualified, no dynamic SQL, no `custom_grants`-style
  escape hatch, EXECUTE revoked from PUBLIC, single grantee.

## 4. Migration and reach (existing alpha DB)
- New Alembic `0069_pi_definer_functions` (25 chars), `down_revision = "0068_app_catalog_kind"`, heads must stay 1;
  explicit `downgrade()` drops the functions. Uses `CREATE OR REPLACE FUNCTION`, so it is idempotent. Guards:
  `to_regclass('public.platform_integrations')`, role-exists checks for the GRANT.
- Existing alpha: `run-alembic.sh` (db-migrate Job as owner) runs `alembic upgrade head`; alpha's
  `alembic_version` is at 0068, so 0069 applies. This is the Alembic chain, not the fresh-only SQL replay.
- Belt and braces: add the function names to `waddles_hub_api.functions` in `service-roles.yaml`. The
  `reconcile --strict` step that runs on EVERY migrate Job (`service_roles.py:679-688`) re-grants EXECUTE if
  missing. `deny_tables` is untouched.
- Fresh DB: same chain, same result; the minimal CI chain skips the role grant when the role is absent.

## 5. Refactor and test plan
Refactor: new `services/platform_integrations_store.py` (typed dataclass DTOs, `@dataclass(slots=True)`)
wrapping the functions via the DAL's `executesql` with bound parameters. No other module touches the table.
- Group A: sites 1-3 switch to `pi_list_metadata` / `pi_community_status`; `_mask` becomes a pass-through.
- Group B: sites 5-9 switch to the B functions; crypto/AAD/downgrade logic stays in `community_connections.py`
  (SQL only enforces monotonic seal).
- Group C: per option chosen. Remove `dal.platform_integrations` use from `hub_api` entirely.

Tests (all `# regression: gh-969`; Postgres via `alembic/tests/pg_docker.py`, the real `waddles_hub_api` role):
1. `waddles_hub_api` `SELECT`/`INSERT` on the table is still denied (existing test unchanged + new INSERT/UPDATE/DELETE).
2. `pi_list_metadata` / `pi_community_status`: seed rows with distinctive token strings; assert the returned
   columns exactly equal the allowed set and no value contains the seeded secrets, including via `config_data`
   containing a `"token"` key (scrubbed). NEW secret-leak test.
3. `pi_community_secret_get` returns ciphertext for community_oauth only; returns nothing for bot/user_oauth
   rows with the same platform; writes an audit row.
4. Upsert/disconnect/set_sealed: bot rows untouched, downgrade refused, CAS miss returns false.
5. `PUBLIC` and other roles (`waddles_svc_*`, legacy) cannot EXECUTE any function.
6. API-level: `GET /api/v1/superadmin/platform-config` 200 under the real runtime role (the original 500).
7. Fresh-replay + upgrade-from-0068 both apply; `alembic heads == 1`; reconcile `--strict` green.
8. Existing `community_connections` unit/seal tests and `test_seal_callers_guard.py` stay green.

## Out of scope / follow-ups
- Erasure of `platform_integrations.user_id` rows (`erasure_registry.py` `review()` gap): needs its own
  definer `pi_erase_user(p_user_id)`; separate PR.
- `scripts/reseal_connections.py`: confirm it runs with owner credentials.

## Open questions for the reviewer
1. Group C option (1, 2 or 3)?
2. Does `get_decrypted_tokens` truly need `client_secret` (refresh)? If not, drop it from `pi_community_secret_get`.
3. `credential_access_log` insert from a definer function: accepted for the audit trail?

## Implementation notes (as built)
- Q1 (Group C): option 1 -- `POST /api/v1/credentials/<platform>/test` in `core/credential_manager_module`;
  hub-api forwards the caller's bearer and returns a loud 503 `CREDENTIAL_MANAGER_UNAVAILABLE` when it is
  unreachable (not deployed on alpha today). No bot-secret function exists.
- Q2: `client_secret`/`client_id` are NOT needed by refresh (`get_decrypted_tokens` and
  `store_refreshed_access_token` only touch access/refresh tokens; provider app credentials come from the
  platform's own env/connection store) -- dropped from `pi_community_secret_get`.
- Q3: audit rows (`credential_access_log`, `db_user = session_user`, ids/op/outcome, `detail` NULL) are written
  by every Group B function, including set_sealed/upsert/disconnect.
- `pi_community_set_sealed` takes no actor and a `p_touch_updated_at` flag; NULL refresh/expiry leave the stored value.
- SQLite (dev `DB_TYPE=sqlite`) has no roles, so `services/platform_integrations_store.py` implements the
  same contract on the pydal table there, selected by adapter engine (never a runtime fallback; other engines raise).
- Minimal CI/test bootstrap gained `credential_access_log` (pg_docker.py + pr-validation.yml) because 0069 audits into it.

## Review-round changes (external review of PR #971)
| Finding | Change |
|---|---|
| `config_data` denylist leak | `pi_scrub_config` replaced by ALLOWLIST `pi_public_config` (only string `account_label`; audit found no other key written by any component). SQLite mirror `_public_config`. |
| Group B caller authorization / spoofable actor | `community_id` always from validated scope (route `community_in_tenant`, signed OAuth `state`, community-bound machine JWT) with an invariant test; every Group B fn + `pi_community_status` takes the VALIDATED `p_tenant_id` and the DB rejects a community of another tenant (`communities.tenant_id`); NULL is refused; system callers resolve the community tenant explicitly. `p_actor_user_id` REMOVED -- no end-user attribution is persisted (DB cannot verify it). |
| Audit gaps | Every Group B call audits success AND every refusal (`invalid_args`, `forbidden_tenant`, `not_found`, `stale`, `seal_downgrade`); refusals are RETURNED (`outcome`), not raised, so the audit row commits. Caller platform text is never logged. |
| EXECUTE drift | catalog lists exact signatures; `service_roles.py` grants by `to_regprocedure` (bare names still work for legacy identity fns); migration drops stray `pi_*` overloads, re-owns, revokes PUBLIC + every non-owner grantee, then grants hub-api by signature. |
| Bearer SSRF | `credential_manager_client.validate_origin`: mandatory https; no userinfo/query/fragment/IP/loopback/path; optional `CREDENTIAL_MANAGER_ALLOWED_HOSTS`; `trust_env=False`, no redirects; bad origin = loud 503, bearer never sent. Machine-token minting does not exist in hub-api -> bearer forwarding kept, boundary documented (follow-up). |
| Definer owner | dedicated `waddles_pi_definer` NOLOGIN NOSUPERUSER BYPASSRLS role owns every function; least-privilege table grants (no DELETE); superuser migrator asserted up front; hub-api cannot SET ROLE to it. |

## Review round 2
| Finding | Change |
|---|---|
| NULL tenant bypass (HIGH) | `p_tenant_id` is REQUIRED (no default) on every Group B function + `pi_community_status`; `pi_tenant_ok` FAILS CLOSED on NULL. hub-api: service/store functions take a required keyword `tenant_id` = validated tenant id or the explicit `Scope.SYSTEM` marker (never None, TypeError otherwise); `SYSTEM` resolves the tenant from the community row and passes THAT id (unknown community -> `-1`, refused + audited). User routes pass `ctx.tenant_id`; callback passes the tenant bound in the signed OAuth `state` (absent -> `save_failed`). AST test asserts every call site passes `tenant_id=` from an allowed expression. |
| Definer-role memberships (HIGH) | migration revokes every membership of `waddles_pi_definer` in both directions (`REVOKE ... GRANTED BY`) and asserts none remain (fail loud). |
| credential-manager URL (MED) | `CREDENTIAL_MANAGER_URL` required (no default); HTTPS only, including in-cluster calls; no plaintext override; optional `CREDENTIAL_MANAGER_CA_FILE` trusts an operator-managed internal CA; metadata/IP/loopback rejects, `trust_env=False`, no redirects, 503 fail-closed kept. |
| `pi_list_metadata` gate | only caller is `GET /superadmin/platform-config`, gated by `users:admin` (global admin bundle only, never the tenant bundle); tested for all tenant bundles. |
| bare-name catalog grants | the two legacy identity functions now listed by exact signature too. |

## Deployment availability
The credential manager has no deployed Helm workload in this branch. Bot test-connection remains a loud 503 until an operator supplies a reachable HTTPS `CREDENTIAL_MANAGER_URL` and, for a private CA, `CREDENTIAL_MANAGER_CA_FILE`. Adding the workload, its per-service database role, TLS listener, Redis TLS support and alpha image build is a separate deployment change; this PR does not claim that the bot test is functional on alpha.

## Recovery verification (Codex, 2026-10-30)
- Independent Codex review reproduced a migration failure with `OWNER -> hub-api WITH ADMIN OPTION -> svc-action` grants (`DependentObjectsStillExist`). Membership cleanup now uses `CASCADE` and re-reads the catalog after each revoke, avoiding stale grants whose grantor lost ADMIN OPTION. The real-PostgreSQL regression covers that dependent chain and both membership directions.
- Mandatory HTTPS has no plaintext allowlist escape. The client validates finite positive timeout and strict JSON boolean verdicts. Optional private CA files are loaded into a hostname-verifying TLS context; absent a private CA, the client retains HTTPX's certifi public-root bundle.
- Helm exposes `pipeline.hubApi.credentialManager.{url,timeoutSeconds,caSecretName}` for a separately deployed HTTPS service; only `ca.crt` is projected read-only. This does not deploy the backend itself.
- Gemini and Ollama reviewed relevant security source with explicit user authorization. Gemini's timeout concern is covered by the base `timeoutSeconds: 15`; unknown/missing grantor assumes catalog corruption and migration postconditions fail closed. OAuth NULL-refresh/expiry semantics predate the security change and remain intentional (re-authorization replaces credentials; refresh without a new expiry preserves the old value). Ollama's redundant `_tenant` commit was removed; SQLite remains a documented development-only path; `aclexplode` is a PostgreSQL builtin, not an extension. Hash lock collisions only serialize unrelated writes and do not weaken correctness. Metadata Group A does not expose secrets; auditing remains mandatory for all Group B calls.

Validation after the recovery fix: full 0069 real-PostgreSQL suite **20 passed** (including reproduced chained-grant regression); hub-api focused regression suite **195 passed**; real restricted hub-api role integration **7 passed**; service-role catalog **33 passed**; credential-manager Helm render assertions **5 passed**. Alpha and dal2alpha render successfully; alpha Helm lint passes. Kubeconform: **71 valid, 0 invalid, 8 skipped CRDs**. Other environment overlays correctly refuse rendering without their operator-managed TLS material. All changed Python passes Ruff; `git diff --check` passes. This is focused validation, not the full `make pre-commit` / coverage / live OTel merge gate. PR #971's published b58c7c31 checks still show build-all/CI Gate/PR Validation failures; build-all's failing job is `build connector_e2e` (124352677961). No push, merge or cluster deploy performed.

Grant-chain re-review: independent Codex found the CASCADE/live-reselect change resolves its finding with no new high/medium issue. Ollama agreed the drain loop is sound. Gemini repeated an order/stale-row concern that the live-reselect specifically prevents: after upstream CASCADE, dependent rows are absent and cannot be selected. Its grantor-only concern is covered by a further real-PostgreSQL test: the owner holds ADMIN OPTION on `pi_parent`, grants that role onward, then removing the owner's parent membership cascades those grants; the test asserts no remaining owner group/member/grantor records. Catalog-corruption and concurrent hostile superuser mutations are outside the supported migration contract, whose final invariant still fails closed.
