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

## kv availability

The host `kv` capability is currently hardcoded to deny every call in
svc-process/svc-action (see `sdk/waddle-sdk-cs/README.md` "kv/db
availability"). This bundle's cooldown check degrades gracefully in that
case -- `!roll`/`!dice` still work, just without cooldown enforcement until
the real `kv` backend lands.

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
