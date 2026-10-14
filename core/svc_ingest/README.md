# svc-ingest (Rust)

Waddles chat/event data-plane ingest: owns every inbound platform connection and HTTP intake
surface, normalizes raw platform payloads, mints D30 workstream identity/binding, and writes each
event exactly once onto the Valkey stream spine. No database. (Legacy Python alpha lives beside it
in `app.py`, `receivers/`, ... until the M6 cutover.)

Build context is **`core/`**, not this directory -- the crate path-depends on `core/service_auth`:

```bash
cd core && docker build -f svc_ingest/Dockerfile.rust -t svc-ingest-rust:dev .
```

## Dynamic Twitch connections

By default (flag OFF) Twitch IRC is configured by static environment: `TWITCH_IRC_NICK`,
`TWITCH_IRC_CHANNEL`, `TWITCH_IRC_OAUTH_TOKEN` -- one bot, one channel, token baked into the pod's
environment. With **`waddles.ingest-dynamic-connections` ON**, svc-ingest instead resolves *which*
channels to join and *which* OAuth tokens to use from hub-api's connection store
(`docs/PROVIDER_CONNECTIONS.md`), so an admin adds/removes a Twitch connection through the API and
ingest follows with no redeploy.

```
hub-api   GET  /api/v1/internal/provider-connections?provider=twitch&capability=chat_ingest&after=&limit=
          POST /api/v1/internal/communities/{c}/provider-connections/{id}/resolve   (decrypted token)
             ^  machine JWT: scope connections:resolve, tenant claim = system
             |
src/connections/
  client.rs      HubConnectionsClient  typed DTOs (dto.rs), cursor paging, error taxonomy
  supervisor.rs  every INGEST_CONNECTIONS_POLL_SECS: list -> group by bot account -> diff
  bot.rs         ONE IRC socket per bot account, `JOIN #a,#b,#c`, re-resolve on every
                 (re)connect, route PRIVMSG by channel -> spine under the binding's Scope
  metrics.rs     Prometheus series (histograms, gauges, counters)
```

### Enabling it

| Where | How |
|---|---|
| PostHog | flag `waddles.ingest-dynamic-connections` (default OFF) |
| No PostHog (alpha) | Docker ENV baseline `FLAG_WADDLES_INGEST_DYNAMIC_CONNECTIONS=true` (Helm: `pipeline.rustDataPlane.svcIngest.extraEnv`) |

Resolution order: PostHog snapshot value -> ENV baseline -> OFF. **License bypass is deliberately
not honored for this flag** (`license::dynamic_connections_enabled`): this service hardcodes a
`*.waddles.app` deployment domain, so bypass is active in every deployment and would turn every
flag ON -- "default OFF" must hold for a change that puts OAuth tokens on the data plane. Read once
at startup; flipping it later needs a restart.

Flag ON **supersedes** the static Twitch receiver (`try_start_twitch_irc` is not started; a WARN
says so if static env is also set). Flag ON with missing plumbing **fails startup loudly** -- it
never silently reverts to the static path or runs with no Twitch ingest. Discord is out of scope and
stays env-driven; the Twitch *outbound* relay (`crate::outbound`) still uses the static identity.

### Configuration

| Env | Default | Purpose |
|---|---|---|
| `HUB_API_URL` | `http://hub-api:8204` | hub-api REST base (plain in-cluster HTTP) |
| `HUB_API_REST_CA_FILE` | empty | PEM CA for an `https://` base -- the *only* trust anchor when set; setting it with `http://` is a startup error |
| `SERVICE_JWT_TOKEN_ENDPOINT` | empty (**required** when flag ON) | hub-api `POST /internal/service-token` |
| `SERVICE_JWT_SA_TOKEN_PATH` | `/var/run/secrets/kubernetes.io/serviceaccount/token` | projected, audience-scoped SA token (chart mounts `/var/run/secrets/waddlebot/service-jwt/service-jwt-bootstrap-token`) |
| `INGEST_CONNECTIONS_POLL_SECS` | `60` (5..3600) | reconcile interval |
| `INGEST_CONNECTIONS_PAGE_SIZE` | `500` (1..1000) | list page size |
| `INGEST_CONNECTIONS_HTTP_TIMEOUT_SECS` | `10` (1..120) | per-request timeout |
| `INGEST_DYNAMIC_SCOPE_MODE` | `binding` | `binding` (use the binding's `tenantSlug`/`communityName`; skip bindings without them) or `runner` (publish under `RUNNER_TENANT_SLUG`/`RUNNER_COMMUNITY`; single-tenant only) |
| `TWITCH_IRC_HOST` / `_PORT` / `_USE_TLS` | Twitch defaults | shared with the static path |
| `ENVELOPE_BINDING_KEYS`, `ENVELOPE_BINDING_ACTIVE_KID`, `VALKEY_URL` | -- | required (same as every receiver) |

### Behavior

| Situation | Behavior |
|---|---|
| hub-api list fails (any page) | whole listing discarded; running bots untouched; retry sooner than the poll interval |
| resolve `409`/`404` | **drop that channel, never retry** (until the binding's `updatedAt` changes or it leaves the list); other channels keep running |
| resolve `5xx`/`429`/`401`/`403`/transport | retry with jittered backoff; the bot is never dropped for these |
| every channel of a bot refused | the bot exits; not respawned while the refusals stand |
| Twitch rejects registration (auth) | re-resolve (hub-api refreshes the token) and retry with backoff -- never stop for good |
| IRC drop / reconnect | re-resolve every member (token never cached past a reconnect) |
| channel added/removed for a bot | that bot's one socket reconnects with the new `JOIN` list (see Limitations) |
| two bindings, same channel | one JOIN; each event is published once per distinct scope |
| token for a different `bot_login` than claimed | refused (`identity_mismatch`) -- never used |

Refresh is hub-api's job inside `resolve` (refresh-if-stale under a row lock); this service never
refreshes.

### Security

- The decrypted token lives only in `dto::AccessToken` (redacted `Debug`/`Display`), reaches the
  IRC `PASS` line, and nothing else. It is never logged, traced, or used as a metric label -- the
  integration tests capture **all** log output at TRACE and assert absence. Error bodies from
  hub-api are reduced to two charset-checked fields; their text is never copied anywhere.
- Logs carry numeric grant/connection/community ids, counts and a hashed bot reference
  (`bot::bot_ref`) -- no handles, channel names or tokens.
- `bot_login`/`channel_login` pass a strict `[a-z0-9_]{1,25}` allow-list before reaching an IRC
  line; scope segments pass `[A-Za-z0-9_-]{1,64}` before reaching a stream key.
- hub-api requires the machine JWT's `tenant` claim to be `system`; the issuer stamps it from
  `serviceJwt.identities.svc-ingest` (scope `connections:resolve` only). No redirects are followed
  and ambient proxies are ignored.

### Observability

Spans `hubconn.list` / `hubconn.resolve` / `hubconn.reconcile`; logs at DEBUG for every decision
point; Prometheus (`:9090`):

| Series | Type |
|---|---|
| `svc_ingest_hubconn_request_duration_seconds{op,outcome}` | histogram (list / resolve latency) |
| `svc_ingest_hubconn_reconcile_duration_seconds{result}` | histogram |
| `svc_ingest_hubconn_requests_total{op,outcome}` | counter |
| `svc_ingest_hubconn_resolve_not_usable_total{reason}` | counter (409/404 drops, bounded reasons) |
| `svc_ingest_hubconn_tokens_acquired_total{cause}` | counter (resolve = hub-api refreshed-if-stale) |
| `svc_ingest_hubconn_bindings_skipped_total{reason}` | counter |
| `svc_ingest_hubconn_bot_reconnects_total{cause}` | counter |
| `svc_ingest_hubconn_events_routed_total{result}` | counter |
| `svc_ingest_hubconn_channels_joined`, `_bots_running`, `_bindings_listed` | gauges |

### Limitations / follow-ups

- **No in-place JOIN/PART:** the pinned `penguin-connector-twitch` exposes none, so a membership
  change reconnects that bot's single socket (coalesced per reconcile pass; a brief gap for its
  other channels). Upstream `join`/`part` support would remove the reconnect. Twitch also limits
  JOINs (20 per 10 s for ordinary accounts) -- a bot bound to far more channels needs staggered
  joins.
- **Scope slugs:** hub-api's `DataPlaneBindingDTO` carries numeric `tenantId`/`communityId` only,
  but spine stream keys use the tenant *slug* and community *name*. `binding` scope mode reads
  optional `tenantSlug`/`communityName` fields (forward-compatible, not emitted by hub-api yet), so
  it skips every binding today; alpha uses `runner` mode.
- REST to hub-api is plaintext in-cluster HTTP (like the token endpoint); the binary supports an
  `https://` base + `HUB_API_REST_CA_FILE` for when hub-api's REST listener gets TLS.
- The twitch outbound relay still uses the static identity.

### Tests

`cargo test` -- unit tests live beside the code; `tests/dynamic_connections.rs` drives the real
`HubConnectionsClient` over HTTP against a stateful mock hub-api and the real bot task over TCP
against a fake Twitch IRC server (only the spine `XADD` is recorded in memory);
`tests/dynamic_flag.rs` proves flag-ON startup fails loudly; chart rendering is covered by
`k8s/helm/waddlebot/tests/test_svc_ingest_dynamic_connections_render.py`.
