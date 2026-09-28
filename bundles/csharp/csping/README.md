# csping (C#)

The C#-toolchain feasibility spike bundle (spec
`docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` S18 R13 /
D35). `!csping` -> `pong (c#)`, origin-routed exactly like
`bundles/rust/ping` and `bundles/python/pyping`: a minimal, real
process/action-stage bundle proving the `componentize-dotnet` toolchain
can build a conformant `waddle:bundle/stage@1.0.0` component that the real
executor (`core/bundle_executor`) loads and runs end to end
(`core/bundle_executor/tests/csharp_bundle_integration.rs`).

This is a spike artifact, not a Tier 1 SDK. There is no `waddle-sdk-dotnet`
package; the bundle is written directly against `wit-bindgen`'s generated
bindings, same relationship `bundles/rust/ping` has to hand-rolled
`wit_bindgen::generate!` output before `waddle-sdk-rs` existed.

## Toolchain versions (as built and verified)

| Component | Version |
|---|---|
| .NET SDK | `10.0.401` (`mcr.microsoft.com/dotnet/sdk:10.0.401-noble`) |
| `BytecodeAlliance.Componentize.DotNet.Wasm.SDK` | `0.8.0-preview00011` |
| `BytecodeAlliance.Componentize.DotNet.WitBindgen` (`wit-bindgen` C# backend) | `0.8.0-preview00011`, wraps `wit-bindgen` `0.58.0` |
| `Microsoft.DotNet.ILCompiler.LLVM` (NativeAOT-LLVM) | `10.0.0-rc.1.26306.1` |
| WASI SDK | `wasi-sdk-29.0` (pinned + sha256-verified in `Dockerfile`; see "Hermeticity" below) |
| `wasm-tools` (validation only, not part of the build) | `1.259.0` (repo-pinned, see `wit/waddle-bundle/stage_wit_test.sh`) |

Sources: `componentize-dotnet`'s own README and `dotnet new
componentize.wasi.lib` template
(github.com/bytecodealliance/componentize-dotnet), and its NuGet nuspec
(queried directly against `api.nuget.org`'s flat-container index — not
guessed). `Microsoft.DotNet.ILCompiler.LLVM` and its per-RID
`runtime.linux-x64.*` package are **not** on nuget.org (confirmed: a
flat-container query 404s) — they are published only to the
`dotnet-experimental` Azure DevOps feed, which `nuget.config` in this
directory adds.

## Build

Containerized, rootless end to end (`Dockerfile` in this directory,
context = repo root). Pass `--user "$(id -u):$(id -g)"` so the container
writes the output into your bind-mounted directory as *you*, not the
image's baked-in `builder` uid (10001) — whichever one doesn't match your
host directory's ownership fails the final `cp` with a permission error:

```bash
docker build -f bundles/csharp/csping/Dockerfile -t waddles/bundle-csping-build:spike .
mkdir -p /tmp/csping-out
docker run --rm --user "$(id -u):$(id -g)" -v /tmp/csping-out:/out waddles/bundle-csping-build:spike
```

Or use `make verify-csping-fixture` from the repo root, which does exactly
this and additionally diffs the result against the committed
`core/bundle_executor/tests/fixtures/csping.wasm.sha256` (see
"Reproducibility & auditability" below).

Or directly, from this directory (needs .NET 10+ SDK on `PATH`):

```bash
dotnet build -c Release
# -> bin/Release/net10.0/wasi-wasm/publish/csping.wasm
```

Validate:

```bash
wasm-tools component wit bin/Release/net10.0/wasi-wasm/publish/csping.wasm
wasm-tools validate --features component-model bin/Release/net10.0/wasi-wasm/publish/csping.wasm
```

## Spike results

**Exports** — exactly the two the `stage` world declares, neither more nor
fewer:

```
export waddle:bundle/process-stage@1.0.0;
export waddle:bundle/action-stage@1.0.0;
```

**Imports** — `waddle:bundle/relay@1.0.0` (the only host capability this
bundle actually calls) and the benign `waddle:bundle/types@1.0.0` (a
`use types.{...}` artifact, same as every other language), plus:

```
wasi:cli/environment@0.2.6      wasi:cli/exit@0.2.6
wasi:cli/stdin@0.2.6            wasi:cli/stdout@0.2.6
wasi:cli/stderr@0.2.6           wasi:cli/terminal-{input,output,stdin,stdout,stderr}@0.2.6
wasi:clocks/monotonic-clock@0.2.6   wasi:clocks/wall-clock@0.2.6
wasi:filesystem/types@0.2.6     wasi:filesystem/preopens@0.2.6
wasi:io/poll@0.2.6              wasi:io/error@0.2.6   wasi:io/streams@0.2.6
wasi:random/random@0.2.6
```

**No `wasi:sockets`, no `wasi:http`.** This fills in the spec's `TBD
pending spike` row for C# in the per-language import allowlist table: C#
needs the **full** `wasi:cli` (including all five `terminal-*`
sub-interfaces — broader than Rust's empty-args/env-only usage) plus
`wasi:clocks`, `wasi:filesystem`, `wasi:io`, `wasi:random` — every
namespace already in `hub_api/services/bundle_component_validator.py`'s
`ALLOWED_WASI_NAMESPACES` and every namespace `core/bundle_executor/src/
engine.rs::build_linker` already links via
`wasmtime_wasi::p2::add_to_linker_async`. **No hub-api or executor code
change was needed** — ran the real, unmodified `validate_component()`
against `csping.wasm` and it returned `ok=True` on the first try.

**Component size:** 4,361,157 bytes (4.36 MiB) — roughly 45x
`hostile_fixture.wasm`'s 96,489 bytes (a trivial Rust fixture, not a fair
comparison baseline) and, going by `bundles/rust/example`'s general order
of magnitude, likely 5-10x a comparable minimal Rust component. The
NativeAOT-LLVM-compiled BCL subset (even trimmed, self-contained,
`InvariantGlobalization`) dominates.

## Cold-load (compile) time

`core/bundle_executor/tests/csharp_bundle_integration.rs`'s `load` step —
`wasmtime::component::Component::new` compiling `csping.wasm` from bytes,
no precompiled `.cwasm` cache (not wired for ANY language yet,
`core/bundle_executor/src/invoke.rs`'s own module doc) — was first
measured at **~104 seconds** under `cargo test`'s default (debug) host
profile. That number is misleading and is **not** a production estimate:
`cargo test` leaves the executor's OWN code (including the `wasmtime`/
Cranelift crates that do the compiling) unoptimized, so it measures how
slowly an unoptimized Cranelift runs, not how slowly Cranelift compiles
this component. `core/bundle_executor`'s actual deployed binary is always
built `--release` (`Dockerfile.rust`'s `cargo build --release --locked`),
so the release-profile number below is the one that matters.

`core/bundle_executor/tests/component_compile_benchmark.rs` isolates just
the `Component::new` call (no wire-protocol/tokio overhead) and reports
both this bundle's fixture and an existing Rust fixture side by side,
built `--release`:

```bash
cargo test --release --test component_compile_benchmark -- --ignored --nocapture
```

| Fixture | Size | `Component::new`, debug profile (`cargo test`) | `Component::new`, release profile (`cargo test --release`) |
|---|---|---|---|
| `csping.wasm` (this bundle, C#/NativeAOT-LLVM) | 4,361,157 bytes | ~104,000ms | **4,939ms** |
| `hostile_fixture.wasm` (Rust, `cargo-component`) | 96,489 bytes | not separately measured | **271ms** |

Release-profile compile is **~21x faster** than the debug-profile number
this spike originally reported — the debug number overstated the real
cost by more than an order of magnitude, and the correction matters:
**~4.9 seconds is close to the spec's own ~3-4 second reference point for
a "large" Rust component** (spec SS7.2), not the 25-30x-worse outlier
the debug measurement implied. C# is still the slowest of the two
fixtures measured here (~18x `hostile_fixture.wasm`'s 271ms), consistent
with being ~45x larger by byte count — sub-linear, not proportional,
suggesting a meaningful fixed per-component compile overhead rather than
a per-byte one.

Once instantiated, execution itself is fast regardless of profile: the
first `transform` invoke (instantiation + actual call) took ~50ms;
`dispatch` was comparable. The cost is entirely front-loaded into the
one-time compile.

**Revised consequence:** at ~4.9s release-profile, C# is in the same
order of magnitude as Rust's own worst-case reference, not disqualified
outright — this spike's original "not viable at Tier 1" verdict was an
artifact of measuring under the wrong build profile and should be
retracted. It is still the slowest of the two fixtures measured and a
few seconds is still non-trivial on a hot `load` path, so the precompiled
`.cwasm` artifact caching spec SS7.2/SS7.6 already calls for (a TODO for
every language, not C#-specific) remains worth landing before any
language is trusted at scale — but C#'s compile cost alone is no longer
the blocking finding it first appeared to be. A larger, more realistic
bundle (this spike's `csping` is deliberately minimal) should be
re-measured before drawing a final conclusion either way.

## Other gaps and caveats

- **`componentize-dotnet` is explicitly experimental.** Its own README:
  "All the underlying technologies are under heavy development and are
  missing features." Not a production-hardened toolchain as of this
  spike — treat Tier 2/pending-spike status (spec D35) as still correct
  even after this spike closes R13; this closes the *feasibility*
  question, not the *maturity* one.
- **`Microsoft.DotNet.ILCompiler.LLVM` lives only on the `dotnet-experimental`
  feed**, not nuget.org. A production CI pipeline must add that feed
  (this directory's `nuget.config` does) and accept that a Microsoft-run
  preview feed, not the hardened nuget.org supply chain, is in the build
  path. No PRC/sanctioned-entity dependency found in the toolchain
  (Bytecode Alliance + Microsoft + dotnet Foundation only).
- **WASI SDK is pinned + sha256-verified in `Dockerfile`** (`WASI_SDK_VERSION`/
  `WASI_SDK_SHA256` build args), not left to NativeAOT-LLVM's own live,
  unverified first-use download (~120MB from
  `github.com/WebAssembly/wasi-sdk` GitHub Releases). Confirmed empirically
  that pre-seeding the exact cache directory the toolchain expects
  (`$HOME/.wasi-sdk/wasi-sdk-<version>`) makes it skip its own download
  entirely: a `docker build --network none`-equivalent run (pre-populated
  cache volume, no network) still built successfully. Its version tracks
  the pinned `Microsoft.DotNet.ILCompiler.LLVM` release — bumping that
  package requires rebuilding once with network access, reading the new
  WASI SDK version/URL out of the log, and re-pinning both Dockerfile
  `ARG`s against the newly observed release asset's sha256.
- **`--with-wit-results` was not used.** By default, `wit-bindgen`'s C#
  backend lowers a WIT `result<T, E>` export return type to "return `T`
  directly, throw `WitException<E>`/`WitException<T>` for `Err`" rather
  than an explicit result wrapper type. Both `ProcessStageExportsImpl.
  Transform` and `ActionStageExportsImpl.Dispatch` (and this bundle's own
  `Relay.Push` import call) follow that convention. A future
  `waddle-sdk-dotnet` would need to decide whether to keep that
  convention (idiomatic C#, but exception-flow for expected business
  errors) or force `--with-wit-results` for parity with Rust's `Result<T,
  E>`-native ergonomics.
- **`IlcExportUnmanagedEntrypoints=true` is required** for any world that
  passes strings/lists/records across the boundary (this one does,
  heavily) — omitting it fails the build with a `cabi_realloc` missing-
  export error, not a runtime failure. Any `CSharpBuilder` recipe must set
  this unconditionally for this world, not leave it to bundle authors to
  discover.
- **JSON handling avoided reflection entirely** (`System.Text.Json.Nodes.
  JsonNode`/`JsonObject`, not `JsonSerializer.Deserialize<T>()`)
  specifically to sidestep NativeAOT trimming/reflection restrictions
  without needing a source-generated `JsonSerializerContext`. A real
  `waddle-sdk-dotnet` handling arbitrary bundle-author payload shapes
  would need to mandate source-generated `JsonSerializerContext` (`System.
  Text.Json.Serialization.JsonSourceGenerationOptions`) for any payload
  POCOs, exactly as `general.md`/NativeAOT's own reflection-free
  requirement dictates — reflection-based `JsonSerializer` calls are
  either trimmed away (silent data loss) or throw at runtime depending on
  trimming warnings a bundle author is unlikely to notice at build time.

## What a `CSharpBuilder` (`core/bundle_compiler`) would need

`core/bundle_compiler/src/build/{rust,python,js}.rs` are all stubs
implementing `LanguageBuilder` (shell out to a pinned toolchain binary,
never a library call) that currently fail loudly with `CompileFailed`;
`builder_for()` in `core/bundle_compiler/src/build/mod.rs` doesn't even
have a `"csharp"` match arm yet (falls through to `no LanguageBuilder for
language "csharp"`). A real `CSharpBuilder` needs:

1. A `"csharp"` arm in `builder_for()`.
2. A pinned, containerized `dotnet build -c Release` invocation against
   this directory's exact `nuget.config`/package-version pins (never a
   host-machine SDK — same untrusted-build-process requirement every
   other `LanguageBuilder` has).
3. Unconditionally set `IlcExportUnmanagedEntrypoints=true` and the
   `RuntimeIdentifier=wasi-wasm`/`SelfContained=true`/`PublishTrimmed=true`
   property block this bundle's `.csproj` sets by hand — a bundle author
   should not need to know any of this.
4. Generate (or vendor) a `<Wit Include=".../stage.wit" World="stage" />`
   item pointing at the manifest's declared world — mechanically
   equivalent to what `bundle.yaml`'s `stages` map already declares.
5. Enforce the `dotnet-experimental` NuGet feed is present, pin/verify the
   WASI SDK download by version + sha256 exactly like this bundle's own
   `Dockerfile` (see "Hermeticity" above) rather than trusting
   NativeAOT-LLVM's live, unverified first-use fetch, and otherwise keep
   the untrusted `build` stage's zero-credentials/network posture
   (`core/bundle_compiler/README.md`) — NuGet restore itself is the one
   remaining network dependency, same category as `cargo`/`pip`
   restoring from their own registries for the other `LanguageBuilder`s.
6. Run the same `wasm-tools component wit`/`ALLOWED_WASI_NAMESPACES`
   check this spike ran by hand, as part of `run_build`'s post-build
   verification.

## Reproducibility & auditability

`make verify-csping-fixture` (`scripts/verify-csping-fixture.sh`)
rebuilds this bundle from source, in the pinned rootless `Dockerfile`,
and asserts the result is byte-identical to the committed
`core/bundle_executor/tests/fixtures/csping.wasm` — checked against a
recorded `csping.wasm.sha256` in that same directory, not just a visual
diff. Run it after any change to this directory's `.cs`/`.csproj`/
`Dockerfile`/`nuget.config`, and re-run
`sha256sum core/bundle_executor/tests/fixtures/csping.wasm > core/bundle_executor/tests/fixtures/csping.wasm.sha256`
plus re-copy the fixture if the change is intentional.

## Porting an existing C# bot codebase (e.g. a Twitch bot) into this model

A typical existing C# bot targets the full BCL on a normal .NET runtime
(console app or ASP.NET service), not a WASI-p2 component with a fixed,
capability-scoped host surface. Porting logic into a bundle means:

- **`async`/`Task` works fine** inside the guest — NativeAOT-LLVM supports
  the async/await state machine and `Task`/`ValueTask` normally; nothing
  about the component model forces synchronous code. What does NOT carry
  over is any I/O the `Task` was awaiting: there is no real thread pool
  timer/socket-completion source outside the WASI imports this world
  actually declares, so an existing bot's `await Task.Delay(...)`/
  background timers need to become `clock`/host-scheduled equivalents, not
  a bare `System.Threading.Timer`.
- **No `HttpClient`, no raw sockets, ever** — the `stage` world excludes
  `wasi:sockets` outright and does not import `wasi:http` either (this
  spike confirmed C# does not pull in `wasi:http` by default, and even if
  it tried to, the executor's `WasiCtx` denies real socket I/O natively,
  spec SS6.5). Any outbound call an existing bot makes with `HttpClient`,
  a Twitch/Discord SDK's own HTTP client, or a raw `TcpClient`/
  `WebSocket` must be rewritten against this world's guarded `http`
  import (`interface http` in `wit/waddle-bundle/stage.wit`) instead —
  same guarded-egress-only rule every language's bundle already follows.
  A bot's own chat-send calls become `relay.push`, exactly like this
  bundle's `Dispatch`, not a direct Twitch/Discord API call.
- **Reflection-based JSON (`System.Text.Json.JsonSerializer.
  Deserialize<T>()`/`Serialize<T>()` without a source-generated context,
  `Newtonsoft.Json`, dynamic `Reflection.Emit`-based DI containers) must
  be replaced.** NativeAOT's trimmer either strips unreferenced reflection
  metadata (silent data loss at runtime, not a build error) or the
  runtime throws `NotSupportedException` for a reflection path it can't
  satisfy. A bot's typical `services.AddSingleton<T>()`/attribute-routing/
  `JsonConvert.DeserializeObject(json)` patterns all rely on exactly the
  reflection this environment removes. This spike sidestepped the whole
  category by using `JsonNode`/`JsonObject` (reflection-free) instead of
  typed deserialization; a larger port should adopt
  `System.Text.Json.Serialization.JsonSerializerContext` source generation
  for every payload type it needs strongly typed, and drop any DI
  container relying on runtime type scanning.
- **No filesystem, no environment variables, no arbitrary config files.**
  The `stage` world has no read/write filesystem beyond a single empty
  `/scratch` preopen and no `wasi:cli/environment` values (spec: "no
  environment variables"). A bot's `appsettings.json`/`.env`-style config
  loading must become the WIT `context.get-context().config_json`
  3-tier-resolved config instead.
- **No persistent in-process state across invocations beyond what `kv`/
  `db` provide.** Each `load`d component instance is checked out per call
  (spec SS7.2); a bot's typical in-memory `Dictionary<string, ...>`
  caches, connection pools, or singleton service state do not survive
  between invocations the way they would in a long-running process — that
  state has to move to the `kv`/`db` host imports.
- **No arbitrary third-party NuGet packages assumed available at
  runtime** — every dependency must itself be NativeAOT-LLVM/wasi-wasm
  compatible (most Twitch/Discord client SDKs target the full BCL and use
  reflection, sockets, or `HttpClient` directly, so they are not directly
  usable inside a bundle; their protocol-level logic can be ported, their
  transport layer cannot).

None of this is C#-specific in spirit — it's the same "guarded host
imports only, no ambient I/O, no reflection-driven runtime magic" contract
`bundles/rust/ping`/`bundles/python/pyping` already live under. What's
C#-specific is that a typical existing .NET bot codebase leans on
reflection and ambient BCL I/O far more heavily by default than a typical
Rust or already-async-first Python service does, so the gap between
"existing bot" and "portable bundle logic" is larger for C# than for the
other two Tier 1 languages.
