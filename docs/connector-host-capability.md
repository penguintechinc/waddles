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
| `ConnectorInvoker` | `core/svc_ingest/src/connector/invoker.rs` | Seam to the executor. Production default `UnwiredInvoker` fails every call loudly |
| `EventSink` / `SpineEventSink` | `core/svc_ingest/src/connector/sink.rs` | Validates events via `penguin_spine::PlatformEvent` and publishes through the one `publish_event` site |
| `ExecutorInvoker` | `core/connector_e2e/src/lib.rs` | `ConnectorInvoker` over the in-process `ConnectorHost` -- the real-path harness |
| Echo connector | `core/bundle_executor/tests/fixtures/echo-connector-src` | **Test fixture only** (no production build/deploy path): a faithful port of `builtin_handlers/echo_ingest.py::normalize`; the B0 smoke. Because it is not production code, `.ci/builtin-handler-map.yaml` keeps `echo_ingest` **pending** -- the parity gate must not go green on a fixture |
| Fault connector | `core/bundle_executor/tests/fixtures/faulty-connector-src` | Trap / spin / memory hog / event flood on demand |

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

- **Wire exposure.** `penguin_bundle_host::wire::ExportKind` (external `penguin-libs`) has only `Transform` /
  `Dispatch`, so a stage cannot yet ask the executor process to run a connector export. Until `penguin-libs` adds
  connector kinds and every `svc_*`/executor rev is bumped, svc_ingest's invoker is `UnwiredInvoker`
  (`ConnectorInvokeError::Unavailable`). The contract is proven in-process by `core/connector_e2e`.
- Real platform transports, heartbeat scheduling (a dedicated per-connection task, spec S3.5), secret-handle
  substitution in the outbound path (spec S2.1), `identity.lookup` backing (RO-replica reader, cache, audit,
  invalidation -- spec S3.3/S3.4), sender dispatch in `svc_action`, shadow-mode harness, flipping any
  `rustDataPlane` default, Ed25519 verification ahead of the connector `Linker` (`VerifiedManifest` is its output).

## Running it

```bash
cd core/bundle_executor && cargo test --test connector_invocation --lib connector   # executor half
cd core/svc_ingest      && cargo test --test connector_driver --test connector_sink --lib connector
cd core/connector_e2e   && cargo test                                              # the end-to-end smoke
```

Rebuilding the fixtures: see `core/bundle_executor/tests/fixtures/README.md`.
