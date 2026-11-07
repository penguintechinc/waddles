# Building a C# app bundle on the newer WIT worlds (`waddle-sdk-cs`)

For the SuperPenguin port wave. Reference bundle: `bundles/csharp/superpenguin-roll` (economy wager, identity, tool call, kv).
Older `stage`-world bundle (relay only): `bundles/csharp/csping`. SDK: `sdk/waddle-sdk-cs`.

## 1. Pick the world

| World | Use | Catalog `wit_world` (core rows only) |
|---|---|---|
| `stage` | relay/kv/db only (csping) | omit |
| `stage-next` | + economy, identity, reputation, overlay, ai, media, platform-table, object-storage; exports `streaming-lifecycle` | `stage-next` |
| **`stage-next-tools`** | `stage-next` + `tool.invoke` (call service bundles). **Default for every port.** | `stage-next-tools` |
| `stage-v2` | numeric scope (`context-v2`) | NOT accepted by `bundles/build_python_bundles.py` `VALID_WIT_WORLDS`; unusable for catalog rows today |
| `stage-tools` | service bundles (export `tool-provider`) | not for app bundles |

`stage-next-tools` has string scope (`tenant`/`community` slugs) and no scheduler. A bundle that does not declare an import in its
manifest still links; the host answers `denied(not_granted)` at call time.

## 2. Project layout (copy `bundles/csharp/superpenguin-roll`)

```
<Bundle>.csproj                 Wit item: <Wit Include="../../../wit/waddle-bundle" World="stage-next-tools" />   (DIRECTORY, not stage.wit: tool.wit is a sibling file)
nuget.config  Dockerfile        verbatim (change paths/assembly name)
WaddleHostAdapter.cs            copy verbatim; change only the namespace if you rename it. Implements IWaddleHostNext.
ProcessStageExportsImpl.cs      exact names/namespace StageNextToolsWorld.wit.Exports.waddle.bundle.v1_0_0 (point at your logic class)
ActionStageExportsImpl.cs       same namespace
StreamingLifecycleExportsImpl.cs copy verbatim (all hooks `unsupported-stage`); REQUIRED, the world exports it
<Logic>.cs <Dispatch>.cs        derive WaddleProcessStageNext / WaddleActionStage(Next); POCOs + IWaddleHostNext only
<Bundle>Json.cs                 your own [JsonSerializable] partial JsonSerializerContext (no reflection JSON, ever)
tests/                          xUnit on the host CLR; link logic files + sdk/waddle-sdk-cs/tests/Fakes.cs + FakesNext.cs
bundle.yaml hub-manifest.yaml   in sync (hub-manifest: artifact prebuilt/native, routes_to). Copy the MIT notice verbatim.
```

The wit-bindgen C# namespace is `<World>World` with the world name PascalCased: `StageNextToolsWorld`. If you move worlds, search/replace it.

## 3. SDK surface for the new world

| Namespace | Client | Needs permission | Stage |
|---|---|---|---|
| `Economy` | `IEconomyClient`: `Balance`, `Wager(user,stake,payout)`, `MaxBet`, `Transfer`, `Credit`, `BatchCredit`, `Debit`, `ConditionalDebit`, `PayPot`, `Leaderboard` | `economy.read` / `.wager` / `.transfer` / `.credit` / `.debit` / `.pot.pay` | process only |
| `Identity` | `ResolveActor`, `ResolveMention`; `UserPlaceholder.For(uuid)` -> `{user:uuid}` | `identity.resolve` | process only |
| `Tool` | `IToolClient.Invoke`; typed `Invoke<TArgs,TResult>`; `EconomyServiceTools.PointsView` (alias) | `tool.call:<target>:<tool>` (+ `requested_tools`) | process only |
| `Overlay` | `RegisterWidget`, `Push` (errors -> `WaddleTransportException`) | `overlay.media` | process + action |
| `Ai` | `Generate(prompt, AiOptions)` | `ai.generate` + flag `waddles.bundle-ai-capability` (not production-ready, #858) | process |
| `Media` | `GenerateImage/SubmitMusic/SubmitVideo/JobStatus/ObjectUrl` | `media.generate` (+ Pro tier, BYOK) | process |
| `State` | `IStateStore` (`KvStateStore`, `DbStateStore`), `StateKey`, `StateCooldown` | `storage.kv` / `data.tables` | any |
| `Scheduling` | `TickScheduler` (tick-on-event emulation) | store's permission | any |

Not wrapped (add when a port needs them): `economy.admin-adjust`/`exchange`, `reputation`, `platform-table`, `object-storage`.
`ai.complete` is a planned SERVICE tool (catalog spec); call it with the generic `IToolClient` once the `ai` service bundle exists and
pin its JSON schema in a contract-fixture test. Today use WIT `Ai.Generate`.

### Money rules every port must follow
- Exactly ONE host economy call per logical settlement; the host de-duplicates per (event, call ordinal), so a redelivery that replays the same calls moves nothing.
- Anything random is rolled ONCE and recorded (kv, keyed by `StateKey.Hash(context.MessageId)`) BEFORE the money call and reused on redelivery (see `RollLogic`), or the reply will not match the ledger.
- `actor`-only debit: `Wager`/`Debit`/`Transfer(from)` must name the invocation's actor (`identity.ResolveActor()`); victim-debit games (steal) need a redesign (pot/fine).
- Handle `InsufficientFunds`/`OverCap` with a chat reply; let `Denied`/`Unavailable`/`Backend`/`NotAMember` propagate (fail loud, never an "unpaid" reply).
- Currency name = economy service alias (`EconomyServiceTools.PointsView`); fall back to `DefaultAlias` only with a logged warning. Names of people = `UserPlaceholder.For(uuid)` (host renders the display name at egress; no name lookup exists or is needed).
- Logs: ids/op/outcome/counts only. State keys: `[A-Za-z0-9_.-]`, <=256 bytes, hash identifiers (`StateKey`).

## 4. State, cooldowns, scheduling

**kv is live on release** (dedicated Valkey, `bundle_host_kv`; requires `storage.kv` in the manifest). Constraints: key charset has NO `:`; 64 KiB values;
10k keys/app; 64 ops/invoke; `increment` refreshes the TTL on every call; no compare-and-swap. The old `CooldownGuard` used `:` keys (broken against the live host) and now uses `.`
keys; prefer `StateCooldown` (works over any store, claims atomically via `increment`, fails loud when the store is unavailable).

**db** (`DbStateStore`): needs `data.tables` and flag `waddles.bundle-db-capability` (default OFF); declared columns `k` text, `v` bytes, `exp` int. The db interface has NO column
filter, so lookups scan (<=200 rows/page, cap 1000): use for small shared state (jackpot pot, join windows, live duels). `Mutate` is a real compare-and-swap on the row version (use it for pots).
Use `IStateStore` in game code so either store works.

**Scheduling.** No WIT world has a scheduler for app bundles. `TickScheduler` emulates one: persist tasks (`Schedule(id, dueAtMillis, kind, payload)`), call `TakeDue()` at the top of every
`transform`. Limitations to document in each bundle: (1) a deadline only resolves when ANOTHER event reaches the bundle, and a bundle only receives events matching its own `consumes` filters (its command prefixes);
(2) so a join window nobody touches again settles late - use `LateByMillis` to void stale windows; (3) one board <= 64 tasks; (4) exactly-once across racing events on db (CAS), best-effort+claim on kv.

## 5. Build and verify

```bash
make test-waddle-sdk-cs            # SDK unit tests + 90% line-coverage floor (139 tests, 98%)
make test-superpenguin-roll        # bundle unit tests + floor (54 tests, 99.5%)
make build-superpenguin-roll-bundle   # pinned Dockerfile -> /tmp/superpenguin-roll-out/superpenguin_roll.wasm
make test-superpenguin-roll-e2e    # builds, loads in the REAL bundle-executor, drives transform through a simulated stage
wasm-tools component wit <wasm>    # exports must be process-stage, action-stage, streaming-lifecycle
```

Toolchain is pinned (NativeAOT-LLVM `Microsoft.DotNet.ILCompiler.LLVM` 10.0.0-rc.1.26306.1 from the `dotnet-experimental` feed only; WASI SDK 29.0 sha256-pinned in the Dockerfile; .NET SDK 10.0.401 digest-pinned).
Do not bump without re-verifying every C# bundle. Measured (roll, stage-next-tools): wasm 7,315,136 bytes (csping 6.6 MB), full `docker build --no-cache` ~148 s (of which `dotnet build` ~65 s).

**Limits.** Per invocation 2 s / 64 MB (`limits:` in the manifest). Cold instantiation of a ~7 MB NativeAOT component is the dominant cost (several seconds on first load per replica, one-time; csping README measured ~5 s).
Risks for large ports (fishing 5k+ LOC, starship 2.4k): size grows with code and with System.Text.Json source-gen contexts; per-invocation state must come from kv/db (no in-memory state between events); keep per-event host calls under the kv 64-op cap;
split a big game into several bundles (core/shop/tournament) rather than one component.

## 6. Needs from S0 (host seams; do not work around)

| Need | Why | Today |
|---|---|---|
| `scheduled-stage` export in `stage-next` (+ mirrored in `stage-next-tools`) and a host timer | real deadlines for heist/ffa/racing/duel expiry/fishing tournaments | `TickScheduler` emulation (late settlement) |
| `economy` + `identity` + `tool` host calls in ACTION stage (or an overlay-from-process path) | settle after a delay, push animation after payout | process stage only; action only relays |
| `stage-v2` (numeric scope) accepted by catalog/builder + `stage-v2-tools` world (stage-v2 has no `tool`) | numeric-id data-plane keys for tool-calling apps | catalog rejects it; `stage-next-tools` uses string scope |
| stream-online / moderation capabilities | first, bonus-tickets, roulette timeout | not in any world |
| victim-debit or `transfer` with arbitrary `from` (admin/pot path) | steal | actor-only |
| Vendor-onboarding validator allows `streaming-lifecycle` export | non-core C# bundles on stage-next worlds | `hub_api/services/bundle_component_validator.py` `REQUIRED_EXPORTS` rejects it (core seeder path does not validate) |
| Decision on catalog placement of third-party ports | roll (and every port) is `waddles.integrations.*`; `seed_core_bundles._guard_core_namespace` hard-refuses non-`waddles.core.*` rows, so NO catalog row was added | vendor path only (blocked by the validator row above) |
