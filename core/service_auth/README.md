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

## gRPC bootstrap (mint + JWKS)

The machine-JWT mint and the JWKS are hub-api **gRPC** RPCs (`proto/waddles/hub/internal/v1/service_auth.proto`,
`ServiceAuthService.{MintServiceToken, GetServiceJwks}`); the REST routes `POST /internal/service-token` and
`GET /internal/service-jwks.json` no longer exist. `src/grpc.rs` owns the transport:

- **TLS 1.3 only**, trusting exactly the CA in `HUB_API_GRPC_CA_FILE` (hostname verification on). An `https://`
  endpoint without a CA is an error -- never system roots, never plaintext. `http://` is accepted for loopback
  only (dev / in-process tests); any other plaintext host is refused.
- **Mint authentication**: the pod's projected, audience-bound ServiceAccount token as `authorization: Bearer`
  metadata (the credential the REST route used; hub-api validates it with TokenReview and maps it to an
  allow-listed identity), over the TLS channel. When hub-api sets `GRPC_TLS_CLIENT_CA_PATH` the channel is
  additionally mTLS. `GetServiceJwks` is public keys only and needs no credential.
- **One long-lived channel** per `HubChannel` (lazy connect, HTTP/2 keepalive while idle, transparent reconnect);
  share it between `MachineJwtClient`, `BoundMachineJwtClient` and `JwksTrustBundle` via `*::with_channel`.
- **Deadlines** are `min(request timeout, 4s)` -- under hub-api's 5s server ceiling.
- JWKS cache semantics are unchanged (single-flight refetch, negative-`kid` TTL, fail closed).

Tests use `test_support::MockHub`, a real in-process tonic `ServiceAuthService` (enable the `test-support` feature).

## BoundMachineJwtClient (community-bound bearers)

`BoundMachineJwtClient::get_token_for(community_id, connection_id)` mints a machine JWT bound by hub-api
(gRPC `ServiceAuthService.MintServiceToken`, request `{scope, community_id?, connection_id?}`) to exactly one
`(community, connection)` and caches it in a 256-entry LRU (bearer JWT only, never connection secrets).
Refreshes within `CLOCK_SKEW_SECONDS` of `exp`; concurrent callers per binding share one mint.
The unbound `MachineJwtClient` is unchanged (`connections:list`).
