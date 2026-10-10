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
      sso.py                    v1 enterprise SSO: public login flows (SAML/OIDC/Google) + tenant-admin connection CRUD
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
    current_user.py         resolve the caller's user id from the bearer JWT
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

## Enterprise SSO (SAML 2.0 / OIDC / Google)

Tier-gated (`waddles.auth.sso_saml` Enterprise, `waddles.auth.sso_google` Professional, both default OFF).
Full deployment, configuration, security model and troubleshooting guide:
[`docs/SSO.md`](../docs/SSO.md). Tests live in `tests/sso/` (OIDC/SAML run against protocol-faithful fake
IdPs -- only the IdP socket and Redis are replaced) plus a real-Postgres migration test in
`alembic/tests/test_0049_sso_connections.py`; run with
`python3 -m pytest tests/sso -q --cov=services --cov=blueprints.v1.sso`.

## OpenAPI (two documents, per backend.md)

- `GET /openapi/v1-public.json` -- unauthenticated, exactly one path (`POST /api/v1/auth/login`).
- `GET /openapi/v1.json` -- the full, quart-schema-generated document, behind
  the same `tenant_middleware` + `require_scope("platform:read")` chain as
  any other protected route. The default `quart-schema` doc routes
  (`/openapi.json`, `/docs`) are disabled in `app.py` -- they are
  unauthenticated by default and would otherwise expose the whole surface.

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
