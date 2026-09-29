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
reaching 64 MiB. `busy-loop` is the CPU-bound counterpart: an unbounded
guest loop with no WIT import and no natural termination, proving the
epoch-deadline mechanism (`Store::set_epoch_deadline`/`epoch_deadline_trap`)
actually bounds guest CPU time -- without it this call would hang forever.

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

## `csping.wasm`

The C#-toolchain feasibility spike's fixture (spec S18 R13 / D35): the real
`bundles/csharp/csping` bundle's published component, built with
`componentize-dotnet` (`BytecodeAlliance.Componentize.DotNet.Wasm.SDK`
0.8.0-preview00011, NativeAOT-LLVM backend) against the same committed
`wit/waddle-bundle/stage.wit` world -- unlike `hostile_fixture.wasm` above
(a throwaway adversarial test double), this is the actual bundle source
under `bundles/csharp/csping`, copied here verbatim so
`tests/csharp_bundle_integration.rs` can load a real, non-Rust, non-Python
component through the executor's genuine wasmtime `Engine`/`Linker`
without a build step in the test itself (the same reasoning
`hostile_fixture.wasm` documents above, applied to a third language).

### Regenerating

```bash
docker run --rm \
  -v "$(git rev-parse --show-toplevel):/repo" -w /repo/bundles/csharp/csping \
  mcr.microsoft.com/dotnet/sdk:10.0.401-noble@sha256:35d40304542c8689331f8cab17c65926cdf48fe711e289321d71924b230a7d29 \
  dotnet build -c Release
cp ../../../bundles/csharp/csping/bin/Release/net10.0/wasi-wasm/publish/csping.wasm csping.wasm
```

Built and verified with `componentize-dotnet 0.8.0-preview00011` / .NET 10
SDK (`10.0.401`) / `wit-bindgen` `0.58.0` (vendored by
`BytecodeAlliance.Componentize.DotNet.WitBindgen` 0.8.0-preview00011) /
`Microsoft.DotNet.ILCompiler.LLVM` `10.0.0-rc.1.26306.1`. See
`bundles/csharp/csping/README.md` for the full toolchain writeup.

## `connector_fixture.wasm`

A real, compiled `waddle:connector@1.0.0` component (`connector-fixture-
src/`), used by `tests/linker_isolation.rs` to prove the per-component
`Linker`'s `identity.lookup` gate (spec `docs/superpowers/specs/2026-09-28-
connector-bundles.md` S3.2.1). `receiver.on-connect` calls
`identity.lookup` unconditionally, so this component's compiled import set
genuinely requires it to be linked -- instantiation succeeds only against a
`Linker` built for a manifest passing
`crate::manifest::VerifiedManifest::may_link_identity`, and fails
(wasmtime's standard "unknown import" error, before any guest code runs)
against one that doesn't.

The fixture's own `wit/connector.wit` is a trimmed, self-contained copy of
the normative `wit/waddle-connector/connector.wit` (`identity`/`receiver`/
`sender` byte-for-byte identical; `http`/`log`/`clock`/`%flags` omitted
since this fixture never calls them) -- avoiding this standalone
`cargo-component` package needing its own cross-package `waddle:bundle`
dependency resolution, which `cargo-component 0.21.1`'s `deps/` merge did
not accept in the same layout `wasm-tools component wit` itself resolves
correctly (worth revisiting if a future fixture needs the reused imports
too).

### Regenerating

```bash
rustup target add wasm32-wasip1
cargo install cargo-component --locked
cd connector-fixture-src
cargo component build --release
cp target/wasm32-wasip1/release/connector_fixture.wasm ../connector_fixture.wasm
```

Built and verified with `cargo-component 0.21.1` / `wasm-tools 1.259.0`
against `wit-bindgen-rt 0.44.0` (same toolchain as `hostile_fixture.wasm`
above).
