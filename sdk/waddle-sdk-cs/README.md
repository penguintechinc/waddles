# waddle-sdk-cs

See `AUTHORING.md` for the condensed per-bundle checklist (SuperPenguin C#
port batch) -- this file has the full rationale and code samples.

Tier-1(spike) C# SDK for Waddles app bundles: typed, reflection-free
(NativeAOT-LLVM/`wasi-wasm`-safe) helpers over the `waddle:bundle/stage@1.0.0`
WIT world (`wit/waddle-bundle/stage.wit`). Model: `sdk/waddle-sdk-rs` (a
path-dependency SDK, no registry publish) and `sdk/waddle-sdk` (Python).
Referenced by C# bundles as an in-repo project reference, never a NuGet
package -- this SDK and the bundles built on it move together in this repo.

## Why this SDK never touches wit-bindgen's generated types directly

`wit-bindgen`'s C# backend (`BytecodeAlliance.Componentize.DotNet.WitBindgen`,
the same one `bundles/csharp/csping` uses) regenerates a **fresh, nominally
distinct copy** of every WIT record/interface type in **every project** that
has a `<Wit Include="..." World="stage" />` item. This is fundamentally
different from Rust: `waddle-sdk-rs`'s single `wit_bindgen::generate!` call
(in `sdk/waddle-sdk-rs/src/bindings_glue.rs`) produces types that are
re-exported with full type identity to every downstream bundle crate, because
Cargo resolves one macro expansion across the whole dependency graph. C#'s
type system is nominal, not structural: if this SDK library *also* ran the
WIT source generator, its `Types.PlatformEvent`/`IKvImports`/etc. would be a
**different CLR type** from the ones a consuming bundle project generates for
itself -- even though both come from the byte-identical `.wit` file -- and
every SDK method signature touching a WIT type would fail to compile in the
bundle project.

The fix: this SDK's entire public surface operates only on

- its own idiomatic POCOs (the `WaddleSdk.Types`/`WaddleSdk.Chat`/
  `WaddleSdk.Db`/etc. namespaces), and
- plain BCL primitives (`string`, `byte[]`, `long`, `bool`, ...),

**never** a wit-bindgen-generated type. That makes every method here usable,
unmodified, from any bundle project regardless of that project's own WIT
codegen. The cost, compared to Rust, is that each bundle needs one small
per-project adapter (see below) doing the mechanical WIT-type <-> SDK-type
conversion that Rust's shared crate gets for free. `waddle-sdk-cs.csproj`
deliberately has **no** `<Wit Include=... />` item -- this is not an
oversight.

## SDK surface

| Namespace | What it gives you |
|---|---|
| `WaddleSdk.Chat` | `ChatCommand.TryParse(payloadJson, prefix)` (prefix/command/args parsing), `ChatMessagePayload`/`ChatReplyPayload`/`RelayMessagePayload` POCOs |
| `WaddleSdk.Json` | `WaddleSdkJsonContext`, a source-generated `JsonSerializerContext` for the SDK's own payload POCOs -- never reflection-based `JsonSerializer` |
| `WaddleSdk.Kv` | `IKvClient`, `TypedKv.GetJson`/`SetJson` (typed get/set via a source-gen `JsonTypeInfo<T>`), `WaddleKvException`/`KvErrorKind` |
| `WaddleSdk.Cooldown` | `CooldownGuard.TryAcquire` -- per-user command cooldowns over `kv`, degrades gracefully on `KvErrorKind.Denied` |
| `WaddleSdk.Db` | `IDbClient`, `DbValue`/`DbRows` (parameterized `execute`, never string concatenation), `WaddleDbException`/`DbErrorKind` |
| `WaddleSdk.Relay` | `IRelayClient`, `ReplyHelper.SendReply` (builds `{channel, text}` and pushes), `WaddleRelayException`/`RelayErrorKind` |
| `WaddleSdk.Http` | `IHttpClient`, `HttpRequestInfo`/`HttpResponseInfo`, `WaddleHttpException`/`HttpErrorKind` |
| `WaddleSdk.Flags` | `IFlagsClient` (PostHog flag + license tier, fail-open) |
| `WaddleSdk.Log` | `ILogClient` + `.Error`/`.Warn`/`.Info`/`.Debug` extensions, `LogFields` (reflection-free field-object builder over `Utf8JsonWriter`) |
| `WaddleSdk.Clock` | `IClockClient` |
| `WaddleSdk.Random` | `WaddleRandom` -- wraps `System.Random.Shared`, which is already WASI-random-backed on `wasi-wasm` (see its doc comment) |
| `WaddleSdk.Context` | `BundleContextInfo` |
| `WaddleSdk.Types` | `PlatformEventInfo`, `StageEnvelopeInfo`, `TransportResultInfo`/`TransportErrorInfo`, `WaddleTransportException` |
| `WaddleSdk.Stage` | `WaddleProcessStage`/`WaddleActionStage` base classes, `IWaddleHost` (aggregates every capability interface above) |

## kv/db availability (2026-09-28)

The host `kv` and `db` capabilities are **currently hardcoded to deny every
call** in svc-process/svc-action while the real backends are implemented
(`db`'s design is still in progress). Both `WaddleKvException`/`KvErrorKind`
and `WaddleDbException`/`DbErrorKind` carry a `Denied` case so callers can
distinguish "not available yet" from a genuine backend failure:

- `db`'s WIT `error` variant already has a native `denied(string)` case
  (`wit/waddle-bundle/stage.wit`), so a bundle's adapter maps it straight to
  `DbErrorKind.Denied`.
- `kv`'s WIT `error` variant does **not** have a native `denied` case (only
  `too-large`/`backend`) -- the current host-side denial surfaces as a
  `backend` error. `WaddleHostAdapter`'s convention (see
  `bundles/csharp/superpenguin-roll/WaddleHostAdapter.cs`) is to map a
  `backend` error whose message contains "denied" onto `KvErrorKind.Denied`.
  This is a narrow, documented, removable heuristic -- revisit it once the
  real `kv` backend lands with its own contract.

**`CooldownGuard.TryAcquire` degrades gracefully on `KvErrorKind.Denied`** --
a cooldown is a best-effort convenience, not a correctness requirement, so it
returns `true` (cooldown unenforced, command proceeds) rather than throwing.
Any other `kv` error (`TooLarge`/`Backend`) is a genuine failure and is
**not** swallowed. If you call `IKvClient`/`IDbClient` directly (not through
`CooldownGuard`), decide your own bundle's degrade-vs-fail policy per call --
don't assume every `kv`/`db` call should silently no-op on denial.

Every SDK capability interface, including `IKvClient`/`IDbClient`, is
covered by unit tests run against a mocked/denying host fake (see
`tests/Fakes.cs`'s `DenyingKvClient`/`DenyingDbClient`/`FaultyKvClient` and
`tests/TypedKvAndCooldownTests.cs`) -- exercise the same pattern in your own
bundle's tests before assuming a capability behaves a particular way.

## Writing a bundle

A C# bundle has five kinds of files. Only the first two are yours to write
business logic in; the rest are small, mechanical, and mostly copy-paste
from `bundles/csharp/superpenguin-roll`.

### 1. Business logic: derive from `WaddleProcessStage`/`WaddleActionStage`

```csharp
using WaddleSdk.Chat;
using WaddleSdk.Cooldown;
using WaddleSdk.Json;
using WaddleSdk.Stage;
using WaddleSdk.Types;

public sealed class MyLogic : WaddleProcessStage
{
    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHost host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null || !command.Is("mycommand")) return null;

        if (!CooldownGuard.TryAcquire(host.Kv, command.Name, @event.Actor ?? "anonymous", cooldownSeconds: 30))
        {
            return null; // on cooldown -- silently drop, exactly like the host `stage` contract expects
        }

        return PlatformEventInfo.WithPayload(@event, new ChatReplyPayload("hi!", command.ChannelId),
            WaddleSdkJsonContext.Default.ChatReplyPayload);
    }
}
```

Write against `WaddleSdk.Types`/`WaddleSdk.Chat` POCOs and `IWaddleHost`
only -- never a wit-bindgen-generated type. This class is fully unit
testable on the host CLR with plain fakes (see
`bundles/csharp/superpenguin-roll/tests/RollLogicTests.cs`).

### 2. The per-project host adapter (`WaddleHostAdapter.cs`)

wit-bindgen's C# codegen is per-project, so the SDK cannot implement
`IWaddleHost` itself (see "Why this SDK never touches wit-bindgen's
generated types directly" above). Copy
`bundles/csharp/superpenguin-roll/WaddleHostAdapter.cs` into your bundle
unmodified -- it delegates every `IWaddleHost` member to
`StageWorld.wit.Imports.waddle.bundle.v1_0_0.I*Imports`, the bindings
your own `<Wit Include=... />` item generates. Only touch it if
wit-bindgen's generated shape changes (e.g. an SDK/toolchain version bump).

### 3. The two thin WIT export shims

`wit-bindgen c-sharp` requires two EXACT type names --
`ProcessStageExportsImpl` implementing `IProcessStageExports` and
`ActionStageExportsImpl` implementing `IActionStageExports` -- in the EXACT
namespace `StageWorld.wit.Exports.waddle.bundle.v1_0_0` (discovered
empirically building `bundles/csharp/csping`, documented in that bundle's
`ProcessStageExportsImpl.cs`). These convert the wit-bindgen `Types.*`
records to/from this SDK's POCOs and delegate to your logic class -- see
`bundles/csharp/superpenguin-roll/ProcessStageExportsImpl.cs`/
`ActionStageExportsImpl.cs` for the exact ~20-line shape to copy and adjust
(only the logic-class name and namespace change).

### 4. The `.csproj`

Copy `bundles/csharp/superpenguin-roll/superpenguin-roll.csproj` (or
`bundles/csharp/csping/csping.csproj`). Three things every C# bundle
`.csproj` needs that are easy to miss:

- `<IlcExportUnmanagedEntrypoints>true</IlcExportUnmanagedEntrypoints>` --
  required whenever the world's imports/exports carry strings (this one
  always does); omitting it fails the build with a `cabi_realloc`
  missing-export error, not a runtime failure.
- `<Compile Remove="tests/**/*.cs" />` -- SDK-style projects glob every
  `*.cs` file under the project directory by default, including a sibling
  `tests/` project's own sources. Without this, your bundle's main library
  tries to compile the xUnit test files too and fails with `FactAttribute`
  "not found" errors (the main library has no xUnit package reference).
- `<ProjectReference Include="../../../sdk/waddle-sdk-cs/waddle-sdk-cs.csproj" />`
  -- a path reference, never a NuGet package.

### 5. `nuget.config`

Copy `bundles/csharp/superpenguin-roll/nuget.config` verbatim -- the
`dotnet-experimental` feed is required because `Microsoft.DotNet.
ILCompiler.LLVM` (NativeAOT-LLVM) is not published to nuget.org.

### Testing your bundle

Your logic class (step 1) has zero WASI-only surface, so test it in a
**separate**, normal host-targeted xUnit project that links your logic
`.cs` files directly (`<Compile Include="../MyLogic.cs" />`) rather than
`ProjectReference`-ing your bundle's own `.csproj` (which pins
`RuntimeIdentifier=wasi-wasm` and cannot run as a `dotnet test` host
target). See `bundles/csharp/superpenguin-roll/tests/
SuperpenguinRoll.Tests.csproj` for the exact shape.

## Building the component

```bash
cd bundles/csharp/<your-bundle>
dotnet build -c Release
# -> bin/Release/net10.0/wasi-wasm/publish/<assembly-name>.wasm
```

Or containerized (matches CI, no host .NET SDK needed): copy
`bundles/csharp/superpenguin-roll/Dockerfile`, adjusting only the bundle
path and output assembly name.

Verify:

```bash
wasm-tools component wit bin/Release/net10.0/wasi-wasm/publish/<assembly-name>.wasm
wasm-tools validate --features component-model bin/Release/net10.0/wasi-wasm/publish/<assembly-name>.wasm
```

## Porting more PenguinTwitchBot (superpenguin) bundles

`bundles/csharp/superpenguin-roll` is the first of many planned ports from
[PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) (MIT,
used with permission). superpenguintv and Psychoboy are the same person
(confirmed) -- every subsequent `bundles/csharp/superpenguin-*` bundle's
`bundle.yaml` must use this exact `author` string and `notice` text (copied
verbatim from upstream's own `LICENSE` file, not paraphrased), so the
marketplace metadata stays consistent across the whole series:

```yaml
author: "superpenguintv (Psychoboy)"
license: MIT
category: alternatives
source_url: https://github.com/Psychoboy/PenguinTwitchBot
notice: |
  Ported from PenguinTwitchBot (<source file path>, github.com/Psychoboy/
  PenguinTwitchBot) by superpenguintv (Psychoboy), used with permission.

  MIT License

  Copyright (c) 2023 Psychoboy

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to
  deal in the Software without restriction, including without limitation the
  rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
  sell copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in
  all copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
  FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
  DEALINGS IN THE SOFTWARE.
```

Only the `<source file path>` in the first `notice` line changes per bundle
(e.g. `PastyGames/Roll.cs`) -- the copyright holder is always "Psychoboy"
(their upstream `LICENSE` file's exact wording), never "superpenguintv",
even though `author` credits both names. See
`bundles/csharp/superpenguin-roll/bundle.yaml` for the reference copy.

## Network access

Bundles should never see or target private IP addresses. Use public FQDNs (`net.http.fqdn:<host>`), which is the preferred form. Public IPs (`net.http.public-ip`) are **HIGH risk**. `net.http.private-ip` is **HIGH risk, DENIED instance-wide by default** — a global admin must opt in, it exists only for exceptional self-hosted/on-prem cases, requires separate explicit approval, and is never available for the platform's own cluster networks. Reviewers and admins should treat any private-ip request as a red flag.

## Testing this SDK

```bash
make test-waddle-sdk-cs
# or directly:
bash scripts/test-waddle-sdk-cs.sh
```

Runs `dotnet test` with coverlet coverage collection inside the same pinned
`mcr.microsoft.com/dotnet/sdk:10.0.401-noble` image every other C# build in
this repo uses (never a host-machine SDK, `rules/client.md` Build &
Distribution). Coverage gate: 90%+ (`rules/critical-rules.md` Coverage).
