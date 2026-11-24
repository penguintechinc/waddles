# Community read-route authorization

Every session carries the viewer bundle's universal `*:read`, so a route whose only scope is
`X:read` is reachable by any signed-in user in the tenant. A tenant check
(`community_in_tenant`) proves the community belongs to the caller's tenant, not that the caller
belongs to the community.

## Rules

- A community-scoped route needs a guard from `hub_api/services/route_guards.py` on top of its
  scope: `community_admin_guard`, `community_member_guard`, an inline owner/own-data check
  stamped with `mark_guarded(...)`, `platform_analytics_guard` (system admin or analytics
  consumer; a literal `analytics:read` is not enough), `tenant_admin_guard`, or
  `machine_or_community_admin_guard` (service identity or community admin).
- Standing is resolved from the database (`community_authz.resolve_caller_standing_sync`);
  the token's own scopes never count. Hard-banned and soft-banned members are not members.
- **A foreign-tenant community id returns 404, not 403**, so a caller cannot enumerate which
  community ids exist in other tenants. 403 is reserved for "the community is yours but you
  lack standing".
- Machine endpoints identify the caller by a literal scope on the service token
  (`distribution:read`), never by wildcard coverage. Never accept enumerable numeric ids from
  human callers.

## CI

`hub_api/tests/test_read_route_guards.py` walks the Quart `url_map`: every route with a
`community_id` argument and only `:read` scopes needs a guard stamp (or a justified allowlist
entry), and scope-less community routes need a recognised inline check or an allowlist
entry. Stale allowlist entries fail the test. `test_read_route_authz.py` and
`test_read_route_inline_guards.py` prove the guards actually enforce.
