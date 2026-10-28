# service_auth

Verifier + caching client for hub-api-issued per-service EdDSA machine JWTs.
See the crate docs in `src/lib.rs` for the SPIFFE/Skauswatch migration notes.

## Outbound HTTP resilience

Every outbound HTTP client (`JwksTrustBundle` JWKS fetch, `MachineJwtClient`
token bootstrap) carries explicit timeouts, so a hung hub-api can never wedge
a verifier or token refresh.

| Knob (env var) | Default | Applies to |
|---|---|---|
| `SERVICE_AUTH_HTTP_CONNECT_TIMEOUT_SECS` | `3` | connect (TCP + TLS) |
| `SERVICE_AUTH_HTTP_REQUEST_TIMEOUT_SECS` | `10` | whole request/response |
| `SERVICE_AUTH_JWKS_NEGATIVE_TTL_SECS` | `30` | unknown-`kid` negative cache |

Values are whole seconds. Zero, empty, or unparsable values are ignored (the
default is used) -- a timeout can never be disabled through the environment.
`new()` constructors read the env; `JwksTrustBundle::with_config(url, JwksConfig)`
and `MachineJwtClient::with_timeouts(.., HttpTimeouts)` take explicit values.

## JWKS refetch on unknown `kid`

- **Single-flight** -- concurrent lookups of a `kid` not in the cache queue on
  one gate; the first performs the refetch and the rest re-check the caches it
  filled. N concurrent unknown-`kid` verifications cause one JWKS request.
- **Negative cache** -- a `kid` absent from the refreshed JWKS (or whose
  refetch failed) is rejected without network I/O until the negative TTL
  expires, then exactly one more refetch is allowed. Bounded to 1024 entries
  (expired purged first, then earliest-expiring evicted).
- **Fail loud** -- an unknown `kid` still rejects the token. A legitimately
  rotated-in `kid` is picked up on the next refetch after the TTL (so a key
  rotation can take up to the negative TTL to be honoured by a verifier that
  saw the new `kid` first; lower the TTL if that window matters).

## BoundMachineJwtClient (community-bound bearers)

`BoundMachineJwtClient::get_token_for(community_id, connection_id)` mints a machine JWT bound by hub-api
(`POST /internal/service-token`, body `{scope, community_id, connection_id?}`) to exactly one
`(community, connection)` and caches it in a 256-entry LRU (bearer JWT only, never connection secrets).
Refreshes within `CLOCK_SKEW_SECONDS` of `exp`; concurrent callers per binding share one mint.
The unbound `MachineJwtClient` is unchanged (`connections:list`).
