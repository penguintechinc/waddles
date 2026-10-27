# Data-plane scope resolver (hub-api internal)

Slice A of the numeric-id migration — design: `docs/superpowers/specs/2026-10-14-data-plane-numeric-ids-migration.md`.
svc-ingest has no database; it asks hub-api which community a platform source belongs to so the data
plane keys on numeric `(tenant_id, community_id)` (names/slugs are display-only, never keys).

## Endpoints (machine JWT only)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/internal/dataplane/resolve-scope?platform=<p>&source_id=<id>` | one lookup |
| POST | `/api/v1/internal/dataplane/resolve-scope/batch` | `{"items":[{"platform","sourceId"}]}`, 1..100, per-item `status` (`resolved`/`not_found`/`ambiguous`/`invalid`), order kept |

200 body: `{success, tenantId, communityId, tenantSlug, communityName}` — `tenantId`/`communityId` are the keys.
No secret exists in the response.

| Status | Meaning | Caller action |
|---|---|---|
| 200 | resolved | cache (`Cache-Control: private, max-age=30`) |
| 400 | platform/source not in canonical form (`^[a-z][a-z0-9_]{0,49}$`, `^[A-Za-z0-9_.:-]{1,255}$`) | bug in caller |
| 401 | missing/invalid token, or token lacks the scope | re-bootstrap |
| 403 | valid token but not the operator-plane `system` tenant | config fault |
| 404 | no active community linked to the source | **fail closed to tenant-wide sentinel `community_id 0`** — never guess |
| 409 | source claimed by several communities, none the single primary | treat as unresolved; fix the data |
| 503 | `SERVICE_JWT_VERIFIER` not configured | deployment fault |

Every non-200 is `Cache-Control: no-store`.

## Auth

Scope `dataplane:resolve-scope` (deliberately **not** `connections:resolve`: this one returns ids, never a
token). Chart grant: `k8s/helm/waddlebot/values.yaml` `serviceJwt.identities.svc-ingest.allowedScopes`;
rendered only while `pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled` is true (default false;
alpha on). No other identity holds it. Guard: `hub_api/services/machine_auth.py` (shared with the
provider-connections internal routes).

## Mapping (what is looked up)

1. `community_servers` rows with `status='approved'` for `(platform, platform_server_id)` — the real
   N-servers-per-community link; the **only** place a Twitch channel (and any additional guild) is recorded.
2. `communities.platform` / `platform_server_id` — a community's legacy primary server (Discord guild id);
   treated as that community's primary link.

Twitch source ids are case-folded (logins are case-insensitive); other platforms match exactly. Inactive
communities, communities with no tenant, and a dangling tenant id never resolve. If several communities
match, the single `is_primary` one wins; otherwise 409. Code: `hub_api/services/dataplane_scope.py`,
`hub_api/blueprints/v1/dataplane_scope.py`.
