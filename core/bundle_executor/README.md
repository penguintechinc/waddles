# bundle-executor

Credential-less WASM bundle sandbox host. Dials a stage's host-API port over
mTLS, loads signed bundle components, and runs `invoke` calls against them.
Design: `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS4.5/SS7.

## Concurrency, pooling and shutdown

One executor pod serves **every tenant**, so a single tenant's bundle must not
be able to starve the others. Three mechanisms enforce that:

| Concern | Behavior |
|---|---|
| WASM JIT compile (`Component::new`, seconds of CPU) | Runs on a tokio blocking-pool thread, never the async reactor. Bounded by `EXECUTOR_MAX_CONCURRENT_COMPILES`; extra cold loads queue. |
| Guest execution | Runs on a blocking-pool thread (epoch deadlines trap rather than yield, so a hot guest would otherwise stall the event loop). Bounded by the two call caps below. |
| `SIGTERM` / `SIGINT` | Executor stops accepting new `load`/`invoke` (`SHUTTING_DOWN`), keeps the host-API connection up so in-flight host calls complete, waits up to `EXECUTOR_SHUTDOWN_DRAIN_SECONDS`, then exits **0**. |

### Config knobs (env vars)

| Env var | Default | Effect |
|---|---|---|
| `EXECUTOR_MAX_CONCURRENT_CALLS` | `32` | Process-wide ceiling on in-flight invokes (semaphore). |
| `EXECUTOR_INSTANCES_PER_BUNDLE` | `4` | Ceiling on in-flight invokes of ONE bundle digest (per-bundle semaphore). |
| `EXECUTOR_POOL_WAIT_MS` | `500` | How long an invoke queues for a free per-bundle AND global slot. On expiry it fails `EXECUTOR_DEADLINE` with `detail = "pool_exhausted"` (the pinned wire crate has no dedicated code). |
| `EXECUTOR_MAX_CONCURRENT_COMPILES` | `2` | Concurrent JIT compiles. |
| `EXECUTOR_SHUTDOWN_DRAIN_SECONDS` | `25` | Drain deadline on SIGTERM. Keep below the pod's `terminationGracePeriodSeconds` (default 30) so rollouts never reach SIGKILL. On expiry the executor logs what was abandoned and still exits 0. |

All four counts must be `>= 1`; `validate()` rejects `0` at startup.

### What "pool" means here

Each digest is pre-linked once at load time (`Linker::instantiate_pre`), so an
invoke pays only for instantiation. A **fresh `Store` is created per call**:
a digest is shared across tenants, so reusing a live instance would leak guest
state between them. `EXECUTOR_INSTANCES_PER_BUNDLE` therefore bounds
concurrent instances rather than keeping warm ones.

### Fail-closed behavior that is unchanged

Platform signing-key enforcement (`BUNDLE_SIGNING_PUBLIC_KEYS`, required at
startup), digest verification, per-call epoch deadline and memory cap, and the
`wasi:sockets` denial are unchanged. Signal-handler install failure is a
startup error, not a silent loss of graceful shutdown.

### Tests

`cargo test` (unit): cap enforcement and queueing (`invoke::tests::*call_cap*`,
`*queues_for_a_slot*`, `*per_bundle_instance_cap*`), drain behavior
(`*drain*`), compile-off-reactor (`cold_load_compile_does_not_starve_the_async_runtime`),
and a real `SIGTERM` delivered to the test process (`lib::tests::a_real_sigterm_*`,
`shutdown_signal_*`).

## Connector exports (`src/connector.rs`)

`ConnectorHost` invokes a core `waddle:connector@1.0.0` component's `receiver` exports (`on-connect`, `on-frame`,
`on-heartbeat-due`, `on-disconnect`) under a fresh, fuel-/epoch-/memory-bounded `Store` per call, with typed
`ConnectorCallError`s for guest errors, traps, fuel exhaustion, deadline, memory cap and pool exhaustion. Knobs:
`EXECUTOR_CONNECTOR_FUEL_LIMIT`, `EXECUTOR_CONNECTOR_POOL_SIZE`. It is an in-process API for now: the host-API wire
(`penguin-libs` `ExportKind`) has no connector kinds yet. Design note: `docs/connector-host-capability.md`.
