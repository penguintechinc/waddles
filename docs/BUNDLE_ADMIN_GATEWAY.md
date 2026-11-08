# Bundle-Admin Gateway (hub-api)

hub-api is the **site foreman**: it never hosts consumer features. Every app bundle's admin UI
(webui) talks to one generic gateway, driven entirely by the **active bundle's manifest**.

```
webui --> hub-api gateway --(signed Valkey request)--> host (svc-action) --> service bundle tool
           authz + schema + rate limit + audit          verifies MAC, P1 matrix, required_scope
```

Code: `hub_api/blueprints/v1/bundle_admin_gateway.py`, `services/bundle_admin_gateway_service.py`,
`services/bundle_tool_transport.py`. Migration `0085_bundle_community_config`.

## Manifest blocks (both optional, additive)

```yaml
# JSON Schema (Draft 2020-12, type: object, <= 64 KiB) for per-community config.
# NOTE: distinct from the legacy `config_schema:` field-map (149 manifests carry `{}`), untouched.
community_config_schema:
  type: object
  properties: {greeting: {type: string, maxLength: 20}}
  required: [greeting]
  additionalProperties: false

admin_tools:               # tools the admin UI may call
- name: board              # gateway handle (URL segment)
  tool: leaderboard        # exported tool name on target_app_id
  kind: read               # read = paginated data view | action = audited write path
  scope: community:moderate   # community:moderate | community:admin
  target_app_id: waddles.core.service.reputation   # default: this bundle (service bundle)
```

Rules enforced at ingest (`parse_bundle_manifest_v2`): a tool on **another** bundle must also be in
`requested_tools` (same consent/approval path); a tool on **this** bundle must be in
`exported_tools`; owner/member scopes are not allowed. Input/output schemas are **not** duplicated:
the gateway validates against the target's `exported_tools` schemas. Reference:
`bundles/rust/reputation/{bundle,hub-manifest}.yaml` (`leaderboard` read, `override` action).

## Routes

Mount `/api/v1/apps/community/<community_id>/bundles/<app_id>`

| Route | Needs | Notes |
|---|---|---|
| `GET /admin` | moderator | schema + tools + `permitted` per tool |
| `GET /config`, `GET /config/history?limit=` | moderator | `version: 0` = never saved |
| `PUT /config` `{config, expectedVersion}` | admin | 422 `config_invalid` (value-free `{path, rule}`), 409 `config_version_conflict`, 413 |
| `POST /admin-actions/<tool>` `{args}` | manifest `scope` | args validated vs target `input_schema`, output vs `output_schema` |
| `GET /data/<view>?limit=&cursor=&args=<json>` | manifest `scope` | `limit` (1..100, default 25) / `cursor` forwarded only if the tool's input schema declares them; `next_cursor` passed through as `nextCursor`; target must be `access: read` |

Authz order: `tenant_middleware` -> DB-backed community membership (`authorize_community`,
tenant ownership re-checked) -> manifest-declared scope (`community:moderate` = `community:manage_channels`
or `manage_members`; `community:admin` = `manage_members`; platform/tenant admin bypass). There is
no flat JWT scope: a community admin never carries one at issuance (`services/community_authz.py`).
The bundle must be **active** for the community (or tenant-wide), else 404 `bundle_not_active`.

Rate limit per user/community/bundle/tool via the shared `RateLimiter` (actions 30/min, reads
120/min); a missing limiter is 503, never an open door.

## Config storage

`bundle_community_config` is append-only: a save INSERTs `version = expectedVersion + 1`;
`UNIQUE (tenant_id, community_id, app_id, version)` makes concurrent saves safe (loser = 409).
Highest version is live; rollback = save an older body as a new version.

## Audit and PII

Write-access or `dangerous` tool calls and config saves are audited **before** the call (fail closed:
if the audit write fails, nothing runs) and again with the outcome (`bundle_admin.tool_invoked`,
`bundle_admin.config_saved`; chain + legacy `audit_log`). The HTTP hook additionally records
`bundle.admin_tool_invoked` / `bundle.config_changed`. Details carry app id, tool name, community id,
version/outcome code only -- never args, config, or results. Validation errors carry no values.

## Needs from S0 (host seam)

The host has **no** path for hub-api to invoke a bundle tool: `tool.invoke` exists only as a guest
host-call inside the executor (`core/svc_action/src/capabilities/tool.rs`, `core/svc_process/...`),
and svc-action's HTTP surface is health-only. hub-api therefore enqueues a signed request, modelled
on event sync (`event_sync_connector_client.py`); the host must add the consumer:

* `BRPOP`/consume `waddles:bundle-admin:requests`; reply `LPUSH waddles:bundle-admin:result:<request_id>`
  `{"ok":true,"result":{...}}` or `{"ok":false,"code":"<[a-z][a-z0-9_.]*>"[,"tool_error":true]}`
  (TTL the result key; `tool_error:true` -> hub-api 422, otherwise 502).
* Request: `{v:1, request_id, tenant_id, community_id, actor_user_id, actor_scope, caller_app_id,
  target_app_id, tool, args_json, exp_ms, sig}`; `args_json` is canonical (sorted keys, compact).
* Verify `sig` (`kid:hex`, HMAC-SHA256, label `waddles.bundle-admin.v1`, 4-byte-big-endian-length-prefixed
  fields in the order of `bundle_tool_transport.sign_call`) against key ring `BUNDLE_ADMIN_REQUEST_KEYS`
  (`kid:hexkey,...`, `BUNDLE_ADMIN_REQUEST_ACTIVE_KID`); reject expired `exp_ms` (TTL 120 s).
* Treat `actor_scope` as the host-verified actor role for `required_scope` tools (e.g. reputation
  `override` = `community:admin`); run the same P1 matrix / `tool.call` gate as a guest `tool.invoke`
  with `caller_app_id` as the caller; tenant/community come from the signed request, never `args`.
* Provision `BUNDLE_ADMIN_REQUEST_KEYS` to hub-api (secret) and the consumer (Helm/seeder wiring).

Until that consumer ships, calls fail loud (504 `bundle_tool_timeout` or 503
`bundle_tool_transport_unconfigured`); nothing falls back to a direct call.

## Testing

* `hub_api/tests/test_bundle_admin_gateway.py` -- manifest blocks, transport (MAC binds every field),
  authz/validation/pagination/rate-limit/audit over HTTP (sqlite).
* `hub_api/tests/test_bundle_admin_gateway_pg_integration.py` -- real Postgres at alembic head, the
  **real reputation hub-manifest**, real signed transport; host side is an emulator that verifies the
  MAC and answers per the bundle's own `output_schema`. Real WASM execution awaits S0.
* `alembic/tests/test_0085_bundle_community_config.py` -- chain, single head, constraints, downgrade.
