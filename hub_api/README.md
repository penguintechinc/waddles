# hub-api

Python3/Quart control-plane service -- Node hub_module / marketplace_module
port per
[docs/plans/2026-08-31-hubapi-node-to-quart-migration.md](../docs/plans/2026-08-31-hubapi-node-to-quart-migration.md).
M0 (Foundation) laid the app skeleton; **M1 (Core Identity/Auth) is the
first real controller-group port** -- auth, identity, passkey,
userManagement, profile -- and the pattern-prover for the remaining M2..M9
groups. **Read [`PORTING.md`](PORTING.md) before porting the next group.**

## Layout

```
hub_api/
  app.py              Quart app factory + hypercorn entry point
  config.py            env-driven HubAPIConfig (@dataclass(slots=True, frozen=True))
  blueprints/
    __init__.py         register_blueprints(app) -- mounts v1 + v2 routers
    v1/
      auth.py             v1 `auth` group (M1): local/admin/temp-password login, OAuth, /me, refresh
      identity.py           v1 `user identities` group (M1): linked-platform CRUD
      passkey.py             v1 `user passkey` group (M1): WebAuthn credential management
      profile.py              v1 `user profile` group (M1): self-service profile + avatar
      user_management.py       v1 `superadmin users` group (M1): platform user CRUD
      compliance_audit.py      v1 `compliance audit` group: Enterprise read/verify/export of the audit chain
      sso.py                    v1 enterprise SSO: public login flows (SAML/OIDC/Google) + tenant-admin connection CRUD
      provider_connections.py    v1 provider connections: per-community CRUD + data-plane list/resolve (machine JWT)
    v2/
      platform.py          example v2 group: tenant -> scope -> DTO chain, exposes BLUEPRINTS
  routers/
    v1.py                frozen /api/v1 -- AUTO-DISCOVERS blueprints/v1/*, never edited per port
    v2.py                additive /api/v2/{module}/{surface}/{app_bundle}/{target} -- same, blueprints/v2/*
    _discovery.py         shared pkgutil-based discover_blueprints(package) helper
  services/
    schema.py             pydal table bindings for the M1 auth/identity/passkey/profile group
    auth_service.py         local/admin/temp-password login, session/JWT issuance, tenant login info
    oauth_service.py         OAuth login/link (Discord/Twitch/Slack)
    identity_service.py      linked-identity CRUD + identity-linking OAuth flow
    passkey_service.py       WebAuthn registration/authentication
    profile_service.py       self-service profile CRUD + avatar
    storage_service.py       S3-compatible avatar object storage (no local-disk fallback)
    user_management_service.py  superadmin user CRUD
    sso_service.py           enterprise SSO orchestration: entitlement-gated connection CRUD, login start/complete, JIT provisioning
    sso_oidc.py / sso_saml.py  protocol layers: OIDC code+PKCE with local ID-token validation; SAML 2.0 SP (XSW/replay/XXE defences)
    sso_http.py / sso_state.py / sso_crypto.py / sso_settings.py / sso_telemetry.py / sso_types.py  SSRF-guarded IdP HTTP, Redis single-use state, AES-GCM secrets, operator settings, OTel + PII-safe logging, shared types
    provider_connections/   standard provider/connection framework: adapter contract, Twitch adapter, registry, vault, store, service
    current_user.py         resolve the caller's user id from the bearer JWT
    audit_chain.py          tamper-evident audit hash chain: canonical form, seal, verify (stdlib only)
    audit_events.py         audit vocabulary, PII-free event validation, request classification
    audit_service.py        append / list / verify the chain (entitlement-gated, fail-loud, OTel)
    audit_http.py           after_request hook + session-issuance hook (HTTP audit coverage)
    bundle_audit.py         bundle-lifecycle audit helper: legacy `audit_log` row + chain bridge
    dto_response.py         jsonify_dto() -- workaround for a quart-schema/pydantic-core crash
    errors.py              ApiError + bad_request()/unauthorized()/etc factories
  openapi/
    spec_builder.py       hand-curated public login-only OpenAPI document
    routes.py             /openapi/v1-public.json (public) + /openapi/v1.json (protected, generated)
  tests/                 75+ tests: app factory, health, platform blueprint, OpenAPI split, discovery, M1 group
  requirements.in/.txt   hash-pinned direct deps (flask_core installed separately, see Dockerfile)
  Dockerfile             multi-stage, rootless, hypercorn CMD
  PORTING.md             the M1 recipe -- read before porting M2..M9
```

### Porting a controller group (the extension point)

Add exactly ONE file: `blueprints/v1/<group>.py` or `blueprints/v2/<group>.py`,
exposing a module-level `BLUEPRINTS: list[Blueprint]` (each blueprint's
`url_prefix` already the full `/api/v{1,2}/...` path). `routers/v1.py` and
`routers/v2.py` auto-discover every module in their respective
`blueprints/v{1,2}/` package (`routers/_discovery.py`, sorted by module
name for deterministic registration order) and mount whatever
`BLUEPRINTS` it exposes -- a module without one is skipped with a log
line, not an error.

**Never edit `routers/v1.py`, `routers/v2.py`, or `blueprints/__init__.py`
to add a group** -- those three files are shared infrastructure, not a
per-port touch point. This is what makes ~10 parallel port agents safe:
every agent's PR adds a new file under `blueprints/v{1,2}/`, so there is
no shared file to collide on.

## Running locally

```bash
uv venv -p 3.13 .venv && source .venv/bin/activate
uv pip install --require-hashes -r requirements.txt
uv pip install -e ../libs/flask_core   # local dev only -- Dockerfile pip-installs it non-editable
python3 app.py                          # or: hypercorn app:app --bind 0.0.0.0:8204
```

## Testing

```bash
uv pip install pytest pytest-asyncio mypy ruff
python3 -m pytest tests/ -v
ruff check . && ruff format --check .
python3 -m mypy .
```

Most tests use `sqlite:memory` (pydal). The M1 group's own tests
(`test_v1_auth_blueprint.py` and friends) use a `tmp_path`-backed sqlite
**file** instead, `pool_size=1` -- see `PORTING.md`'s async_dal testing
gotcha for why `sqlite:memory` breaks once a route calls
`async_dal.select_async`/`insert_async`/etc.

## Enterprise audit logging (tamper-evident)

Security-relevant events (authz denials, tenant/role changes, DSAR/erasure, logins incl. SSO and
passkey, admin actions, license changes) are appended to a per-tenant **SHA-256 hash chain**
(`audit_events`, alembic `0053_audit_events_hash_chain`): altering, removing, inserting or
re-ordering any record is detected by re-verification. Recording and reading are an **Enterprise**
feature (flags `waddles.compliance.audit_logs` / `audit_export`, default OFF); the legacy
`audit_log` basic trail and the statutory DSAR/erasure rights are never gated. A failed audit write
is never swallowed -- it logs at ERROR with a traceback, bumps `waddles.audit.write_failures`, and
fails the request (entitled tenants only).

```bash
make verify-audit-chain                   # recompute every chain from the DB (exit 1 = tampering)
make verify-audit-export EXPORT="a.json"  # verify a downloaded export offline, no server needed
```

API: `GET /api/v1/compliance/audit/{events,head,verify,export}` (scope `compliance.audit:admin`).
Full design, threat model and operating guide: [`docs/compliance/audit-logging.md`](../docs/compliance/audit-logging.md).

## Enterprise SSO (SAML 2.0 / OIDC / Google)

Tier-gated (`waddles.auth.sso_saml` Enterprise, `waddles.auth.sso_google` Professional, both default OFF).
Full deployment, configuration, security model and troubleshooting guide:
[`docs/SSO.md`](../docs/SSO.md). Tests live in `tests/sso/` (OIDC/SAML run against protocol-faithful fake
IdPs -- only the IdP socket and Redis are replaced) plus a real-Postgres migration test in
`alembic/tests/test_0049_sso_connections.py`; run with
`python3 -m pytest tests/sso -q --cov=services --cov=blueprints.v1.sso`.

## Provider connections (Twitch first)

One adapter interface, one connection API and one encrypted per-community token store for every
external platform. Builds on the three-layer connection tables from migration 0035 (adds
`0058_provider_connections`: per-link `resource_metadata` / `expires_at` / `status_reason`, per-grant
`enabled`). Gated by scope `connections:admin`, flag `waddles.integrations.provider_connections`
(default OFF) and the Professional tier. Tokens are AES-256-GCM ciphertext under
`CREDENTIAL_ENCRYPTION_KEY` and are decrypted only at the point of use; the data plane
(`svc-ingest`) gets a fresh access token through machine-JWT-authenticated `/api/v1/internal/...`
routes. Full guide, API, security model, data-plane contract and "adding a provider" checklist:
[`docs/PROVIDER_CONNECTIONS.md`](../docs/PROVIDER_CONNECTIONS.md). Tests: `tests/provider_connections/`
(only the Twitch socket is faked) plus real-Postgres tests in `tests/provider_connections/
test_pg_integration.py` and `alembic/tests/test_0058_provider_connections.py`; run with
`python3 -m pytest tests/provider_connections -q --cov=services.provider_connections
--cov=blueprints.v1.provider_connections --cov-branch`.

## OpenAPI (two documents, per backend.md)

- `GET /openapi/v1-public.json` -- unauthenticated, exactly one path (`POST /api/v1/auth/login`).
- `GET /openapi/v1.json` -- the full, quart-schema-generated document, behind
  the same `tenant_middleware` + `require_scope("platform:read")` chain as
  any other protected route. The default `quart-schema` doc routes
  (`/openapi.json`, `/docs`) are disabled in `app.py` -- they are
  unauthenticated by default and would otherwise expose the whole surface.

## Tier-gated surfaces

Gates are `flask_core.feature_flags.feature_enabled` (PostHog flag AND license tier, fail-closed), keyed on contracts in `libs/core_platform_module/features.py`.

- **Admin/bulk DSAR console (Enterprise, `compliance.bulk_dsar`)** -- `blueprints/v1/admin_data_privacy.py` + `services/admin_data_privacy_service.py`, mounted at `/api/v1/tenant/<slug>/privacy/*`. The statutory self-service DSAR (`/api/v1/user/me/data`) stays ungated in every tier. See `docs/PRIVACY.md` "Admin / Bulk DSAR Console".
- **Whitelabel branding (Professional, `tenancy.whitelabel`)** -- `services/branding_service.py`, applied in `GET /api/v1/auth/tenant/<slug>` and gated on write in `PUT /api/v1/tenant/<slug>`. See `docs/features/whitelabel-branding.md`.

## Data-subject erasure (GDPR Art. 17)

`services/data_privacy_service.py::anonymize_user_data` is the single erasure core, shared by self-service (`DELETE /api/v1/user/me/data`) and the Enterprise admin console.

- **All-or-nothing** -- every delete, the `hub_users` anonymization and the `completed` `data_deletion_requests` row run as ONE executor job with ONE commit (rollback on any failure). `AsyncDAL.delete_async()` & co. commit per call, so they are deliberately not used here. A failed erasure leaves the account untouched, records a `failed` ledger row (exception type only) and re-raises.
- **Erases `hub_chat_messages`** the user sent (previously exported, never deleted -- GRC#1).
- **Nulls `community_members` PII** (`display_name`, `avatar_url`, `bio`, `social_links`, `platform_user_id`; `COMMUNITY_MEMBER_PII_COLUMNS`) on the subject's membership rows in every community -- hub-linked rows (`user_id == str(hub_users.id)`) and platform-keyed rows matching a linked `hub_user_identities`. The rows stay (reputation/role retention); a bound-column drift fails the erasure instead of leaving PII behind. Counted as `community_members_anonymized` in the ledger and the `hub.dsar.erase` span (not as deleted rows).
- **Telemetry (OTLP via the standard `OTEL_*` env vars)** -- span `hub.dsar.erase`, counter `waddles_hub_dsar_erasure_total{result}`, histograms `waddles_hub_dsar_erasure_duration_ms` / `waddles_hub_dsar_erasure_rows`. Labels/attributes carry no user id; failure spans carry the exception type only (never the driver message). A broken exporter is logged, never fails an erasure.
- **Retains consent + audit evidence** (`cookie_consent`, `cookie_audit_log`, `audit_log`, `data_deletion_requests`; `ERASURE_RETAINED_TABLES`) for GDPR Art. 5(2). See `docs/PRIVACY.md` "Data Deletion". Regression tests: `tests/test_data_privacy_erasure.py`.

## API versions

- `/api/v1/*` -- frozen, ported 1:1 from the Node contract
  (`admin/hub_module/frontend/src/services/api.js` is the pinned source of
  truth). M1 (auth, identity, passkey, profile, superadmin users) is
  live; M2..M9 land phase by phase per the migration plan.
- `/api/v2/{module}/{surface}/{app_bundle}/{target}` -- additive,
  bundle-oriented. `blueprints/v2/platform.py` is mounted at
  `/api/v2/core/platform/default/*` as the one worked example.

## Known gaps (tracked, not fixed here)

- `libs/flask_core` ships no `py.typed` marker and `tenant_middleware`/
  `require_scope`'s inner wrappers are unannotated -- every consumer sees
  mypy `--strict`'s `untyped-decorator`; worked around here with scoped,
  documented `# type: ignore[untyped-decorator]` (see `blueprints/v2/platform.py`,
  `openapi/routes.py`).
- `AsyncDAL.close_async()` (`libs/flask_core/flask_core/database.py`)
  closes the underlying pydal `DAL` from a different thread than created
  it, which pydal's thread-local bookkeeping rejects. `app.py`'s shutdown
  hook catches and logs this rather than crashing the ASGI lifespan
  (matching `services/core-community/app.py`'s existing pattern).
- gRPC (backend.md's mandatory service-to-service transport) is not wired
  in this scaffold -- REST + MCP only. Follow-up, not blocking M1.
- `quart-schema==0.25.0` + `pydantic==2.13.x`: `POST .../shoutout/creators`
  (`blueprints/v1/bot.py::add_shoutout_creator`, M5) reproducibly hits
  `TypeError: 'None' is not an instance of 'SchemaSerializer'` in
  `quart_schema`'s app-level second response-dump pass, specific to this
  one route once registered on the real `bot_bp` (bisected -- an
  equivalent hand-built route with identical DTOs does not reproduce it;
  root cause not identified). Worked around by skipping
  `@validate_response` on that single handler and building the response
  dict via `dataclasses.asdict` instead -- the DTO (`ShoutoutCreatorResponse`)
  stays the documented, OpenAPI-generated contract; only the runtime
  double-validation is skipped. See the handler's own docstring.
