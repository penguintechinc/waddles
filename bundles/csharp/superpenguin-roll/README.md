# superpenguin-roll (C#)

`!roll`/`!dice` dice game, ported from
[PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)'s
`PastyGames/Roll.cs` by superpenguintv (Psychoboy), MIT-licensed, used with
permission -- see `bundle.yaml`'s `notice`. The first C# app bundle built on
`sdk/waddle-sdk-cs`, listed as a Waddles marketplace **alternative**
(`category: alternatives`) rather than a Waddles-native feature.

## Behavior

Both `!roll` and `!dice` roll two six-sided dice. Doubles win a
prize (name + amount, faithful to the original's table); anything else loses.
Win/lose flavor text is picked at random from the original's own message
pools (~20 win, ~62 lose), verbatim. Each command name has its own
independent 180s per-user cooldown, exactly like the original's
`RegisterDefaultCommand(..., userCooldown: 180, ...)` per command.

Two faithful-but-adapted simplifications (see `RollLogic.cs`'s doc comment
for the full rationale): the caller's opaque `actor` id substitutes for the
original's Twitch/Discord display name (no display-name lookup capability
exists in this world yet), and the prize is announced generically as
"points" rather than a per-streamer-configured point-type name (no
cross-bundle points ledger exists yet).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!roll` / `!dice` | anyone | Rolls two six-sided dice for the caller. Doubles win a prize (name + amount from the original's table, boxcars on double sixes); any other roll loses. Win/lose flavor text is picked from the original's verbatim message pools. Each alias has its own independent 180 s per-user cooldown; a command received inside the window is **silently dropped** exactly like the original (no reply, no error). |

```text
viewer> !roll
bot>    <actor> rolls a [3] and [3]. <prize name> for <amount> points! <win flavor text>
viewer> !roll
         (no reply -- inside the 180 s cooldown, matching the original)
```

(Exact prize names/amounts and flavor strings are the original `Roll.cs` tables, kept verbatim in
`RollLogic.cs`; `!roll` here is distinct from the first-party Python `!roll NdM` bundle -- see
"Feature flag".)

## Economy and permissions (world `stage-next-tools`)

Stakes and pays through the host `economy.wager` (one atomic call per roll), names the player with the host
`{user:uuid}` placeholder, reads the community currency alias via the economy service's `points_view` tool.
`!roll [stake|all]` (default stake 10); doubles pay Snake eyes 2x, Hard four 3x, Hard six 4x, Hard eight 6x, Hard ten 8x,
Boxcars 10x (expected return 33/36 of the stake). Deviation from the original (fixed prizes, no stake) is documented in `RollLogic.cs`.

| Permission | Why |
|---|---|
| `storage.kv` | per-user cooldown (180 s, per command) + per-event outcome record (idempotent redelivery) |
| `economy.wager` (`max_bet: 500`) | `wager` and `max-bet` |
| `identity.resolve` | actor -> community user uuid |
| `tool.call:waddles.core.service.economy:points_view` | currency alias for display |
| `chat.send:twitch` / `chat.send:discord` | relay the reply |

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!roll", "!dice"]`; the
reply is relayed to `envelope.event.platform` (never a hardcoded provider -- regression-tested for
both).

## Logging / PII

The bundle writes **no log lines**. The public reply names the caller by the opaque `actor` id the
event carries (a documented simplification of the original's display name, going back to the same
channel it came from); only `{channel, text}` cross the relay boundary -- the actor id is not
relayed separately (`the_relayed_message_carries_only_the_channel_and_reply_text`).

## Failure semantics (fail loud)

| Condition | Behavior |
|---|---|
| Missing `channel_id` on the reply | fatal `MISSING_CHANNEL` (`WaddleTransportException`) |
| Reply payload is not a roll reply (e.g. a JSON array) | fatal `BAD_PAYLOAD` |
| Relay host failure (`Backend` or `Denied`) | retryable `RELAY_PUSH_FAILED` -- never swallowed into a success |
| `kv` denied (today's host) | the roll still replies; only the cooldown goes unenforced |

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, marketplace metadata (`author`/`license`/`category`/`source_url`/`notice`) |
| `RollLogic.cs` | The game logic (`WaddleProcessStage`) -- unit tested directly, no WIT types involved |
| `RollDispatch.cs` | Relays the reply (`WaddleActionStage`) |
| `WaddleHostAdapter.cs` | Per-project `IWaddleHost` implementation over the real WIT imports -- see `sdk/waddle-sdk-cs/README.md` for why this can't live in the SDK |
| `ProcessStageExportsImpl.cs` / `ActionStageExportsImpl.cs` | Thin WIT export shims wit-bindgen's naming contract requires |
| `superpenguin-roll.csproj` / `nuget.config` / `Dockerfile` | Build (mirrors `bundles/csharp/csping`) |
| `tests/` | xUnit tests for `RollLogic`/`RollDispatch`, on the host CLR (see that project's own header comment for why it isn't a `ProjectReference` to the bundle itself) |

## kv

`kv` is live on the host (dedicated Valkey; needs `storage.kv`). Keys use `.` separators and hashed ids (the host rejects `:`).
A `kv` failure is NOT swallowed: a money game without a working cooldown/outcome record fails loud. See `docs/sdk-cs.md`.

## Build

```bash
docker build -f bundles/csharp/superpenguin-roll/Dockerfile \
  -t waddles/bundle-superpenguin-roll-build:latest .
mkdir -p /tmp/superpenguin-roll-out
docker run --rm --user "$(id -u):$(id -g)" \
  -v /tmp/superpenguin-roll-out:/out waddles/bundle-superpenguin-roll-build:latest
```

Or `make build-superpenguin-roll-bundle` (`scripts/verify-superpenguin-roll-fixture.sh`),
which does the same and reports the resulting component's size and sha256.

**No wasm fixture is committed** to
`core/bundle_executor/tests/fixtures/` -- unlike `csping.wasm` (4.36 MiB),
this component was measured at build time to be at/above this repo's 5MB
threshold for committed binary fixtures (see that directory's own note).
Build it locally with the command above instead of relying on a committed
copy; there is correspondingly no
`core/bundle_executor/tests/csharp_superpenguin_roll_integration.rs` --
see that directory's note for the same reason.

## Test

```bash
make test-superpenguin-roll
# or:
bash scripts/test-superpenguin-roll.sh
```

26 xUnit tests on the host CLR (the pinned, containerized .NET SDK image); line coverage of
`RollLogic.cs` + `RollDispatch.cs` is 100% (branch ~93-100%), gated by
`tests/coverlet.runsettings`. The suite covers the prize table, both aliases, per-alias and
per-user cooldown independence, the kv-denied graceful degradation, flag-off (and that a disabled
flag never burns the cooldown slot), the flag's documented key + fail-closed default, relay target
= the event's origin platform, and every dispatch failure code above.

## Feature flag

Gated behind the PostHog flag `waddles.command-superpenguin-roll`, defaulted
OFF (`rules/critical-rules.md` Feature Flags & License Tiers), checked in
`RollLogic.Transform` after the command match and before the cooldown
acquire -- see `RollLogic.cs`'s own doc comment. Deliberately a different key
from the unrelated first-party `bundles/python/roll` bundle's
`waddles.command-roll` (a different NdM dice command that also answers to
`!roll`) so the two are independently toggleable.

## Activation (intentionally NOT in `bundles/core-bundles.yaml`)

Third-party-attributed (`provider: thirdparty`, `waddles.integrations.*`), so `hub_api/cli/seed_core_bundles.py`'s
`_guard_core_namespace` refuses it by design (vendor-separation ruling). It must be published through the vendor path
(`POST /apps/{app_id}/versions`, then admin approve) -- BUT that path's `bundle_component_validator.py` currently requires
exactly `process-stage` + `action-stage` exports and rejects the `streaming-lifecycle` export every `stage-next*` world carries
(`stage_export_mismatch`). Until S0 relaxes the validator (see `docs/sdk-cs.md` "Needs from S0") the 1.1.0 component is
built and executor-verified (`make test-superpenguin-roll-e2e`) but not installable through the vendor pipeline.

## App/service boundary (2026-11-07)

This port remains C#. Game rules, wagering, and maximum-bet handling belong
to the app; atomic ledger settlement uses the app's granted host primitive.
The shared economy service is unchanged. Shared balance/credit/debit operations,
where used, invoke its existing tools via the C# SDK; no private ledger is created.
See `docs/superpenguin-port-inventory.md` for the port decisions and remaining work.
