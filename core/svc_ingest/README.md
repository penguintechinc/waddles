# svc-ingest (Rust data-plane ingest)

Owns inbound platform connections and HTTP intake; normalizes events and appends them once to the
Valkey stream spine. No database.

## Generic transport credentials

`WsTransport` and `PollTransport` use `TransportSecrets::new(resolver, community_id)`
with a real `connection_resolver::ConnectionResolver`. The host binds the community
and the handle-to-connection map; a guest can name a registered handle but cannot
select a different community. Each handshake or poll resolves its credential anew,
so rotation is visible on the next use. OAuth access tokens, bearer credentials and
API-key values are supported; empty values and multi-field credentials fail closed.

A credential is released only to an approved destination: the target URL host must be in
the host-owned per-provider allowlist (`connector::secrets::approved_hosts`, keyed by the
RESOLVED connection's provider, exact hosts, unknown provider = none), and a static row's
`endpoint.url` host must also match. An optional expected provider
(`with_expected_provider`) must equal the resolved one. API-key rows use the stored header
name. Follow-up: a mandatory approved endpoint on OAuth records would tighten this floor
from per-provider to per-connection (hub-api + connection_resolver change).

Webhook verification uses `ConnectionResolverSecrets` instead. Its
`WebhookSecretSource` trait is separate from `TransportSecretSource`, and it rejects
OAuth access tokens as signing keys. Resolved secrets stay in host-owned zeroizing
buffers. HTTP and WebSocket libraries may retain their own non-zeroizing send-buffer
copies, as documented in the transport modules.

These transport primitives do not activate generic runtime intake. Production
activation also requires the authenticated executor link and a trusted registry of
connector bindings; the generic webhook route continues to return 503 until those
dependencies are wired. The legacy EventSub environment-secret implementation is
still present and must be retired when that runtime path is ready.

## Per-community Twitch sessions (`INGEST_DYNAMIC_CONNECTIONS`)

With the flag ON, svc-ingest runs one Twitch IRC session **per connected bot/channel binding**
instead of a single static `TWITCH_IRC_*` identity.

| Env | Default | Meaning |
|---|---|---|
| `INGEST_DYNAMIC_CONNECTIONS` | `false` | Turns on per-community sessions (chart: `pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled`, alpha only today) |
| `TWITCH_RECONCILE_INTERVAL_SECS` | `60` (alpha: `30`) | Reconcile cadence, bounded 10..3600; a binding change takes effect within one interval (+10% jitter) |
| `TWITCH_RESOLVE_TIMEOUT_SECS` | `10` | Per-call bound on hub-api list/resolve |

**Static `TWITCH_IRC_NICK` / `TWITCH_IRC_CHANNEL` with the flag ON is a FATAL startup error** (never a
silent override) -- the chart never renders them for alpha. Required alongside the flag:
`SERVICE_JWT_TOKEN_ENDPOINT`, a valid `ENVELOPE_BINDING_KEYS` + `ENVELOPE_BINDING_ACTIVE_KID`, a
reachable Valkey (leases + spine), and https hub-api outside alpha. The machine identity needs the
`connections:resolve` + `connections:list` scopes (chart grants them to svc-ingest only).

### Runbook

1. **Connect:** in the webui connect the Twitch bot for a community (OAuth) and name the channel -- this
   creates a provider connection in hub-api.
2. Within <= one reconcile interval a session starts: svc-ingest lists bindings, takes a per-binding
   Valkey lease (one replica owns each session), resolves a fresh token and JOINs the channel.
   Check `svc_ingest_twitch_sessions_live` (gauge) and `svc_ingest_twitch_session_events_total{event="started"}`.
3. **Disconnect/delete** the connection: the next reconcile stops the session and releases its lease
   (`event="stopped"`). A hub outage keeps running sockets open (no start/stop decisions on a failed list).
4. Token refresh/rotation is picked up on the session's next (re)connect; a revoked token ends the session
   as non-retryable until the binding is re-authorised.

Logs and metric labels never carry tokens, channel or bot logins -- only numeric community/connection ids
and counts.

### Tests

| Command | Covers |
|---|---|
| `cargo test --locked` (needs `VALKEY_URL`, a real Valkey; unset = failure, never a skip) | unit + real-socket lifecycle/reconcile tests |
| `make test-svc-ingest-twitch-live` | live e2e: real hub-api + Postgres + Valkey + IRC server; binding created/deleted through the admin API starts/stops a session; prints and gates on counts (docker + helm + cargo; `HUB_PYTHON` = python with `hub_api/requirements-dev.txt` + `libs/flask_core/requirements.txt`) |
| `helm` render: `k8s/helm/waddlebot/tests/test_svc_ingest_twitch_sessions_render.py` | alpha renders the flag + timing envs and no static pair; beta/prod unchanged; egress to hub-api |

## Outbound relay host-pin (`src/outbound_pin.rs`)

The outbound drain never trusts a destination it was handed. `svc_action` pins every
channel-targeted relay op (Twitch `chat.send`; Discord `chat.send`/`chat.delete`) to the
triggering event's own channel and stamps it as `origin_channel`; the drain then refuses any
entry whose `channel` is not exactly that stamp, is not a well-formed id (Twitch login
`^[a-z0-9_]{1,25}$`, non-numeric, one optional `#` and nothing else normalized; Discord snowflake = non-zero `u64`), or has no stamp at all (pre-upgrade
entries are refused loudly, never guessed at).

| Refusal reason | Meaning |
|---|---|
| `channel_not_pinned` | `channel` differs from `origin_channel` |
| `no_origin_pin` | entry has no `origin_channel` |
| `invalid_channel` | malformed id (incl. IRC multi-target `#a,#b`) |
| `unsupported_platform` | no pin policy -> fail closed |
| `no_pin_sig` / `bad_pin_sig` | missing / invalid `pin_sig` integrity stamp (see Provenance) |
| `pin_key_unconfigured` | no verification keys -> every op refused |
| `pin_expired` / `pin_exp_invalid` | signed `exp_ms` in the past / missing or > 5 min ahead |

Refusals surface as `SenderError::Rejected`, bump `svc_ingest_outbound_pin_refused_total{platform,op,reason}`
and log a PII-free WARN (platform/op/reason only -- never a channel, id or message text).
Bundles only ever supply message content. Tests: `cargo test --lib outbound` (regression tests named
`*unpinned*`/`*regression_p0*`). Follow-up (not in this change): per-community credential resolution via
`connection_resolver` replacing the single process-wide Twitch bot identity.

### Provenance: who can write the outbound queue (`src/pin_sig.rs`)

**The Valkey ACL does not restrict the writers.** Every app client authenticates as the shared
password-only `default` user with `~*` and `+@write` (`k8s/helm/waddlebot/templates/infrastructure/valkey-durable.yaml`,
`--user default on ... '~*' ... +@write`); the per-service users in `config/redis/users.acl` are not loaded by this durable instance
(it takes `--user` flags inline). So any pod holding the durable Valkey password (svc_process, hub-api, seeder, ...) can `LPUSH`
`waddles:transport:irc:{twitch,discord}:outbound`; `origin_channel == channel` alone proves nothing. WASM bundles cannot write
the list directly -- only through the `relay` host capability in `svc_action`
(`core/svc_action/src/capabilities.rs`, `outbound_relay_queue_key`).

Trust boundary enforced in code instead: `svc_action` signs `platform|op|channel|origin_channel|message_id|text|user_id|exp_ms`
(HMAC-SHA256, domain label `waddles.outbound-pin.v1`, length-prefixed) for EVERY op (`chat.send`, `chat.delete`, `dm.send`)
and puts `pin_sig` = `kid:hexmac` plus `exp_ms` on the envelope; the drain verifies in constant time **before** the pin
check and refuses `no_pin_sig` / `bad_pin_sig` / `pin_key_unconfigured` (no usable keys => every op refused, ERROR at startup)
and, after the MAC passes, `pin_expired` (exp in the past) / `pin_exp_invalid` (missing, or more than 5 min ahead).
Chat entries live 60s (`PIN_TTL_MS`); Discord entries use the ack timeout.

- Key: a DEDICATED secret `OUTBOUND_PIN_KEYS` (`kid:hexkey[,...]`, >=16 bytes each) + `OUTBOUND_PIN_ACTIVE_KID`, mounted by
  `secretKeyRef` ONLY into svc-action-rust (signer) and svc-ingest-rust (verifier) -- not the shared envFrom Secret, not the
  binding-key ring (`templates/outbound-pin-secret.yaml`; `outboundPin.existingSecret` for ExternalSecret/SealedSecret,
  alpha/local auto-generate, other tiers fail closed). Rotation: add the new kid to the ring, flip `outboundPin.activeKid`,
  later drop the old kid (the drain accepts any kid it holds).
- Symmetric, so a holder of the key (svc-action, svc-ingest) can still forge; Ed25519 (signer-only private key) is the follow-up.
  The Valkey ACL is defense in depth only. Replay inside the TTL to the SAME channel is possible (no seen-op_id set yet).
- `core/svc_ingest/src/pin_sig.rs` and `core/svc_action/src/pin_sig.rs` are byte-identical (test `pin_sig_copies_do_not_drift`).

### Rollout order (required) and dropped entries

Deploy **svc_action first, then svc_ingest**, with the queue drained between if possible. The new drain hard-refuses
anything not signed by the new svc_action: entries queued by a pre-P6a svc_action have no `origin_channel` and are dropped
with a WARN and counted `svc_ingest_outbound_pin_refused_total{reason="no_origin_pin"}` (stamped-but-unsigned entries from an
intermediate build: `no_pin_sig`; stamped but without `exp_ms`: `pin_exp_invalid`). A Discord entry whose stamp fails verification is dropped and counted but NEVER acked (an unauthenticated
writer must not settle another producer's `op_id`); the real producer times out. `op_id` is MAC-bound. On alpha, accept the loss of in-flight chat ops. (The metric label is `no_origin_pin`, not
`missing_origin`.) A refused entry is already popped (`BRPOP`) so it is never retried; there is no poison loop.

## Connector heartbeat (P6a T5)

The connector driver schedules `receiver.on-heartbeat-due` per connection when a transport declares
`Transport::heartbeat_interval()` and the driver is opted in (`with_heartbeat_enabled`, PostHog
`ingest.connector-heartbeat`, default OFF). See `docs/connector-host-capability.md` "Heartbeat scheduling".
Tests: `cargo test --locked --test connector_heartbeat`.

## Connector egress (SSRF guard)

Poll/WebSocket connector transports resolve and pin their endpoint through `connector::egress_guard`, a thin
adapter over `bundle_host_http::guarded` -- the one shared implementation (denylist incl. IPv4-mapped/NAT64 v6,
resolve-then-connect pinning, https/wss only, no userinfo, class-only errors, `guarded_egress_total{outcome}`
metric + tracing span). The former mirrored denylist was removed; do not re-add range logic here. Real-path
redirect/rebind tests live in `core/bundle_host_http/tests/guarded_real_path.rs` (`cargo test --locked`).
Follow-up: `poll.rs::pinned_client` should call `egress_guard::pinned_client` once PR #982 lands (kept out of this
change to avoid conflicting with it); the Discord REST/outbound client (`outbound.rs`, PR #987 area) is not yet routed
through the guard.
