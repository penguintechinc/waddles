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

## Not in B0 (explicit, typed, not silent)

- **A production listener + pinned set.** The wire invoker takes any accepted duplex stream
  (`ExecutorLink::attach`); the mTLS accept loop and the executor-side startup list of pinned core
  connectors arrive with the first real platform connector (neither exists in production yet, and svc_ingest
  still constructs no `ConnectorDriver`). Pinned connectors run with no host-call bridge: a connector import
  that needs one (`http`, ...) fails typed, and any `host-call` frame is answered `HOST_CALL_DENIED`.
- Real platform transports, heartbeat scheduling (a dedicated per-connection task, spec S3.5), secret-handle
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
