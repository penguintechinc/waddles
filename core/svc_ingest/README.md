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
| `no_op_id` | authentic entry with no MAC-bound `op_id` nonce (rollout signal: pre-nonce `svc_action`) |
| `pin_replayed` | single-use check: this exact signed entry was already executed (counted on `svc_ingest_outbound_pin_replayed_total{platform,op}`, not the refusal counter) |

Refusals surface as `SenderError::Rejected`, bump `svc_ingest_outbound_pin_refused_total{platform,op,reason}`
and log a PII-free WARN (platform/op/reason only -- never a channel, id or message text).
Bundles only ever supply message content. Tests: `cargo test --lib outbound` (regression tests named
`*unpinned*`/`*regression_p0*`).

## Per-community Twitch outbound credentials (`src/outbound_creds.rs`)

Design: `docs/superpowers/specs/2026-11-02-per-tenant-bot-credentials.md`. Replaces the single process-wide
Twitch bot identity on the outbound drain.

- `svc_action` stamps the invoke scope's NUMERIC `community_id` on each queued `chat.send` (never bundle input).
- On **every send** the drain resolves that community's own twitch connection via `connection_resolver`
  (binding list -> the community's binding that owns the pinned channel -> `resolve`), asserts the answer is the
  same community/connection/provider/OAuth kind, and uses the token once. The secret is **never cached**
  (`SecretString`, zeroizing, redacted `Debug`); keys are numeric ids, never slug/name.
- Failure is loud and typed, with NO fallback credential (`CredError` -> `SenderError::Rejected`/`Failed`,
  stable reason in the ack): `no_community_scope`, `no_connection`, `channel_not_owned` (incl. a channel
  claimed by another community), `connection_not_usable`, `credential_unavailable` (retryable, `Failed`).
- Telemetry: `svc_ingest_outbound_credential_resolve_total{platform,outcome}`, PII-free logs (community id +
  reason only; never token/login/channel/text); resolver hub-api calls keep their own spans/metrics.
- **Switch / migration:** governed by the existing `waddles.ingest-dynamic-connections` flag (env baseline
  `INGEST_DYNAMIC_CONNECTIONS`, default OFF). ON = per-community (misconfig is a fatal startup error; a set
  `TWITCH_IRC_OAUTH_TOKEN` is ignored with a WARN). OFF = the LEGACY single-identity path
  (`StaticCredentialSource`, needs `TWITCH_IRC_NICK`+`TWITCH_IRC_OAUTH_TOKEN`, else the drain does not start and
  logs why) -- single-tenant dev only; removed when the flag is retired. The two are never mixed per call.
- Known follow-up: each send pages the (non-secret) binding list; a community-filtered hub-api endpoint or a
  short-TTL cache of non-secret binding metadata would cut that cost. Secrets stay uncached regardless.
- Tests: `cargo test -p svc-ingest --lib outbound` (regressions tagged `p6a-t6b`).

### Provenance: who can write the outbound queue (`src/pin_sig.rs`)

**The Valkey ACL does not restrict the writers.** Every app client authenticates as the shared
password-only `default` user with `~*` and `+@write` (`k8s/helm/waddlebot/templates/infrastructure/valkey-durable.yaml`,
`--user default on ... '~*' ... +@write`); the per-service users in `config/redis/users.acl` are not loaded by this durable instance
(it takes `--user` flags inline). So any pod holding the durable Valkey password (svc_process, hub-api, seeder, ...) can `LPUSH`
`waddles:transport:irc:{twitch,discord}:outbound`; `origin_channel == channel` alone proves nothing. WASM bundles cannot write
the list directly -- only through the `relay` host capability in `svc_action`
(`core/svc_action/src/capabilities.rs`, `outbound_relay_queue_key`).

Trust boundary enforced in code instead: `svc_action` signs `platform|op|channel|origin_channel|message_id|text|user_id|op_id|exp_ms|community_id|tenant_id`
(HMAC-SHA256, domain label `waddles.outbound-pin.v2`, length-prefixed; numeric ids, `0` = absent) for EVERY op (`chat.send`, `chat.delete`, `dm.send`)
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

### `community_id` / `tenant_id` are MAC-bound (pin_sig v2, P6a T6d-1)

The numeric `community_id` (and `tenant_id` when present) the drain resolves the credential for is part of the MAC, so a queue
writer replaying a validly signed entry can neither change it to another community, add one, nor strip it (each => `bad_pin_sig`).
An entry signed under the v1 label (no community/tenant in the MAC) is authentic but unscoped: it is refused with its own
counted reason `svc_ingest_outbound_pin_refused_total{reason="pin_sig_legacy"}` (`PinSigError::LegacyUnscoped` ->
`PinError::LegacyStamp`), never accepted. **Rollout: svc_action first (signs v2), then svc_ingest (verifies v2).** Between the two
deploys the old verifier rejects v2 entries as `bad_pin_sig`; after, any v1 entry still queued is `pin_sig_legacy`. In-flight
entries are lost, not replayed (they are already popped; alpha is disposable and the producer's ack timeout reports it).
Golden vectors: v2 in `pin_sig.rs::golden_vector_is_stable_across_services`, v1 in `a_v1_signature_is_classified_legacy_never_accepted`.

### Rollout order (required) and dropped entries

Deploy **svc_action first, then svc_ingest**, with the queue drained between if possible. (P6a producer-completeness adds two
more distinct, counted refusals: an authentic entry with no `op_id` is `no_op_id`; a `dm.send`/`role.*` entry with no MAC-bound
`tenant_id` is refused by the executor duty as `no_tenant_scope` (`svc_ingest_discord_duty_refused_total`). The new svc_action stamps
both, so they only appear for entries signed by the OLD svc_action. Entries in flight across the svc_ingest deploy are lost -- alpha
is disposable; Discord producers time out `relay_unconfirmed`.) The new drain hard-refuses
anything not signed by the new svc_action: entries queued by a pre-P6a svc_action have no `origin_channel` and are dropped
with a WARN and counted `svc_ingest_outbound_pin_refused_total{reason="no_origin_pin"}` (stamped-but-unsigned entries from an
intermediate build: `no_pin_sig`; stamped but without `exp_ms`: `pin_exp_invalid`). A Discord entry whose stamp fails verification is dropped and counted but NEVER acked (an unauthenticated
writer must not settle another producer's `op_id`); the real producer times out. `op_id` is MAC-bound. On alpha, accept the loss of in-flight chat ops. (The metric label is `no_origin_pin`, not
`missing_origin`.) A refused entry is already popped (`BRPOP`) so it is never retried; there is no poison loop.

### Single-use execution (replay protection, P6a producer-completeness)

The Valkey ACL lets any app client write the queue and a stamp is valid until `exp_ms`, so a captured, validly signed entry could be
re-pushed inside its TTL. After `verify_stamp` succeeds and BEFORE any send/ack, both drains (Twitch and Discord) atomically claim the
entry: `SET outbound:seen:<platform>:<sha256(pin_sig)> 1 NX PX <exp_ms - now>` on the existing Valkey (a dedicated non-blocking
connection, `ValkeyReplayGuard`). `pin_sig` is a MAC over the per-op `op_id`, so the key is per op (two sends of identical text are two
keys) and hash-only (no channel/user id/text/op_id in the key or logs). Its TTL is the time left on the signed expiry; after that the
entry is `pin_expired` anyway.

| Outcome | Behaviour |
|---|---|
| first sight | executed |
| duplicate | dropped, **not executed**, **no ack** (the original execution already answered the producer; a second ack would overwrite its result); `svc_ingest_outbound_pin_replayed_total{platform,op}` + PII-free WARN |
| Valkey unreachable | **fail CLOSED**: NOT executed, typed `SenderError::Failed` (Discord producer gets ack code `failed`, Twitch is logged), `svc_ingest_outbound_replay_check_failed_total{platform,op}` + ERROR. An outage never opens a replay window. Like every post-pop failure the entry is dropped (best-effort relay, never re-queued); a waiting Discord producer sees `failed` and may resend under a fresh `op_id` |

Entries without an `op_id` are refused `no_op_id` (the verifier needs a per-op identity). Real-Valkey proof (atomic claim, PX ttl,
32-way race => exactly 1 winner, and a killed connection => fail-closed typed `Failed`): `real_valkey_*` lib tests, run in CI by the
svc-ingest job (`VALKEY_URL`, same as the other real-Valkey suites; unset FAILS, never skips; no `#[ignore]`). Locally:
`VALKEY_URL=redis://127.0.0.1:<port> cargo test --lib real_valkey` against a throwaway `valkey/valkey:8-alpine` container.

## Connector heartbeat (P6a T5)

The connector driver schedules `receiver.on-heartbeat-due` per connection when a transport declares
`Transport::heartbeat_interval()` and the driver is opted in (`with_heartbeat_enabled`, PostHog
`ingest.connector-heartbeat`, default OFF). See `docs/connector-host-capability.md` "Heartbeat scheduling".
Tests: `cargo test --locked --test connector_heartbeat`.

## Connector session lifecycle (P6b)

Host-owned declarative rules (`WsConfig.session`, `PollConfig.lifecycle`; `connector/lifecycle.rs`) give platform
overlays what a stateless guest cannot: captured session state fed back through `session-token`/handshake slots, a
frame-announced heartbeat interval, reconnect/park directives and WS close-code classification, poll interval hints,
403-reason back-off vs park, and a percent-encoded API-key query slot. No WIT change. Config examples (Discord,
YouTube), limits, metrics: `docs/connector-host-capability.md` "Session lifecycle". Tests:
`cargo test --locked --lib connector::lifecycle`, `--test connector_session_lifecycle`, `--test connector_heartbeat`.

## Connector IDENTIFY budget (`src/connector/identify_budget.rs`, `handshake_gate.rs`)

Fleet-wide Discord IDENTIFY budget as a reusable host module (port of the legacy `ingest::discord_identify_budget`, which
stays until T4). The WS driver awaits a `HandshakeGate` after `on-connect` and before the handshake is sent;
`DiscordIdentifyGate` skips the budget for a RESUME (session token carries `session_id`) and otherwise takes one slot from
Valkey (atomic Lua: daily `session_start_limit` counter + `shard % max_concurrency` 5s bucket windows, keys hold only a
SHA-256 of the bot token). **Fails CLOSED**: a Valkey outage is a retryable `GateError`, never an unchecked IDENTIFY (the
legacy path failed open); exhaustion is a typed `retry_after` the driver honours instead of hammering. `session_start_limit`
comes from `GET /gateway/bot` through the guarded client (or an explicit host-config fallback, logged and counted).
Metric: `connector_identify_budget_total{outcome}`. Details: `docs/connector-host-capability.md` "IDENTIFY budget".

Tests: `cargo test --test connector_identify_budget` needs Docker (real Valkey via `core/test_support`, one container per
test); `--test connector_handshake_gate` and the module unit tests need nothing.

## Connector egress (SSRF guard)

Poll/WebSocket connector transports resolve and pin their endpoint through `connector::egress_guard`, a thin
adapter over `bundle_host_http::guarded` -- the one shared implementation (denylist incl. IPv4-mapped/NAT64 v6,
resolve-then-connect pinning, https/wss only, no userinfo, class-only errors, `guarded_egress_total{outcome}`
metric + tracing span). The former mirrored denylist was removed; do not re-add range logic here. Real-path
redirect/rebind tests live in `core/bundle_host_http/tests/guarded_real_path.rs` (`cargo test --locked`).
Follow-up: `poll.rs::pinned_client` should call `egress_guard::pinned_client` once PR #982 lands (kept out of this
change to avoid conflicting with it); the Discord REST/outbound client (`outbound.rs`, PR #987 area) is not yet routed
through the guard.

## Generic outbound executor (P6a T6d-1, `src/connector/sender_exec/`)

The host half of `sender.build-request`. A platform overlay (`bundles/rust/{twitch,discord,youtube}`, `world connector-tools`)
returns an `http-request-tpl` made of SLOTS; this module verifies it, fills the slots from HOST state, and sends it through
`bundle_host_http::guarded::GuardedClient` only.

```
queue entry --(drain: pin_sig HMAC + expiry, FIRST gate)--> ActionExecutor::execute_action --(pin re-check, enforce_pin)-->
  FlagGate waddles.connector-tools (OFF => FlagOff, nothing runs) -> digest pin -> TemplateBuilder (pinned component)
  -> verify::verify (typed TemplateError) -> SlotSource (per community, per call, uncached) -> subst::render
  -> GuardedClient (SSRF guard, per-hop revalidation, 64 KiB / 10 s bounds, redirects NOT followed) -> 2xx | typed ExecError
```

| Module | Role |
|---|---|
| `policy.rs` | per-platform/kind contract: hosts, slots + allowed positions, header allowlist, path/query shape, body bound |
| `verify.rs` | `verify()` -> `VerifiedTemplate` (the only type `subst` accepts) |
| `subst.rs` | percent-encode URL positions, JSON-escape body (rebuilt via `serde_json`), header-safe ASCII; zeroizing `SlotValue` |
| `mod.rs` | `SenderExecutor`, `FlagGate`/`LicenseFlagGate`, `TemplateBuilder`, `SlotSource`, `DutyHook`, `ExecError`, metrics |
| `twitch_slots.rs` | `TwitchSlots`: `SlotSource` over `outbound_creds::TwitchCredentialSource` + one guarded `GET /helix/users` for numeric ids |
| `discord_slots.rs` | `DiscordSlots`: `SlotSource` over `outbound_creds_discord::DiscordCredentialSource` (`discord-bot` token per community, per call) + one guarded `GET /channels/{pinned}` proving the channel is in the binding's guild |
| `youtube_slots.rs` | `YoutubeSlots`: `SlotSource` over `outbound_creds_youtube::YoutubeCredentialSource`: `youtube.oauth` (per-community OAuth token) + `youtube.live_chat_id` (the connection's pinned chat), per call, uncached; refuses every other handle |
| `discord_duties.rs` | `DiscordDuties`: the Discord `DutyHook` (T6d-2) for `dm.send`, `role.add`, `role.remove` |
| `../platform_users.rs` | `HubPlatformUserResolver`: production `PlatformUserResolver` over `hub_client` `IdentityService.ResolvePlatformUser` |

**Supported kinds:** `chat.send`, `chat.delete` (Twitch, Discord), `chat.send` (YouTube); Discord `dm.send`, `role.add`, `role.remove` once
`DiscordDuties` is registered (`SenderExecutor::with_duties`). With no hook (`NoDuties`) a duty kind returns `ExecError::HostDutyMissing`
(`SenderError::Unsupported`).

**Slot table** (token = exact text between the braces; handle = the overlay's `secret-refs` handle):

| Platform | Slot | Handle | Source | Positions | Shape |
|---|---|---|---|---|---|
| twitch | `TWITCH_TOKEN` | `token` | resolver (per-community OAuth) | `Authorization: Bearer <slot>` | secret |
| twitch | `TWITCH_CLIENT_ID` | `client_id` | resolver (app client id) | `Client-Id` | id |
| twitch | `TWITCH_BROADCASTER_ID` | `broadcaster_id` | resolver (Helix id of the stamp-pinned channel) | body `/broadcaster_id`, query `broadcaster_id` | id |
| twitch | `TWITCH_SENDER_ID` | `sender_id` | resolver (Helix id of the connection's bot login) | body `/sender_id`, query `moderator_id` | id |
| discord | `{target.channel_id}` | - | the signed origin stamp (`origin_channel`) | one path segment | id |
| discord | `Authorization` (header-credential convention) | `discord-bot` | `DiscordSlots` (per community, per call); host composes `Bot <token>` | header | secret |
| discord | `{dm.channel_id}` | - | duty: the `id` in Discord's reply to the host's own `POST /users/@me/channels` | path segment of `dm.send` | id |
| discord | `{target.guild_id}` | - | duty: the community binding's guild (never payload/template) | path of `role.*` | id |
| discord | `{target.user}` | - | duty: the entry's `{user:<uuid>}` detokenized, community-scoped | path of `role.*` | id |
| discord | `{target.role}` | - | duty: managed-role key -> binding allowlist snowflake, privileged roles refused | path of `role.*` | id |
| youtube | `YT_OAUTH_TOKEN` | `youtube.oauth` | resolver | `Authorization: Bearer <slot>` | secret |
| youtube | `YT_LIVE_CHAT_ID` | `youtube.live_chat_id` | resolver | body `/snippet/liveChatId` | id |
| youtube | `YT_CHANNEL_ID`, `YT_VIDEO_ID` | `youtube.channel_id`, `youtube.video_id` | resolver | declared for the read path; refused in every outbound kind | id |

Shapes: `id` = `[A-Za-z0-9_-]{1,128}`, `secret` = visible ASCII 1..4096 bytes. Fixed endpoints: Twitch `POST /helix/chat/messages`,
`DELETE /helix/moderation/chat?broadcaster_id&moderator_id&message_id`; Discord `POST /api/v10/channels/{c}/messages`,
`DELETE /api/v10/channels/{c}/messages/<id>`; YouTube `POST /youtube/v3/liveChat/messages?part=...`. Hosts: `api.twitch.tv`,
`discord.com`, `www.googleapis.com` (narrow-only via `PlatformPolicy::with_manifest_hosts`).

**What `verify` refuses** (all typed, content-free `TemplateError`s): method/path/query differing from the kind; a slot not declared,
not allowed for the kind, or in a position its declaration forbids; any `{{`/`}}`/brace outside a declared slot; control, zero-width,
bidi, tag characters; userinfo, fragment, non-`https`, a host not in the declared list (port included); a header outside the allowlist or
in the forbidden set (`Host`, `Cookie`, `Proxy-*`, `X-Forwarded-*`, `X-Waddle-Host-*`, ...); `Authorization` set by the template other
than `<allowed prefix><credential slot>`; `secret-refs` unknown / wrong handle / duplicate / missing; a body where none is allowed, over
the bound, not a JSON object, deeper than 8 or wider than 512 nodes, or without `Content-Type: application/json`.

**Never logged / never in an error / never a metric label:** URL, header value, slot text, credential, message text, ids. Errors carry
a stable code only (`ExecError::code`). Metrics (bounded labels): `svc_ingest_sender_exec_total{platform,kind,outcome}`,
`svc_ingest_sender_exec_seconds{platform,kind}`; unknown platform/kind collapse to `other`.

**Config:** flag `waddles.connector-tools` (default OFF, enforced, fail-closed); `SENDER_EXEC_DIGEST_<PLATFORM>` = `sha256:<64 hex>` of the
pinned connector component (a template built by any other digest is `DigestMismatch`; no pin is `NotPinned`).

### T6d-1 status (read before enabling)

* **Template source.** `build-request` is called through the `TemplateBuilder` seam. The production implementation,
  `WireTemplateBuilder` (`sender_exec/wire_builder.rs`), carries it over the executor wire (`penguin-bundle-host` 0.5.0
  `ExportKind::ConnectorBuildRequest`, penguin-libs#151; executor side `ConnectorHost::build_request` + `connector_wire`). It is BUILT BUT NOT WIRED INTO
  BOOT: `lib.rs` still starts the legacy drains. Follow-up (after the executor PR merges): construct
  `WireTemplateBuilder::new(link, registry)` where `lib.rs` already holds the `ExecutorLink`/`ConnectorRegistry`, build the
  `SenderExecutor`, and start `outbound::run_exec` / `run_discord_exec` instead of the legacy drains (seam documented in the
  `wire_builder` module doc). The drains are already executor-ready (readiness key, acks, stamp gate unchanged).
* **Legacy egress still present** (removed once the above lands): `outbound.rs` `RealIrcOutbound` (IRC), `discord_rest.rs` (Discord REST).
  `connector/poll.rs` is the INBOUND poll path (its own pinned client) and is unchanged.
* **Slot sources.** Twitch (`TwitchSlots`, per-send Helix lookup for numeric ids), Discord (`DiscordSlots`) and YouTube (`YoutubeSlots`,
  below) each have one. There is no composite router yet: the executor takes one `SlotSource`, so production wiring picks per platform.

### YouTube slot source

`YoutubeSlots` (`sender_exec/youtube_slots.rs`) over `ResolverYoutubeCredentials` (`outbound_creds_youtube.rs`). Per send, uncached:
list the community's `youtube` bindings, require one whose `live_chat_id` metadata equals the **stamp-pinned** chat and that no OTHER
community also claims, `resolve` it, re-assert provider/kind/community/connection and the RESOLVED record's own `live_chat_id`, then
hand out only the requested handles. `pin_target("youtube")` verifies `channel == origin_channel` and `^[A-Za-z0-9_-]{1,128}$`, so a
live chat from the queue payload or guest is never used; an unstamped YouTube `chat.send` is refused `no_origin_pin`.

| Slot | Handle | Source |
|---|---|---|
| `{{YT_OAUTH_TOKEN}}` | `youtube.oauth` | resolved connection's kind-1 `accessToken` (zeroizing; only the `Authorization: Bearer` header position) |
| `{{YT_LIVE_CHAT_ID}}` | `youtube.live_chat_id` | the resolved record's `live_chat_id` metadata (== the stamp pin) |
| `YT_CHANNEL_ID` / `YT_VIDEO_ID` | `youtube.channel_id` / `youtube.video_id` | refused `unknown_handle` (read-path only) |

**Expiry:** the executor never refreshes. hub-api resolves/refreshes; a resolved token whose `expiresAt` is past (or within 30 s) is
`token_expired` (`CredError::TokenExpired`, a retryable `SlotError::Unavailable`). Reason codes: `no_origin_pin`, `no_connection`,
`channel_not_owned`, `connection_not_usable`, `token_expired`, `credential_unavailable` (several of the community's connections owning
the same chat resolve to the lowest connection id).

**hub-api side (shipped):** the `youtube` provider adapter (`hub_api/services/provider_connections/youtube.py`; guide:
`docs/PROVIDER_CONNECTIONS.md` "YouTube provider") creates the connection through a Google OAuth connect flow (code + PKCE), refreshes
the token (`refreshable=True`, per-tenant Google app), and carries the `live_chat_id` binding (`^[A-Za-z0-9_-]{1,128}$`, admin-only
writes, set by `PATCH` or discovered from the channel's active broadcast). It serves the access token as kind-1 `accessToken` + `expiresAt`
+ `metadata {live_chat_id}` -- exactly what `ResolverYoutubeCredentials` parses. A connection with no `live_chat_id` yet is skipped by the
list and `409 metadata_incomplete` on resolve, so its sends refuse `no_connection` until it is bound.

### Discord host duties (T6d-2)

`DiscordDuties<C: DiscordCredentialSource, U: PlatformUserResolver>` implements `DutyHook`; `PlatformPolicy::discord()` carries the
real `dm.send` path (`/api/v10/channels/{dm.channel_id}/messages`) and `role.add` (`PUT`) / `role.remove` (`DELETE`)
`/api/v10/guilds/{target.guild_id}/members/{target.user}/roles/{target.role}`. Order per op: gate -> payload -> build -> `bind_template`
-> verify -> `DiscordSlots` (token + channel-in-guild proof) -> duty slots -> guarded send.

| Duty | Behaviour | Refusal codes |
|---|---|---|
| `{user:<uuid>}` recipient | entry `user_id` must be a UUID (bare or `{user:..}`); `PlatformUserResolver` maps it to the raw Discord id inside the executor only, scoped to the stamp's tenant + community (`tenant_id` rides in the MAC-bound stamp; absent -> refused); raw id lives in zeroizing values, never logged/stored; a raw snowflake is refused | `user_not_tokenized`, `no_tenant_scope`, `user_not_in_community`, `tokenization_denied`, `tokenization_bad_request`, `tokenization_bad_answer`; retryable: `tokenization_unavailable`, `tokenization_rate_limited` |
| `send_dm_in_community` gate | before any template: pinned channel is in the binding's guild AND the recipient is a member (`discord_rest::classify_membership`, shared with the legacy client) | `not_in_community`; lookup failure `membership_lookup_failed` (nothing sent) |
| `{dm.channel_id}` | host `POST /users/@me/channels {recipient_id}` per operation; slot filled only from that reply; no state kept, so another dm.send's channel is never reused; no `dm.post` kind | `dm_open_failed` |
| Managed roles | overlay emits a KEY; host maps it through the binding's `managed_roles`, refuses roles whose permissions include ADMINISTRATOR / MANAGE_ROLES / MANAGE_GUILD (role list via guarded client, 30s TTL cache keyed by numeric guild id, <=256 guilds), pins the guild from the binding | `role_not_managed`, `role_privileged`, `role_not_found`, `role_lookup_failed` (fail closed) |
| Credential | `DiscordSlots`: per community, per call, uncached `Authorization: Bot <token>`; the channel must be in the binding's guild | `channel_not_owned`, `connection_not_usable`, `ambiguous_binding`, `no_connection` |

`X-Waddle-Host-Dm-Recipient`: the contract does not need it (the recipient is the entry's own, already host-known). The policy keeps
`X-Waddle-Host-*` forbidden; `bind_template` strips the DM pseudo-header BEFORE verification after checking it equals the entry's
own `{user:<uuid>}` (`recipient_mismatch` otherwise), and rewrites exactly the entry's `{user:<uuid>}` / `{target.role.<key>}` to the
fixed duty slots -- any other user/role in the template is an undeclared slot. Role ops ride the queue as `op: role.add|role.remove`
with `user_id` (UUID) and the managed-role KEY in `text` (so the `pin_sig` MAC covers it); a `guild_id`/`role_id` field is ignored.
Metric `svc_ingest_discord_duty_refused_total{kind,reason}`.

**Binding contract** (`outbound_creds_discord.rs`; hub-api PR #1002): list `items[].metadata` / resolve `metadata` =
`{"guild_id": "<snowflake>", "managed_roles": "key:snowflake,..."}` (`managed_roles` optional/omitted when empty, <=32 entries, keys
`[a-z][a-z0-9_]{0,31}`; a JSON-object string is accepted defensively). The bot token is the kind-1 `accessToken`
(`ConnectionSecret::OAuthAccessToken`); any other secret scheme/kind is refused. More than one binding per community is refused
(`ambiguous_binding`).

**Production user resolver** (`platform_users.rs`): `HubPlatformUserResolver::connect(endpoint, token_endpoint, sa_token_path, ca)`
mints a machine JWT with scope `identity:platform-user:resolve` and calls `ResolvePlatformUser{tenant_id, community_id, platform:"discord",
uuids}` (1..=25 canonical uuids, one RPC per call, never per-uuid probing: any miss is ONE `NOT_FOUND`). `PERMISSION_DENIED`/
`UNAUTHENTICATED` -> `Denied`, `RESOURCE_EXHAUSTED` -> `RateLimited` (backoff, not a refusal), transport/circuit -> `Unavailable`.
Build needs `protoc` + the repo-root `proto/` tree (Dockerfile.rust installs protoc and takes `proto` as a named build context, like svc_process).

**Producer side (done, P6a producer-completeness):** `svc_action` emits `role.add`/`role.remove` (`relay.send` with
`{"op":"role.add","user_id":"{user:<uuid>}","role_key":"vip"}`; authorized by the existing dangerous `moderation.discord` grant;
host-pinned to the event's channel exactly like `chat.send`/`dm.send`; any bundle `guild_id`/`role_id`/`channel`/`tenant_id` is
ignored; throttled 20/min per (tenant, community, app); the role KEY rides in the MAC-covered `text`) and stamps the numeric
`tenant_id` + `community_id` from the verified invoke scope on `dm.send` and the role ops (never from the bundle). `dm.send` also
accepts a `{user:<uuid>}` recipient, the only form this executor's duty accepts. Every signed entry carries a unique `op_id`.

**Gaps (not silent):** (1) YouTube has a slot source and a hub-api
provider adapter (see "YouTube slot source"); (2) the executor and `HubPlatformUserResolver::connect` are not wired at boot (T6d-1 status above);
the default `NoUserResolver` refuses every DM/role op loudly until it is.

## Overlay host selector (connector runtime)

Env baselines, default OFF, strict booleans, read identically by the Rust and Python svc-ingest (never the PostHog client: the `*.waddles.app` bypass would read every flag ON).

| Env | Effect when ON |
|---|---|
| `OVERLAY_TWITCH_ENABLED` | legacy Twitch IRC inbound off; Twitch outbound drains off only if `CONNECTOR_TOOLS_ENABLED`; legacy EventSub stays unless `twitch` in `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS` |
| `OVERLAY_DISCORD_ENABLED` | FATAL until the identify budget is ported (`IDENTIFY_BUDGET_READY`) |
| `OVERLAY_YOUTUBE_ENABLED` | Python `youtube_live_poll` off |
| `CONNECTOR_RUNTIME_ENABLED`, `CONNECTOR_RECONCILE_INTERVAL_SECS` (60), `CONNECTOR_MAX_CONNECTIONS` (500), `CONNECTOR_START_CONCURRENCY` (8), `CONNECTOR_DISCORD_START_PACE_MS` (5500), `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS` | see spec 2026-11-05 section 1.3 |

Any overlay ON validates, in order and naming the item: `INGEST_DYNAMIC_CONNECTIONS`, `HUB_API_GRPC_ENDPOINT`, `SERVICE_JWT_TOKEN_ENDPOINT`, `SENDER_EXEC_DIGEST_<P>`, `HOST_API_SERVER_CERT_FILE`/`_KEY_FILE`/`HOST_API_CLIENT_CA_FILE`/`HOST_API_CLIENT_IDENTITY`, spine config, `CONNECTOR_ROUTE_KEY_SECRET` (when webhooks listed). `/readyz` reports each overlay platform (`overlay[]`, reason while not ready).

## Outbound executor — boot wiring & rollout order (P6a, `src/outbound_boot.rs`)

Flag `waddles.connector-tools`, env baseline `CONNECTOR_TOOLS_ENABLED` (default OFF, read once at
startup, like `INGEST_DYNAMIC_CONNECTIONS` -- the `*.waddles.app` license bypass would make a
licensing-client read always ON). Metric `svc_ingest_outbound_path{path=legacy|executor,platform}` = 1
for the path that started.

| Flag | Behaviour |
|---|---|
| OFF | legacy drains exactly as before (`RealIrcOutbound`, `discord_rest`); none of the settings below are read |
| ON | executor path for twitch/discord/youtube; ANY missing/malformed item below is a fatal startup error naming the item (never a value, never a fallback to legacy) |

**Flag-ON startup requirements** (all host config, never from the queue or the guest):

| Item | Source |
|---|---|
| `INGEST_DYNAMIC_CONNECTIONS=true`, `SERVICE_JWT_TOKEN_ENDPOINT`, hub-api URL | per-community creds via `connection_resolver` |
| `OUTBOUND_PIN_KEYS` + `OUTBOUND_PIN_ACTIVE_KID` (active kid must be in the ring) | `outbound-pin` Secret (#987) |
| `SENDER_EXEC_DIGEST_TWITCH` / `_DISCORD` / `_YOUTUBE` (`sha256:<64 hex>`) | chart `svcIngest.connectorTools.digests.*` |
| `TWITCH_CLIENT_ID` (public Helix app id) | platform-credentials Secret key |
| `HUB_API_GRPC_ENDPOINT` (+ `HUB_API_GRPC_CA_FILE`) | `HubPlatformUserResolver::connect`, scope `identity:platform-user:resolve` |
| `HOST_API_SERVER_CERT_FILE` / `_KEY_FILE` / `HOST_API_CLIENT_CA_FILE`, `HOST_API_CLIENT_IDENTITY`, `SENDER_EXEC_HOST_API_PORT` (8301) | mTLS listener the executor dials (`connector/host_api_listener.rs`). Server-only cert from svc-ingest's OWN Secret (never the shared executor/stage `host-api-tls`); the client cert must chain to its CA AND carry exactly `HOST_API_CLIENT_IDENTITY` (a stage cert is refused) |

Wiring: `PlatformSlotRouter` (twitch/discord/youtube slot sources; unknown platform -> typed
`slot_platform_unknown`) + `DiscordDuties` + `WireTemplateBuilder` over the `ExecutorLink`; the drains are
`run_exec` (twitch), `run_discord_exec`, `run_youtube_exec` (queue
`waddles:transport:irc:youtube:outbound`, executor-only). Shutdown uses the same per-drain SIGINT/SIGTERM
receivers as the legacy drains. The link answers the overlay cutover flags
(`waddles.overlay-{twitch,discord,youtube}`) `true` to `flags.enabled` host-calls; every other host-call is
refused.

**Rollout order:** (1) deploy `svc_action` first (pin_sig v2; v1-signed entries are refused
`pin_sig_legacy`), (2) deploy `svc_ingest` with the flag OFF, (3) set `connectorTools.digests.*`, (4) set
`connectorTools.enabled=true`. **Rollback = flag OFF** (restarts onto the legacy drains; in-flight queue
entries are consumed by whichever path is running).

**Connector host-call bridge (`connector/host_calls.rs`, runtime spec decision 2.4).** `ExecutorLink::with_host_calls(Arc<dyn
HostCallHandler>)` plugs in `OverlayHostCalls`, the explicit allowlist for the host-calls an overlay makes (default stays deny-all).
The executor stamps `args._scope = {connection_id}` after guest code for every export that carries a `ConnectionCtx`; the handler
maps it through the host-owned `ActiveConnections` table (`connection_id -> platform/community/tenant`) to a `CallScope`.
`tool-call` exports carry no ctx, so their host-calls are UNSCOPED and **denied** (`build-request` only gets its own flag read, see below) (typed `denied`/`unscoped_call`,
counted, no fallback). Also denied: unknown/stale connection, unknown overlay, an overlay acting on another platform's
connection, and any op needing a grant the overlay's `hub-manifest.yaml` lacks (twitch: `connector.pii.read`, `flags.read`;
discord: `connector.pii.read`, `platform.log`; youtube: `connector.pii.read`; a unit test parses the real manifests).

| Capability / op | Behavior |
|---|---|
| `context get-context` | tenant + community from the `CallScope` (needs `connector.pii.read`) |
| `db identity.lookup` | `platform-identity` only; `args.tenant`/`platform` cross-checked against the scope; returns `{uuid, linked:false, handle:null, display_name:null, tenant}` via `HubIdentityTokenizer` (`MintEphemeralPseudonyms`, scope `identity:ephemeral:mint`, no handle sent); 200/s per overlay; uuid-kind (detokenize) denied |
| `flags enabled/tier` | own `waddles.overlay-<platform>` key only, answered from the env baseline (needs `flags.read`) |
| `log write` | fixed-field PII-free line, guest text dropped (needs `platform.log`) |
| `clock now-millis/now-rfc3339/monotonic-nanos` | served |
| everything else (http, kv, relay, other db ops) | `denied` / `not_serviced` |

Limits: 2 s deadline, 16 in flight per overlay, 4 per connection (over cap = `quota_exceeded`, never queued). Metrics:
`connector_host_call_total{capability,outcome}`, `connector_host_call_denied_total{reason}` (bounded labels, no ids). Wired at boot
by the connector runtime (below): the supervisor `activate`s/`deactivate`s each connection, and `build-request` flag reads are served through the
host-recorded `CallOrigin::BuildRequest` (the link matches the call's `call_id` to the build-request invocation IT sent; only the invoked overlay's
own `waddles.overlay-<p>` flag is answered, every other unscoped call stays `unscoped_call`-denied).

**Known prerequisite outside this crate:** a `bundle-executor` must dial this pod's host-api port with the
three connectors pinned (`Executor::pin_connector`) and a host-call bridge so the twitch overlay's
`waddles.overlay-twitch` gate can read the flag. Neither exists in the production executor yet
(`connector_wire` doc: "no host-call bridge today"); until then every build-request is a typed, counted
`build_unavailable` (nothing is sent). The real-path proof is `core/connector_e2e/tests/outbound_exec.rs`.


## Connector runtime boot (PR-E, spec `2026-11-05-connector-runtime-inbound-manager.md`)

Opt-in per platform by the env baselines `OVERLAY_{TWITCH,DISCORD,YOUTUBE}_ENABLED` (default OFF, strict bool; Discord stays a fatal boot error while
`IDENTIFY_BUDGET_READY` is `false`). With every overlay OFF (and `CONNECTOR_RUNTIME_ENABLED` unset) nothing below is built, bound or spawned --
`tests/connector_runtime_default_boot.rs` pins it (no `connector_runtime_*`/host-call series, no overlay in `/readyz`, webhook ingress stays 503).

```text
 lib.rs run_with_shutdown (data-plane flag ON, any overlay ON)
   connector_runtime::boot::start_overlay
     RuntimeSettings (digests SENDER_EXEC_DIGEST_<P>, CONNECTOR_* knobs, CONNECTOR_HEARTBEAT_ENABLED)
     webhook ingress (WebhookEnv::from_lookup -> build_ingress)  ──> state.webhook   BEFORE http::router
     start_bridge: HubIdentityTokenizer + HostBridge{ExecutorLink+OverlayHostCalls, ActiveConnections} + mTLS listener (8301)
     start_runtime: spine + Valkey lease -> assemble -> spawn ConnectorRuntime::run ; ready handle -> /readyz
   start_outbound(shared link)      outbound executor rides the SAME link/listener (no second bind)
```

| Piece | Behavior |
|---|---|
| Tenant | `ScopeTenantResolve`: hub-api scope resolution of the profile's upstream resource (twitch channel / discord guild) must resolve to the connection's own community, else the connection is parked (`tenant_unresolved`/`tenant_mismatch`). YouTube has no scope mapping yet: parked `tenant_unresolved` (not defaulted). Profiles take the feed's `tenantId` and still refuse a metadata tenant. |
| Sink | `ScopedSinkFactory`: one sink per connection stamping that connection's resolved numeric scope (transports' `source_id` is an opaque hash no per-event lookup could resolve). |
| Twitch secrets | `ResolverSecrets::with_wrapper("twitch", TwitchFieldSecrets)`: `pass` via the community-bound source, `nick`/`channel` from the validated config only. |
| Host calls | `ConnectionCtx.connection_id` is pinned to the hub connection row id (what the executor stamps into `_scope`); `TableActivator` registers `(platform, community, tenant, connection)`. A different scope for a live id is refused (`activation_failed` park) and `deactivate` is compare-and-remove (refcounted per exact scope), so a stale guard never removes a newer registration. |
| Rotation | `BindingSummary.secret_version` (hub-api `secretVersion`, >= 1) is in every fingerprint; a bump restarts the connection. |
| Webhooks | `IngressAttach`: `activate_plan` for Webhook plans, `deactivate_route` on end; listed by `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS`. |
| Readiness | `/readyz` per platform: enabled, executor attached, pinned ok, desired/live, discovery failed. Overlay ON with the data plane OFF or a prerequisite missing is a fatal named boot error. |

Env (helm #1012 names): `OVERLAY_<P>_ENABLED`, `SENDER_EXEC_DIGEST_<P>`, `HOST_API_{SERVER_CERT,SERVER_KEY,CLIENT_CA}_FILE`, `HOST_API_CLIENT_IDENTITY`,
`SENDER_EXEC_HOST_API_PORT`, `HUB_API_GRPC_ENDPOINT`/`HUB_API_GRPC_CA_FILE`, `CONNECTOR_RECONCILE_INTERVAL_SECS`, `CONNECTOR_MAX_CONNECTIONS`,
`CONNECTOR_START_CONCURRENCY`, `CONNECTOR_DISCORD_START_PACE_MS`, `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS`, `CONNECTOR_ROUTE_KEY_SECRET`,
`CONNECTOR_HEARTBEAT_ENABLED` (new, default false).
