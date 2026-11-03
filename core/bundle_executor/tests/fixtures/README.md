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

## No `superpenguin_roll.wasm` fixture here (by design)

`bundles/csharp/superpenguin-roll` (the first real C# app bundle built on
`sdk/waddle-sdk-cs`, not a spike) builds with the exact same toolchain as
`csping` above, but its compiled component measured **6,659,228 bytes
(6.35 MiB)** at build time -- above the informal size this repo has used so
far for a committed binary blob under this directory (`csping.wasm` at
4.36 MiB was already the largest; `hostile_fixture.wasm` is 96,489 bytes).
Rather than committing a second multi-megabyte NativeAOT-LLVM binary here,
`core/bundle_executor` has no
`tests/csharp_superpenguin_roll_integration.rs` -- build the component
locally instead (`make build-superpenguin-roll-bundle`, or
`bundles/csharp/superpenguin-roll/README.md`'s Build section) and load it
manually if an ad hoc executor integration check is needed. Its WIT
conformance (`hub_api/services/bundle_component_validator.py`) and business
logic (`bundles/csharp/superpenguin-roll/tests/`) are both otherwise fully
covered without a committed fixture.

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

## `reputation_fixture.wasm`

A real, compiled `waddle:bundle` **`world stage-next`** component
(`reputation-fixture-src/`, issue #726), used by
`tests/stage_next_reputation.rs` to prove a bundle can CALL the new
`reputation` host import through the executor's production `Linker` and
that the call reaches the stage as `capability = db`, `op =
"reputation.get" | "reputation.adjust"`. `process-stage.transform` branches
on `event.event-type` (`rep-get` / `rep-adjust`) and echoes the call's
outcome (`ok:<balance>` or a stable error tag) into `payload-json`. The
fixture's `wit/stage.wit` is a verbatim copy of the normative
`wit/waddle-bundle/stage.wit`; refresh it whenever that file changes.

### Regenerating

```bash
cd reputation-fixture-src
cargo component build --release
cp "${CARGO_TARGET_DIR:-target}/wasm32-wasip1/release/reputation_fixture.wasm" ../reputation_fixture.wasm
rm -f src/bindings.rs   # generated, not committed
```

Built with `cargo-component 0.21.1` / `wasm-tools 1.259.0` against
`wit-bindgen-rt 0.44.0` (same toolchain as `hostile_fixture.wasm`).

## `economy_fixture.wasm`

A real, compiled `waddle:bundle` **`world stage-next`** component
(`economy-fixture-src/`, issue #714), used by `tests/stage_next_economy.rs` to
prove a bundle can CALL every `economy` host import through the executor's
production `Linker` and that each call reaches the stage as `capability = db`,
`op = "economy.balance" | "economy.wager" | "economy.transfer" |
"economy.max_bet" | "economy.leaderboard"`. `process-stage.transform` branches
on `event.event-type` (`eco-balance` / `eco-wager` / `eco-transfer` /
`eco-max-bet` / `eco-board`) and echoes the call's outcome (`ok:<value>` or a
stable error tag) into `payload-json`. The fixture's `wit/stage.wit` is a
verbatim copy of the normative `wit/waddle-bundle/stage.wit`; refresh it
whenever that file changes. The Python counterpart is built fresh on every run
by `tests/stage_next_python_economy_e2e.rs` from `economy-py-bundle/`.

### Regenerating

```bash
cd economy-fixture-src
cargo component build --release
cp "${CARGO_TARGET_DIR:-target}/wasm32-wasip1/release/economy_fixture.wasm" ../economy_fixture.wasm
rm -f src/bindings.rs   # generated, not committed
```

Built with `cargo-component 0.21.1` / `wasm-tools 1.259.0` against
`wit-bindgen-rt 0.44.0` (same toolchain as `reputation_fixture.wasm`).

## `ai_fixture.wasm`

A real, compiled `waddle:bundle` **`world stage-next`** component
(`ai-fixture-src/`), used by `tests/stage_next_ai.rs` to prove a bundle can CALL
the `ai` host import (`ai.generate`) through the executor's production `Linker`
and that the call reaches the stage as `capability = db`, `op = "ai.generate"`
with exactly the four guest-controlled args (`prompt`, `max_tokens`,
`temperature`, `json`). `process-stage.transform` handles event type
`ai-generate` (payload `prompt` / optional `max_tokens` / `temperature_x100` /
`json`) and echoes the outcome (`ok:<text>` or a stable error tag) into
`payload-json`. The fixture's `wit/stage.wit` is a verbatim copy of the
normative `wit/waddle-bundle/stage.wit`; refresh it whenever that file changes.
The Python counterpart is built fresh on every run by
`tests/stage_next_python_ai_e2e.rs` from `ai-py-bundle/`.

### Regenerating

```bash
cd ai-fixture-src
cp ../../../../../wit/waddle-bundle/stage.wit wit/stage.wit
cargo component build --release
cp "${CARGO_TARGET_DIR:-target}/wasm32-wasip1/release/ai_fixture.wasm" ../ai_fixture.wasm
rm -f src/bindings.rs   # generated, not committed
```

Built with `cargo-component 0.21.1` / `wasm-tools 1.259.0` against
`wit-bindgen-rt 0.44.0` (same toolchain as `economy_fixture.wasm`).

## `echo_connector.wasm` and `faulty_connector.wasm`

Real, compiled `waddle:connector@1.0.0` components for the connector host capability (issue #840 B0; design note
`docs/connector-host-capability.md`), used by `tests/connector_invocation.rs` and by `core/connector_e2e`.

- `echo-connector-src/` -- the Echo reference/smoke connector: a faithful port of
  `core/svc_ingest/builtin_handlers/echo_ingest.py::normalize` (`source`/`text` required, trimmed `text`, default
  `event_type` `message` and actor `unknown`). Malformed input is `frame-error::malformed`, never a trap. The
  `sender` export is `frame-error::unsupported` (sender wiring is a later phase).
- `faulty-connector-src/` -- fault injection: `on-frame` bytes `ok` / `malformed` / `trap` / `spin` / `grow` /
  `flood:<N>` make it return an event, a frame-error, trap, loop forever, hog memory, or return N events.

Both use a trimmed `connector.wit` (receiver/sender/types byte-identical to the normative file; no reused
`waddle:bundle` imports and no `identity`, which neither calls), like `connector_fixture.wasm` above.

### Regenerating

```bash
cd echo-connector-src   # or faulty-connector-src
cargo component build --release
cp "${CARGO_TARGET_DIR:-target}/wasm32-wasip1/release/echo_connector.wasm" ../echo_connector.wasm
rm -f src/bindings.rs   # generated, not committed
```

Built with `cargo-component 0.21.1` / `wasm-tools 1.259.0` against `wit-bindgen-rt 0.44.0` (same toolchain as the
fixtures above); `serde`/`serde_json` are exact-pinned in `echo-connector-src/Cargo.toml`.

## `stage_v2_fixture.wasm`

A real `world stage-v2` component (`stage-v2-fixture-src/`, numeric-scope contract proof, `tests/stage_v2_scope.rs`): `transform`
event `scope` echoes `context-v2.get-context`'s numeric ids (or `no-scope` when `tenant-id == 0`), and `action-stage-v2.dispatch`
echoes the envelope's ids in `provider-message-id`. The `wit/stage.wit` copy is the committed `wit/waddle-bundle/stage.wit`.

```bash
cd stage-v2-fixture-src
cargo component build --release
cp "$CARGO_TARGET_DIR/wasm32-wasip1/release/stage_v2_fixture.wasm" ../stage_v2_fixture.wasm   # or target/wasm32-wasip1/...
rm -f src/bindings.rs   # generated, not committed
```

Built with `cargo-component` / `wit-bindgen-rt 0.44.0`, same toolchain as the fixtures above.

## `tool_provider_fixture.wasm` / `tool_caller_fixture.wasm`

Real `world stage-tools` components (bundle-MCP-tools P3b) driven by `core/tool_invoke_e2e` (the real-path
`tool.invoke` e2e: real executor + svc-process `handle_tool`). Both use the Rust SDK (`sdk/waddle-sdk-rs`
`ToolRegistry` / `invoke`) and adapt their OWN generated `stage-tools` bindings to its `ToolTransport` seam (the SDK's
`bindings_glue` only carries the frozen `stage` world; it is dead-stripped from these builds). `wit/` holds verbatim
copies of `wit/waddle-bundle/stage.wit` + `tool.wit`; refresh both whenever either changes.

- **provider** (`tool-provider-fixture-src/`, a service): `echo` (read), `whoami` (echoes the host-verified actor + scopes), `guarded` (WRITE; echoes the forwarded
  idempotency key), `relay` (nested `tool.invoke` -- cycle/depth/self-call probes) and `slow` (spins; only the deadline ends it).
- **caller** (`tool-caller-fixture-src/`, an app): `go` `{target, tool, args, key?}` forwards through the SDK client and returns
  the host's typed outcome. (`stage-tools` is the only world with the `tool` import, so the app also exports
  `tool-provider`; it is just the harness's entry point.)

```bash
cd tool-provider-fixture-src   # or tool-caller-fixture-src
cp ../../../../../wit/waddle-bundle/stage.wit ../../../../../wit/waddle-bundle/tool.wit wit/
cargo component build --release
cp "${CARGO_TARGET_DIR:-target}/wasm32-wasip1/release/tool_provider_fixture.wasm" ../tool_provider_fixture.wasm
rm -f src/bindings.rs   # generated, not committed
```

Built with `cargo-component 0.21.1` / `wasm-tools 1.259.0` against `wit-bindgen-rt 0.44.0`.
