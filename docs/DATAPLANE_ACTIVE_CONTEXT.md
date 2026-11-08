# Data-plane active context (per-user community routing)

A Discord guild can be paired to several communities. Slash interactions are guild-wide, so the
invoking **user's active context** picks the processing community: their explicit choice, else the
first-registered community of the source. svc-ingest (no database) asks hub-api; the client is
`core/svc_ingest/src/active_context.rs` (`ContextSink`, `/context`).

## Endpoint

`/api/v1/internal/dataplane/active-context` — machine JWT only, scope `dataplane:active-context`,
`system` tenant (any other tenant: 403). Granted in the chart to svc-ingest's SPIFFE identity alone.
Every response is `Cache-Control: no-store`.

| Verb | Input | Success |
|---|---|---|
| `GET` | `platform`, `source_id`, `user` (query) | `ActiveContextDTO` |
| `PUT` | `{platform, sourceId, user, community}` | `ActiveContextDTO` (the new context) |

`user` is the **tokenized user UUID** (`hub_users.uuid`); hub-api never sees a platform user id.
`community` is a community `public_id` UUID — names never resolve at hub-api.

```json
{
  "communityPublicId": "uuid",
  "communityId": 12,
  "communityName": "display alias",
  "explicit": false,
  "communities": [{"publicId": "uuid", "displayName": "alias", "position": 0}]
}
```

`communityId` is the **numeric** community id: the key the data plane routes on and the interaction
ownership check compares (numeric-id rule). It is required by svc-ingest — an answer without it is
`Malformed` and routing fails closed. The field set is pinned by a regression test.

| Status | Meaning |
|---|---|
| 400 | `platform`, `source_id`, `user` or `community` not in canonical form (`platform=community` is not a source) |
| 401 / 403 / 503 | missing/wrong-scope credential / non-`system` tenant / verifier unconfigured |
| 404 `SCOPE_NOT_FOUND` | no usable community is linked to the source (an inactive verified claimant never falls back to a legacy claim) |
| 409 `SCOPE_AMBIGUOUS` | the source is claimed by more than one tenant (judged before the activity filter) |
| 404 `COMMUNITY_NOT_SWITCHABLE` | PUT target is not in the source's switchable set |

## Switchable set

Resolved by the ONE shared rule `dataplane_scope.resolve_claimants`, also used by
`dataplane/resolve-scope`: an approved `community_servers` link beats a legacy
`communities.platform_server_id`; an inactive verified claimant refuses (404) with no legacy
fallback; a source claimed across tenants is refused (409) on the pre-filter claimants, never pinned
to one tenant. Ordered by first-registered link. Inactive, deleted, tenant-less and inactive-tenant
communities are never members. A stored choice that has left the set yields to the default
(`explicit=false`).

## Storage

Alembic `0082_user_active_context`: `communities.public_id` (UUID, unique, backfilled) and
`user_active_contexts (platform, source_id, user_uuid) -> community_id` (FK cascade). Rows are
deleted on Art. 17 erasure (`erasure_registry`: `delete("user_active_contexts", uuid("user_uuid"))`).

Tests: `hub_api/tests/dataplane_active_context/` (real Postgres migrated to head, real verifier).
