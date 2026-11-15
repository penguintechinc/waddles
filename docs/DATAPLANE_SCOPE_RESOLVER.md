# Data-plane scope resolver (hub-api internal)

Slice A of the numeric-id migration — design: `docs/superpowers/specs/2026-10-14-data-plane-numeric-ids-migration.md`.
svc-ingest has no database; it asks hub-api which community a platform source belongs to so the data
plane keys on numeric `(tenant_id, community_id)` (names/slugs are display-only, never keys).

## Contract (internal gRPC, machine JWT only)

`proto/waddles/hub/internal/v1/dataplane_scope.proto` -- `DataplaneScopeService` on hub-api's
internal TLS gRPC listener (`hub_api/grpc_internal/dataplane_scope_servicer.py`); the svc-ingest
client is `hub_client::resolve_scope` (one long-lived channel, machine JWT in `authorization`
metadata, breaker, deadline, tracing span). This replaced the REST hop
`GET /api/v1/internal/dataplane/resolve-scope` (+ `/batch`): intra-cluster traffic is gRPC only.

| RPC | Notes |
|---|---|
| `ResolveScope(platform, source_id)` | one lookup |
| `ResolveScopeBatch(items[])` | 1..100 items, per-item `status` (`RESOLVED`/`NOT_FOUND`/`AMBIGUOUS`/`INVALID`), order kept |

Answer: `{tenant_id, community_id, tenant_slug, community_name}` -- `tenant_id`/`community_id` are the
keys. No secret exists in the response.

| gRPC status | Meaning | Caller action |
|---|---|---|
| `OK` | resolved | cache (svc-ingest LRU, 30s) |
| `INVALID_ARGUMENT` | platform/source not in canonical form (`^[a-z][a-z0-9_]{0,49}$`, `^[A-Za-z0-9_.:-]{1,255}$`), or batch outside 1..100 | bug in caller |
| `UNAUTHENTICATED` | missing/invalid token, or token lacks the scope | re-bootstrap |
| `PERMISSION_DENIED` | valid token but not the operator-plane `system` tenant | config fault |
| `NOT_FOUND` | no active community linked to the source | **fail closed to tenant-wide sentinel `community_id 0`** -- never guess |
| `FAILED_PRECONDITION` | source claimed by several communities, none the single primary | treat as unresolved; fix the data |
| `UNAVAILABLE` | scope store not configured | deployment fault |

## Auth

Scope `dataplane:resolve-scope` (deliberately **not** `connections:resolve`: this one returns ids, never a
token). Chart grant: `k8s/helm/waddlebot/values.yaml` `serviceJwt.identities.svc-ingest.allowedScopes`;
rendered only while `pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled` is true (default false;
alpha on). No other identity holds it. Guard: the gRPC interceptor chain (`hub_api/grpc_internal/
interceptors.py`: EdDSA machine-JWT + per-method scope) plus the servicer's `system`-tenant check.

## Mapping (what is looked up)

1. `community_servers` rows with `status='approved'` for `(platform, platform_server_id)` — the real
   N-servers-per-community link; the **only** place a Twitch channel (and any additional guild) is recorded.
2. `communities.platform` / `platform_server_id` — a community's legacy primary server (Discord guild id);
   treated as that community's primary link.

Twitch source ids are case-folded (logins are case-insensitive); other platforms match exactly. Inactive
communities, communities with no tenant, and a dangling tenant id never resolve. If several communities
match, the single `is_primary` one wins; otherwise 409. Code: `hub_api/services/dataplane_scope.py`,
`hub_api/blueprints/v1/dataplane_scope.py`.
