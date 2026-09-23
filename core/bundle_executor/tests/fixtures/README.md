# `hostile_fixture.wasm`

A real, compiled WASI 0.2 component built against the committed
`wit/waddle-bundle/stage.wit` world (`waddle:bundle/stage@1.0.0`), used by
the integration tests in this crate to prove the executor's wasmtime
instantiation + host-call bridging against genuine compiled WASM rather
than a hand-rolled stand-in (spec `docs/superpowers/specs/2026-09-14-rust-
data-plane-design.md` SS4.5/SS7, task instruction "never fake a host
call").

`process-stage.transform` branches on `event.event-type` and calls one WIT
import per branch (`get-context`, `kv-roundtrip`, `db-roundtrip`,
`log-write`, `clock-read`), echoing the result into `payload-json` so a
test can assert on it. `action-stage.dispatch` exercises `flags`/`relay`.
`memory-hog` allocates and touches linear memory in 1 MiB steps (up to 64
MiB) with no WIT import involved at all -- the negative test for the
per-instance memory cap (`crate::invoke`'s `Store::limiter`/`StoreLimits`
wiring, spec SS7.3 sandbox layer 8): loaded with a small
`limits.memory_mb`, this must trap with `MEMORY_LIMIT` well before
reaching 64 MiB.

## Regenerating

Source lives in `hostile-fixture-src/` alongside a copy of the WIT world
it was built against. To rebuild after a WIT change:

```bash
rustup target add wasm32-wasip2   # or wasm32-wasip1 + cargo-component's adapter
cargo install cargo-component --locked
cd hostile-fixture-src
cargo component build --release
cp target/wasm32-wasip1/release/hostile_fixture.wasm ../hostile_fixture.wasm
```

Built and verified with `cargo-component 0.21.1` / `wasm-tools 1.259.0`
against `wit-bindgen-rt 0.44.0`.
