# `IdentityService.ResolvePlatformUser` (UUID -> raw platform user id)

The one detokenization point of the PII boundary: maps tokenized user UUIDs back to the RAW platform
user id (for example a Discord snowflake) **for one community**. Consumer: svc-ingest's outbound
executor (Discord `dm.send` / `role.add` / `role.remove`), which holds the answer transiently for one
operation and never logs or stores it. Defined in `proto/waddles/hub/internal/v1/identity.proto`,
implemented in `hub_api/services/identity_resolution_service.py::resolve_platform_users` and
`hub_api/grpc_internal/servicers.py`.

```
rpc ResolvePlatformUser(ResolvePlatformUserRequest) returns (ResolvePlatformUserResponse);
request  { string tenant_id = 1; int64 community_id = 2; string platform = 3; repeated string uuids = 4; }
response { repeated ResolvedPlatformUser users = 1; }          // ResolvedPlatformUser { uuid = 1; platform_user_id = 2; }
```

| Property | Behaviour |
|---|---|
| Scope | `identity:platform-user:resolve` (own scope; chart grants it to `svc-ingest` only) |
| Caller | machine JWT (EdDSA, `AuthInterceptor`); a token with no `tenant` claim -> `PERMISSION_DENIED`; wrong/missing scope -> `UNAUTHENTICATED` |
| Tenant | authorized against the token claim exactly like the other RPCs (`authorize_tenant`): a tenant-bound token is confined to its tenant; `system` (svc-ingest) must name one. The community must belong to that tenant |
| Ids | `community_id` is the numeric `communities.id` (> 0); `platform` explicit, `^[a-z0-9_-]{1,50}$`; `uuids` 1..25, strict canonical UUIDs (a raw snowflake is `INVALID_ARGUMENT`) |
| Membership | every uuid must be a CURRENT member of that community on that platform (member row's own platform account, or a hub user's verified identity link); deactivated hub users never resolve |
| Misses | any unknown uuid / other community or tenant / non-member / platform mismatch / ambiguous identity -> ONE constant `NOT_FOUND` ("platform user not found") for the whole call -- no oracle, no partial answer |
| Rate limit | per-caller bucket for this method (`GRPC_PLATFORM_USER_RPS`=20, `GRPC_PLATFORM_USER_BURST`=40), separate from the default |
| Audit | one structured record per call (`action=identity_resolve_platform_user`: caller `sub`, numeric community id, platform, count, outcome) -- never the raw ids, uuids or tenant reference as sent. Plus the `hub_api.identity.platform_user_resolutions` counter |
| Caching | none; nothing persisted |

Tests: `hub_api/tests/test_identity_platform_user_pg_integration.py` (real Postgres + the production
interceptor chain).
