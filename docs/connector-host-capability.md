# Connector-bundle host capability (issue #840, "B0")

The serial prerequisite that lets the Rust data plane run WASM **connector bundles**
(`waddle:connector@1.0.0`, `wit/waddle-connector/connector.wit`). Design authority:
`docs/superpowers/specs/2026-09-28-connector-bundles.md` (S2, S2.3, S2.6, S3.5, S6 phases 0-2).
B0 is host plumbing only -- it implements **no** platform connector.

## Shape

```text
 svc_ingest (host)                                        bundle_executor (sandbox)
 -----------------                                        -------------------------
 Transport  --raw bytes-->  ConnectorDriver
 (socket/poll/webhook)         | size ceiling (host, pre-guest)
                               | ConnectorRegistry (always-pinned)
                               v
                       ConnectorInvoker  ========>  ConnectorHost  (pinned, pooled)
                               ^                       fresh bounded Store per call
                               | normalized events      fuel + epoch + memory caps
                       cap + platform check             typed ConnectorCallError
                               v
                          EventSink -> publish_event -> Valkey spine (D30 envelope)
```

| Piece | Where | Role |
|---|---|---|
| `ConnectorHost` | `core/bundle_executor/src/connector.rs` | Loads one core connector (core namespace, `connector` world, digest-checked), pre-links against the per-component `Linker`, calls `on-connect` / `on-frame` / `on-heartbeat-due` / `on-disconnect` |
| `Transport` trait | `core/svc_ingest/src/connector/transport.rs` | Host-owned socket/poll/webhook abstraction, one impl per family (none shipped in B0) |
| `ConnectorRegistry` | `core/svc_ingest/src/connector/registry.rs` | Always-pinned platform -> connector map; no removal/eviction; re-registering is an error |
| `ConnectorDriver` | `core/svc_ingest/src/connector/driver.rs` | connect -> `on-connect` -> handshake -> frame pump -> reconnect under `crate::ingest::Backoff` |
| `ConnectorInvoker` | `core/svc_ingest/src/connector/invoker.rs` | Seam to the executor + the typed three-class `ConnectorInvokeError` |
| `WireConnectorInvoker` / `ExecutorLink` | `core/svc_ingest/src/connector/wire_invoker.rs` | **Production invoker**: carries each `receiver` export as an `ExportKind::Connector*` `invoke` frame over the executor host-API wire. `ExecutorLink` is svc_ingest's listening side (executor dials, `hello`/`hello-ok`, correlation-id multiplexing); no executor attached = typed `Unavailable`, loudly |
| `ConnectorRouter` | `core/bundle_executor/src/connector_wire.rs` | Executor side of the wire: always-pinned `ConnectorHost` registry keyed by digest; translates the canonical-JSON payloads and maps `ConnectorCallError` onto typed `error` frames |
| `EventSink` / `SpineEventSink` | `core/svc_ingest/src/connector/sink.rs` | Validates events via `penguin_spine::PlatformEvent` and publishes through the one `publish_event` site |
| `WireRig` | `core/connector_e2e/src/lib.rs` | Real executor + real `ExecutorLink` joined by a duplex pipe (stand-in for the mTLS socket) -- the real-path harness; reconnects and severs are real |
| Echo connector | `core/bundle_executor/tests/fixtures/echo-connector-src` | **Test fixture only** (no production build/deploy path): a faithful port of `builtin_handlers/echo_ingest.py::normalize`; the B0 smoke. Because it is not production code, `.ci/builtin-handler-map.yaml` keeps `echo_ingest` **pending** -- the parity gate must not go green on a fixture |
| Fault connector | `core/bundle_executor/tests/fixtures/faulty-connector-src` | Trap / spin / memory hog / event flood on demand |

## Wire contract (`penguin-bundle-host` 0.3.0 `ExportKind::Connector*`)

`invoke.export` is one of `connector_on_connect` / `connector_on_frame` / `connector_on_heartbeat_due` /
`connector_on_disconnect`; `app_id` + `digest` name the pinned connector (executor refuses an unpinned digest or a
mismatched `app_id`: `UNKNOWN_BUNDLE`). Payloads are canonical JSON, unknown fields rejected, all `bytes` base64:

| Export | `payload` | `result.payload` |
|---|---|---|
| `connector_on_connect` | `{ctx}` | `{bytes, secret_refs:[[slot,handle],..]}` |
| `connector_on_frame` | `{ctx, bytes, received_at}` | `{events:[{platform,event_type,actor,payload_json,occurred_at}]}` or `{frame_error:{kind,message}}` |
| `connector_on_heartbeat_due` | `{ctx}` | `{bytes}` (`null` = none) |
| `connector_on_disconnect` | `{ctx, reason}` | `{}` |

Sandbox containment is a typed `error` frame, mapped back by `WireConnectorInvoker` (by code, never message text; the
executor's message is never carried into the host error): `EXECUTOR_DEADLINE` (+`fuel_exhausted`) / `MEMORY_LIMIT` /
`WASM_TRAP` -> `SandboxTrip`; `EXECUTOR_DEADLINE`+`pool_exhausted`, any other code, no executor, lost connection, reply
timeout or a malformed reply -> `Unavailable`.

## Failure classes (spec S2.6)

| `on-frame` outcome | Host reaction |
|---|---|
| `ok(events)` | cap at `max_events_per_frame` (64), drop events whose platform differs from the pinned one, enforce the size caps below, publish the rest |
| connector `frame-error` | that frame is dropped and counted; the connection stays up |
| trap / fuel / deadline / memory cap / pool exhausted / invoker unavailable | connection torn down, `svc_ingest_connector_trap_total{platform,kind}`, reconnect under backoff; the connector keeps serving other connections |
| transport `TransportError::Fatal` (e.g. 401, dead credential) | source **parked**: no reconnect, `ConnectorDriver::run` returns `RunEnd::Parked`, `svc_ingest_connector_parked_total{platform}`; the transport records `needs_reauth` itself |

Frames larger than the pinned `max_frame_bytes` are dropped **before** any guest runs.

**Backoff reset** (`driver::should_reset_backoff`): only after a connection was sustained for `STABILITY_WINDOW` (60s)
and did not end in a connector fault -- never merely because a frame or two succeeded before a trap (a poison frame
at position >= 2 used to reset it every cycle and hammer the upstream).

**Host size caps** (`registry::EventLimits`, per pinned connector, typed `SinkError::Oversize`/`Invalid`, counted as
`events_total{outcome=oversize|invalid}`, never silently truncated): `payload_json` 64 KiB, `actor` 256 B,
`event_type` 128 B, summed `payload_json` per frame 1 MiB; `event_type` must be a dotted lowercase namespace
(`message`, `channel.raid`).

**`Transport::on_frame_outcome`** -- after every yielded frame the driver reports `FrameOutcome::Published` or
`Dropped(Oversize | GuestError | ConnectorFault | PublishIncomplete)`, so a stateful poll transport can hold its
snapshot cursor instead of advancing past a frame that was dropped. Default is a no-op (stateless transports).

## Isolation (spec S2.3 / S2.6 / S3.5)

- **Fresh `Store` per call, never reused.** Strictly stronger than "destroy a trapped Store": no guest-corrupted
  state can leak into any later call. The pool is a semaphore (`EXECUTOR_CONNECTOR_POOL_SIZE`, wait
  `EXECUTOR_POOL_WAIT_MS`, then typed `pool_exhausted`); the compiled `Component` is shared read-only.
- **Three bounds from the one `Store` constructor** (`invoke::new_bounded_store`): epoch deadline, memory cap, and
  fuel (`EXECUTOR_CONNECTOR_FUEL_LIMIT`, default 50M; spec asks for per-platform tuning).
  `engine::spawn_epoch_ticker` **must** run for any in-process caller, or fuel is the only CPU bound.
- Guest runs on a blocking-pool thread (`spawn_blocking` + `block_on`), never the async reactor.
- `ConnectorHost::load` refuses: non-`connector` world, non-core `app_id`, bytes not matching the digest, a component
  that does not compile / link / export the connector world. `identity.lookup` links only per
  `VerifiedManifest::may_link_identity` (unchanged).

## Observability (PII-free, bounded labels)

Executor (OTel): `waddles_executor_connector_calls_total`, `waddles_executor_connector_call_seconds`
(labels `app_id, export, outcome`) -- see `docs/observability/data-plane-metrics.md`.
svc_ingest (Prometheus): `svc_ingest_connector_{frames,events,trap,connects}_total`,
`..._on_frame_duration_seconds`, `..._connection_up` (an inc/dec **count** of live connections per platform, correct
with N connections), `..._parked_total`. Spec S2.6 names `source_id` as a trap-metric label; it is deliberately
omitted (unbounded cardinality). Nothing frame-derived (bytes, actor, payload, guest message) is logged or used as a
label: logs carry error **classes** only (`SandboxTrip.message`/`Unavailable` payloads are never logged, the
executor withholds the wasmtime error chain, `SinkError::Invalid` is a `&'static str`). Flood-prone WARNs (oversize,
spoofed platform, rejected/failed events, truncation) are sampled -- 5 per 30s per category per connection with a
`suppressed_before` count -- while every occurrence is still counted. `tracing` span `connector.on_frame` wraps each call.

## Heartbeat scheduling (P6a Task 5, spec S3.5)

`ConnectorDriver` runs a per-connection heartbeat loop concurrently with the frame pump (same task, bounded
`mpsc(1)` hand-off, so at most ONE `on-heartbeat-due` is in flight). A due heartbeat is written even while a
frame is still being processed (`on-frame` + publish only borrow the invoker/sink, never the transport). One
structural limit: the transport is a single `&mut`, so reading the NEXT frame pauses while a heartbeat write is
in progress; that write has a deadline (default 5s, `with_heartbeat_send_timeout`) after which it is cancelled and
the connection torn down + reconnected.

| Piece | Behaviour |
|---|---|
| `Transport::heartbeat_interval()` | `None` (default) = no heartbeat; outside `MIN_HEARTBEAT_INTERVAL..=MAX_HEARTBEAT_INTERVAL` (100ms..=1h, zero included) = `DriverError::HeartbeatIntervalOutOfRange` (before connect, only when the flag is ON) |
| `Transport::send_heartbeat(bytes)` | sends the connector body; the default FAILS LOUD (`TransportError::Send`); must be cancel-safe |
| `Transport::send_default_heartbeat()` | called when the connector returns `none` (WIT: "host sends its platform-default heartbeat unchanged"); the default FAILS LOUD |
| Schedule | first beat one period after establishment; missed ticks are delayed, never burst |
| Flag | `ConnectorDriver::with_heartbeat_enabled(bool)`; caller resolves PostHog `ingest.connector-heartbeat` (default OFF). OFF = no effect at all: no validation/refusal, no timer, one DEBUG line per connection |
| Outcomes (`svc_ingest_connector_heartbeats_total{platform,outcome}`) | `sent`, `default_sent`, `guest_error` (connection stays up), `send_failed` (fatal parks, else reconnect), `send_timeout` (reconnect), trap/`unavailable` kinds (teardown + backoff) |
| Latency | `svc_ingest_connector_heartbeat_duration_seconds{platform}`; span `connector.on_heartbeat_due` |

Shutdown cancels an in-flight heartbeat promptly, and an in-flight frame too; the frame is reported to the transport
as `Dropped`, so a stateful cursor transport re-yields it. The heartbeat future is dropped before `on-disconnect`
runs. Tests: `core/svc_ingest/tests/connector_heartbeat.rs` (paused-clock + real-time).

## WebSocket host-side frame rules and heartbeat (P6b host transport support)

`WsConfig` (`core/svc_ingest/src/connector/ws.rs`, rules in `transport_common.rs`) carries three opt-in, host-owned
settings. All default OFF; an invalid value is refused by `WsTransport::new` (`TransportError::Connect`, fail loud).

| Setting | Behaviour |
|---|---|
| `auto_replies: Vec<AutoReplyRule>` | Line rule `prefix` -> host-built `reply_prefix + token + reply_suffix` (e.g. `PING :` -> `PONG :`). The overlay never sees a consumed line unless `forward_to_guest`. Only the token after `prefix` is reflected: `[A-Za-z0-9.:_/+=@-]`, `1..=max_token_len` (hard cap 256); anything else (CR, LF, space, non-ASCII, empty, too long) is a `TransportError::Read` -- never a reply. Max 8 rules (non-overlapping prefixes), 8 replies per frame. Matching is per line, so a batched frame is answered and the remaining lines are still forwarded. Replies are queued and flushed at the top of the next `next_frame` (cancel-safe: popped only after a completed send; cleared on reconnect) |
| `self_echo: Option<SelfEchoFilter>` | Drops lines `[@tags ]<open><self_id><close><user@host> <command> ...` whose id equals `self_id` (ASCII case-insensitive) -- the bot's own echoed `PRIVMSG`. Host-side because the frozen `connector` WIT gives the guest no self identity; no WIT change. `self_id` is the bot's public login supplied by host config (never a secret, never guest data) |
| `heartbeat_interval: Option<Duration>` | Enables the driver's heartbeat loop for this source; `send_heartbeat` writes the connector's body (non-empty, `<= max_frame_bytes`). `send_default_heartbeat` FAILS LOUD unless `session.default_heartbeat` is configured (below); the interval may instead be captured from a frame (`session.heartbeat`) |

Metrics: `FrameRuleMetrics::register(&registry)` once, then `WsTransport::with_metrics`:
`svc_ingest_transport_frame_rules_total{platform,rule,outcome}` (`auto_reply`: `replied`/`reply_failed`/`rejected`;
`self_echo`: `dropped`). Labels are bounded; frame content and tokens are never logged or labeled.

Twitch IRC example: `auto_replies = [{prefix:"PING :", reply_prefix:"PONG :", reply_suffix:"\r\n", max_token_len:64}]`,
`self_echo = {self_id:<bot nick>, sender_open:":", sender_close:"!", command:"PRIVMSG"}`. Tests: `ws.rs`
(real local WS server: PING -> PONG, overlay never sees the PING, hostile token never reflected, own echo dropped,
others pass) and `transport_common.rs`.

Permission catalog: `connector.pii.read` (Dangerous, global-approved, first-party-core only) gates `identity.lookup`;
cataloged in `core/bundle_capability_gate/src/permission.rs` and `hub_api/services/bundle_permission_catalog.py`.

## Session lifecycle: captured state, dynamic heartbeat, reconnect/park, poll hints (P6b session lifecycle)

Platform overlays (Discord gateway, YouTube poll) need the host to run session mechanics a stateless guest cannot.
Design rule (same as `auto_replies`): **generic, declarative rules owned by host config** -- never from a guest, no
platform named in the transport, **no change to the frozen `connector` world or the WIT contract**. The host feeds
state back to the guest through the EXISTING opaque `session-token` and the handshake bytes it already returns.
Code: `connector/lifecycle.rs` (rules, engine, validation), wired into `ws.rs`, `poll.rs`, `driver.rs`. All rule sets
default empty (= byte-for-byte the previous behaviour); every misconfiguration is refused at `*Transport::new`
(`TransportError::Connect`, fail loud).

### WebSocket: `WsConfig.session: SessionRules`

| Field | Behaviour |
|---|---|
| `captures: Vec<CaptureRule>` | `when` (JSON conditions `[(pointer, Int/Bool/Str)]`, or an IRC command) + `pointer` -> named value in per-transport host state. Value = JSON string or non-negative integer; absent/`null` is skipped (heartbeat ACK `s:null`). **Rejected, never sanitized**: charset `[A-Za-z0-9._:/+=-]`, 1..=128 bytes; anything else (CR, LF, quote, brace, space, wrong type, negative) fails the frame (`TransportError::Read`) and stores nothing from it. Max 8 rules / 8 distinct names `[A-Z][A-Z0-9_]{0,31}` |
| `heartbeat: Option<HeartbeatCapture>` | Captures a millisecond number (HELLO `/d/heartbeat_interval`) as the transport's `heartbeat_interval()`. Outside `MIN..=MAX_HEARTBEAT_INTERVAL` (100 ms..1 h) -> rejected (`Read`). Per connection: reset on `connect()` (the next HELLO re-announces) |
| `directives: Vec<FrameDirective>` | Matched frame -> `Reconnect` (clean end, state kept, driver reconnects under backoff), `ReconnectFresh` (state dropped first), `Park` (`Fatal`, source parked). The frame is consumed. First match wins, max 8. `IrcCommand("RECONNECT")` matches the command token only, so `PRIVMSG #c :RECONNECT` (attacker text) never fires |
| `close_codes: Vec<CloseCodeRule>` | Inclusive ranges (1000..=4999, non-overlapping, max 16) -> same actions. Unclassified code = previous behaviour (plain close, reconnect, state kept) |
| `session_token_template` | Rendered into `ConnectionCtx.session_token` on every `connect()` when each `{{NAME}}` has a value (or `{{NAME\|default}}`); otherwise `None` = start fresh. Mutually exclusive with a static `resume_token` |
| handshake slots | `{{NAME}}` / `{{NAME\|default}}` for DECLARED state names inside the connector's handshake bytes are substituted host-side BEFORE the secret pass (values are charset-safe; secret slots are not state names and pass through). A declared slot with no value and no default fails the handshake loudly before any byte or secret leaves |
| `default_heartbeat` / `host_owned_heartbeat` | Host-built heartbeat body (`{"op":1,"d":{{SEQ\|null}}}`) used for `send_default_heartbeat` (connector answered `none`); with `host_owned_heartbeat` it also REPLACES a connector-supplied body (the guest cannot see the live sequence) |

State: per transport, bounded, survives reconnects (that is what RESUME needs), dropped by `ReconnectFresh`/a fresh-class
close code, never logged (`SessionEngine`'s `Debug` prints only the held count), never a metric label.

**Driver:** the heartbeat schedule now follows `Transport::heartbeat_interval()` live. The pump re-reads it after every
frame and publishes a change over a watch channel; the loop restarts its schedule (first beat one period later). A value
outside the driver bounds ends the connection (`TransportError`). With the flag OFF everything stays dormant.
Handshake ordering: by default the driver sends the `on-connect` handshake right after the socket opens (IRC and
every transport that does not opt in). `handshake_after: FirstFrame{timeout}` (below) makes it wait for the first frame.

Metrics (`FrameRuleMetrics`, same series): `svc_ingest_transport_frame_rules_total{platform,rule,outcome}` with
`session_capture` (`stored`/`rejected`), `heartbeat_interval` (`applied`/`rejected`), `directive` and `close_code`
(`reconnect`/`reconnect_fresh`/`park`; close also `unclassified`). Bounded enums only.

Discord overlay host config (matches the overlay's session-token JSON `{"session_id","seq"}`; the overlay should answer
`on-heartbeat-due` with `none` and let `host_owned_heartbeat` supply the live `d:<seq>`):

```rust
let op = |n| ("/op".to_string(), Scalar::Int(n));
SessionRules {
    captures: vec![
        CaptureRule { when: FrameMatch::Json(vec![op(0), ("/t".into(), Scalar::Str("READY".into()))]),
                      pointer: "/d/session_id".into(), name: "SESSION_ID".into() },
        CaptureRule { when: FrameMatch::Json(vec![op(0)]), pointer: "/s".into(), name: "SEQ".into() },
    ],
    heartbeat: Some(HeartbeatCapture { when: FrameMatch::Json(vec![op(10)]),
                                       pointer: "/d/heartbeat_interval".into() }),
    directives: vec![
        FrameDirective { when: Json([op(7)]),                          action: Reconnect },
        FrameDirective { when: Json([op(9), ("/d", Bool(true))]),      action: Reconnect },       // resumable
        FrameDirective { when: Json([op(9), ("/d", Bool(false))]),     action: ReconnectFresh },  // re-IDENTIFY
    ],
    close_codes: vec![ // fatal auth/config: 4004, 4010-4014 (+4013/4014 intents); fresh: 4007, 4009; resume: 4000-4003, 4005, 4008
        CloseCodeRule { from: 4004, to: 4004, action: Park },  CloseCodeRule { from: 4010, to: 4014, action: Park },
        CloseCodeRule { from: 4007, to: 4007, action: ReconnectFresh }, CloseCodeRule { from: 4009, to: 4009, action: ReconnectFresh },
        CloseCodeRule { from: 4000, to: 4003, action: Reconnect }, CloseCodeRule { from: 4005, to: 4005, action: Reconnect },
        CloseCodeRule { from: 4008, to: 4008, action: Reconnect },
    ],
    session_token_template: Some(r#"{"session_id":"{{SESSION_ID}}","seq":{{SEQ}}}"#.into()),
    default_heartbeat: Some(r#"{"op":1,"d":{{SEQ|null}}}"#.into()),
    host_owned_heartbeat: true,
}
```
Plus `slot_prefixes: [r#""token":""#]`, `handles {"discord-bot" -> bot-token connection}` as before. Not covered here
(explicit): the IDENTIFY budget / shard buckets (host-only, unchanged), intents and sharding (see below).

### Gateway gaps closed (P6b gateway gaps): reconnect URL, heartbeat-ACK timeout, handshake ordering

Code: `connector/gateway_rules.rs` (rules + URL validation), wired through `SessionRules`/`SessionEngine`, `ws.rs`, `driver.rs`.
All three default OFF/empty (= previous behaviour) and are refused at `WsTransport::new` when malformed.

| `SessionRules` field | Behaviour |
|---|---|
| `reconnect_url: Option<ReconnectUrlCapture{when, pointer, allowed_hosts, query}>` | Captures the URL the NEXT connect dials (READY `/d/resume_gateway_url`) -- only while a session exists to resume (`session_token_template` renders); otherwise the configured URL. **Validated strictly, rejected never sanitized** (the frame fails with `TransportError::Read`, nothing from it is stored, not even `session_id`): printable ASCII <= 256 B (CR/LF/space/NUL/non-ASCII refused), `wss://` only (`ws://` only to loopback under the test switch), no userinfo, no explicit port, path `/` only, no query/fragment, no trailing dot, host in the host-pinned `allowed_hosts` (exact, or `*.suffix` with a >= 2-label suffix and a non-numeric TLD -- `*.gg`, `*`, `*.0.1` refused; an IP literal only matches an exact entry). The host-configured `query` (`[A-Za-z0-9=&._-]`, e.g. `v=10&encoding=json`) is appended afterwards. A guest or frame can never choose the scheme, port, path or query. |
| SSRF guard on the reconnect dial | The accepted URL still goes through the same `resolve_pinned` (`bundle_host_http::guarded`, #989) as the configured one: every resolved address must pass the denylist (or be in `egress_allow`) and the connection is pinned to it. A refusal drops the session (`SessionEngine::clear`) so the next attempt is a fresh IDENTIFY on the configured URL -- logged (WARN, no URL) and counted `reconnect_url`/`refused_cleared`. A plain network failure keeps the session and retries (same URL) under driver backoff. |
| cleared on fresh identify | `ReconnectFresh` (op 9 d=false, close 4007/4009) and `clear()` drop the URL with the session state. |
| `heartbeat_ack: Option<HeartbeatAck{when, max_unacked}>` | Zombie-connection rule. Each heartbeat sent increments an unacked counter; a frame matching `when` (op 11) resets it. When a heartbeat is due with `max_unacked` (1..=3; 1 = Discord) already outstanding it is NOT sent: the transport returns `TransportError::Send("heartbeat ack timeout")`, the driver ends the connection (non-fatal -> reconnect under backoff, session state KEPT so the next connect RESUMEs on the reconnect URL). The socket is closed with code **4000** (never 1000/1001, which invalidate a gateway session) under a `connect_timeout` bound (a zombie peer never answers a close). Needs the driver heartbeat flag (`ingest.connector-heartbeat`) ON. Metric `svc_ingest_transport_heartbeat_ack_timeout_total{platform}` |
| `handshake_after: HandshakeAfter::{Connect (default), FirstFrame{timeout}}` | `FirstFrame`: the driver (via `Transport::handshake_after_first_frame`) calls `on-connect`, then reads ONE frame bounded by `timeout` (100 ms..=60 s) -- the transport applies its captures (heartbeat interval, session state) -- then sends the handshake, then processes that frame normally (`on-frame`, `on_frame_outcome`) so HELLO still reaches the guest. Timeout / close / read error before the first frame: NOTHING is sent (no IDENTIFY, no secret resolved), `ConnectionEnd::HandshakeFailed` (a fatal read parks), WARN, `svc_ingest_connector_connects_total{outcome="first_frame_timeout"}`. |
| `jitter_first_heartbeat: bool` | The first heartbeat of a connection is delayed by a random fraction `[0,1)` of the interval (Discord HELLO jitter) instead of a full period; later schedules unchanged. |

Metrics added: `svc_ingest_transport_heartbeat_ack_timeout_total{platform}`; `frame_rules_total` gains rule `reconnect_url`
(`stored`/`rejected`/`used`/`refused_cleared`). Residual, deliberate: the secret **destination** for the credential slot stays the
CONFIGURED gateway host (the provider-approved destination); the resume host is trusted only as far as the host-pinned
`allowed_hosts` + SSRF guard, so the RESUME payload (which carries the token) goes over TLS to an allow-listed host only.

Discord host config additions (to the example above; `allowed_hosts` is the platform's declared egress allow-list):

```rust
SessionRules {
    // ... captures/heartbeat/directives/close_codes/session_token_template/default_heartbeat as above ...
    reconnect_url: Some(ReconnectUrlCapture {
        when: FrameMatch::Json(vec![op(0), ("/t".into(), Scalar::Str("READY".into()))]),
        pointer: "/d/resume_gateway_url".into(),
        allowed_hosts: vec!["*.discord.gg".into()],
        query: "v=10&encoding=json".into(),
    }),
    heartbeat_ack: Some(HeartbeatAck { when: FrameMatch::Json(vec![op(11)]), max_unacked: 1 }),
    handshake_after: HandshakeAfter::FirstFrame { timeout: Duration::from_secs(10) },
    jitter_first_heartbeat: true,
}
```

Intents / sharding (documented, deliberately not built): the overlay sends `GUILDS | GUILD_MESSAGES | MESSAGE_CONTENT` = 33281
in IDENTIFY exactly like the hardcoded gateway (`bundles/rust/discord/README.md` parity table); no `shard` field is sent (single
shard, Discord's default), so `shard_id % max_concurrency` identify buckets and `session_start_limit` are host-only
fleet concerns that stay with the IDENTIFY budget. Multi-shard operation would need a per-shard host config (one `WsConfig`
per shard + a shard slot in the IDENTIFY template) -- not required for the current single-bot deployment.

### Poll: `PollConfig.lifecycle: PollLifecycle`

| Field | Behaviour |
|---|---|
| `interval_hint: Option<IntervalHint>` | `pointer` (e.g. `/pollingIntervalMillis`, integer or numeric string) -> next gap, **clamped** to `[min, max]` (max <= 24 h); no usable hint reverts to `PollConfig.interval`. `PollTransport::interval()` exposes it. Metric `poll_interval`: `applied`/`clamped`/`ignored` |
| `error_rules: Vec<HttpErrorRule>` | `status` (400..=599) + `reason_pointer` + `reasons` (charset `[A-Za-z0-9_.-]`, max 8) -> `Backoff{base,max,honor_retry_after}` or `Park`. Backoff pauses polling (not an error: no driver backoff/reconnect), doubles per consecutive hit, capped at `max` (<= 24 h); a numeric `Retry-After` replaces the computed wait (still capped). Reset by the next good response. A 403 matching no rule (or an unparsable body) keeps the old **Fatal** behaviour; 401 stays Fatal. The body is only read for a status some rule targets, bounded by `max_body_bytes`. Metric `poll_http`: `backoff`/`park`/`unclassified`/`body_unreadable` |
| `query_secret: Option<QuerySecret>` | `{param, handle}`: the secret resolved from `handle` (must be in `handles` = the manifest `secret_ref`; `param` must not equal `cursor_param` nor already be in the URL) is appended with strict form-urlencoding (everything outside `[A-Za-z0-9*-._]` is `%XX`, so it cannot end the parameter or add one). Resolved per request, never cached. Every transport error is a fixed class string -- the URL (which carries the key) is in no error, log or metric (tests enumerate refused-connect, 5xx, 403-park and captured tracing output). Residual: the `Url`/hyper own a non-zeroizing copy until the request completes |

YouTube overlay host config:

```rust
PollConfig { /* url = ".../liveChat/messages?part=id,snippet,authorDetails&liveChatId=...", cursor_param "pageToken",
                cursor_pointer "/nextPageToken", interval 5s */
    lifecycle: PollLifecycle {
        interval_hint: Some(IntervalHint { pointer: "/pollingIntervalMillis".into(),
                                           min: 2s, max: 60s }),            // overlay's 2 s floor
        error_rules: vec![
            HttpErrorRule { status: 403, reason_pointer: "/error/errors/0/reason".into(),
                reasons: vec!["quotaExceeded","rateLimitExceeded","dailyLimitExceeded","userRateLimitExceeded"],
                action: Backoff { base: 30s, max: 1h, honor_retry_after: true } },     // exponential, bounded
            HttpErrorRule { status: 403, reason_pointer: "/error/errors/0/reason".into(),
                reasons: vec!["forbidden","insufficientPermissions"], action: Park },
        ],
        query_secret: Some(QuerySecret { param: "key".into(), handle: "youtube.api_key".into() }),
    }, ..
}
```
`liveChatEnded`-style 403s are left unclassified (Fatal) on purpose: rediscovery is the overlay/host discovery loop's
job (not in this change). Tests: `connector/lifecycle_tests.rs` (real local WS + HTTP servers, wire asserted),
`tests/connector_session_lifecycle.rs` (real driver + `WsTransport`: HELLO -> heartbeat with live seq -> op 7 -> RESUME
-> 4004 parks), `tests/connector_heartbeat.rs` (announced interval start/re-schedule/out-of-range/flag-off).

## Connector tools (`world connector-tools`, P6a Task 4)

A connector overlay can EXPOSE normalized platform tools (`get_chatters`, `send_chat`, ...) and optionally CALL other
bundles' tools, reusing the P3b tool mechanism (`docs/bundle-tool-invoke.md`) -- so app bundles reach platform data
through `tool.invoke`, never a platform-specific host path.

- **New world, frozen old one.** `wit/waddle-connector/connector.wit` gains `world connector-tools` = `connector` +
  `import waddle:bundle/tool@1.0.0` + `export waddle:bundle/tool-provider@1.0.0`. `world connector` is untouched, so
  every compiled connector keeps linking. `deps/waddle-bundle/tool.wit` carries byte-identical `tool`/`tool-provider`
  interface bodies (checked by `wit/waddle-connector/connector_wit_test.sh`).
- **Host.** `WitWorld::ConnectorTools` (manifest) selects `engine::connector_tools_world`; the same core-namespace +
  `connector.pii.read` gates apply (`WitWorld::is_connector`). `ConnectorHost::call_tool(name, args)` runs
  `tool-provider.call` under the same pool slot / fuel / deadline / memory bounds as every other export; a guest
  refusal is `ConnectorCallError::Tool(ToolError)` (typed, not a sandbox trip), a plain-`connector` host answers
  `NoToolProvider` (fail loud, never an empty result).
- **Wire.** `ExportKind::ToolCall` for a digest pinned in the connector registry is served by the overlay
  (`{"ok": <json>}` / `{"tool_error": ...}`, the stage-tools result shape); a plain connector answers
  `EXPORT_MISSING`; an unpinned digest still falls through to the stage registry.
- **Resource isolation (security review).** Tool calls run on a DEDICATED semaphore (`tool_pool_size` = half the
  receiver pool, min 1), never the receiver pool, so saturating `tool-provider` traffic cannot starve `on-frame` /
  heartbeat (excess tool calls get typed `PoolExhausted`). The wire `deadline_ms` is clamped to
  `min(body.deadline_ms, limits.deadline_ms)` (`clamp_deadline_ms`) -- a caller can tighten, never extend. Regression
  tests: `saturated_tool_pool_cannot_starve_on_frame_or_heartbeat`, `a_requested_deadline_above_the_limit_is_clamped_*`.
- **`tool` import.** Linked via the shared `HasExecState` host impl; authority stays with the stage (grants, kind
  matrix, depth). With no stage bridge it answers a typed `target-unavailable`.
- **Gating / flag.** The executor only exposes the mechanism; nothing in production pins a `connector-tools`
  component yet (the first one is a P6b overlay). The flag `waddles.connector-tools` (default OFF) is ENFORCED by the
  outbound executor (P6a T6d-1, `svc_ingest::connector::sender_exec`): flag OFF => `FlagOff`, nothing is built or sent. Telemetry: `waddles_executor_connector_calls_total` /
  `_call_seconds` with `export="tool-call"`; logs carry `app_id` only (never args or results).
- **Tests.** `core/bundle_executor/tests/connector_tools.rs` (real compiled fixtures through the production
  engine/linker/wire path), `src/connector_wire.rs` and `src/manifest.rs` unit tests.

## Not in B0 (explicit, typed, not silent)

- **A production listener + pinned set.** The wire invoker takes any accepted duplex stream
  (`ExecutorLink::attach`); the mTLS accept loop and the executor-side startup list of pinned core
  connectors arrive with the first real platform connector (neither exists in production yet, and svc_ingest
  still constructs no `ConnectorDriver`). Pinned connectors run with no host-call bridge: a connector import
  that needs one (`http`, ...) fails typed, and any `host-call` frame is answered `HOST_CALL_DENIED`.
- Real platform transports, secret-handle
  substitution in the outbound path (spec S2.1), `identity.lookup` backing (RO-replica reader, cache, audit,
  invalidation -- spec S3.3/S3.4), sender dispatch in `svc_action`, shadow-mode harness, flipping any
  `rustDataPlane` default, Ed25519 verification ahead of the connector `Linker` (`VerifiedManifest` is its output).

## Running it

```bash
cd core/bundle_executor && cargo test --test connector_invocation --test connector_wire --lib connector   # executor half
cd core/svc_ingest      && cargo test --test connector_driver --test connector_sink --lib connector
cd core/connector_e2e   && cargo test                                              # the end-to-end smoke (over the real wire)
```

Rebuilding the fixtures: see `core/bundle_executor/tests/fixtures/README.md`.

## Outbound executor (`sender.build-request`, P6a Task 6d-1)

The host executes a connector's `build-request` -> `http-request-tpl` in `core/svc_ingest/src/connector/sender_exec/`.
Contract, slot table, verification rules, status and the T6d-2 plug-in points: `core/svc_ingest/README.md`
"Generic outbound executor". Summary: the overlay returns slots only; the HOST verifies the template against a per-platform/kind
policy (declared egress hosts, slot positions, header allowlist, no template-set `Authorization`), substitutes pinned-target slots
from the signed origin stamp / per-community connection and credential slots per call (never cached; zeroizing), percent-encodes
URL positions and JSON-escapes body positions, and sends ONLY through `GuardedClient`. `dm.send` and `role.*` host duties
(DM channel open, `send_dm_in_community`, community-scoped `{user:<uuid>}`, managed-role allowlist refusing ADMINISTRATOR /
MANAGE_ROLES / MANAGE_GUILD, per-community bot credential) are the Discord `DutyHook` (`sender_exec/discord_duties.rs`,
`discord_slots.rs`; table + gaps: `core/svc_ingest/README.md` "Discord host duties (T6d-2)"). YouTube `chat.send` is stamp-pinned to the live chat
and its OAuth token + chat id come from the community's own connection (`youtube_slots.rs`; `core/svc_ingest/README.md` "YouTube slot
source"; expired token -> retryable `token_expired`; hub-api has no YouTube provider adapter yet). The wire does not yet carry
`build-request` (no `ExportKind::ConnectorBuildRequest` in `penguin-bundle-host` 0.4.0): see "T6d-1 status".

## Outbound executor boot wiring (P6a, flag `waddles.connector-tools`)

svc_ingest listens (mTLS, `connector/host_api_listener.rs`) and the executor dials in, so
`ExecutorLink::attach` receives the stream; `WireTemplateBuilder` then runs `sender.build-request` on the
connector pinned for the platform. Pinned connector digests are HOST config
(`SENDER_EXEC_DIGEST_<PLATFORM>`), mirrored into the `ConnectorRegistry` and compared by the executor on
every send -- never taken from the queue entry or the guest. `ExecutorLink::with_enabled_flags` answers
`flags.enabled` host-calls for the overlay cutover flags (the twitch overlay refuses build-request while its
flag reads OFF); all other host capabilities stay `HOST_CALL_DENIED`. Flag-ON requirements, rollout order
and rollback: `core/svc_ingest/README.md` "Outbound executor -- boot wiring & rollout order". Real-path
proof: `core/connector_e2e/tests/outbound_exec.rs` (real Valkey queue, real compiled overlays over the real
wire, fake platform endpoints; only scheme+host of the overlay template is rewritten to the local fake).
