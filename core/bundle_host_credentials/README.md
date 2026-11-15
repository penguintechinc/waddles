# bundle-host-credentials

Production adapters for bundle `http.send` secret refs (the traits, grant
grammar, quota guard and egress guard live in `bundle-host-http`):

- `ResolverConnections` — `ConnectionCredentials` over hub-api's connection
  resolver. Finds the invoking community's connection through a cached
  `(community, provider)` directory (30s TTL; misses rate-limited), then
  resolves per call. Credentials are never cached here; hub-api refreshes
  OAuth tokens under lock.
- `ValkeyQuotaStore` — atomic check-then-increment of minute / day /
  per-community counters in one Lua script (a refused charge consumes nothing).
- `wiring::build_credential_sources` — builds both plus the host-minted app
  token minter from env. Anything that cannot be built fails closed.

Env: `HUB_API_URL`, `SERVICE_JWT_TOKEN_ENDPOINT`, `SERVICE_JWT_SA_TOKEN_PATH`,
`DEPLOYMENT_TIER`, `BUNDLE_SECRET_QUOTAS_JSON` (optional quota overrides),
`SPOTIFY_CLIENT_ID/SECRET`, `SOUNDCLOUD_CLIENT_ID/SECRET`.

Tests: `VALKEY_URL=redis://... cargo test` (the Valkey real-path test FAILS if
`VALKEY_URL` is unset — no silent skip).
