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

## Permissions (V2 structured)

`permissions: []` in `bundle.yaml` -- **none requested**:

| Capability | Why not needed |
|---|---|
| `storage.kv` | The cooldown uses the host `kv` capability (via `CooldownGuard`), which degrades gracefully when denied (see "kv availability"); no extra grant is declared. |
| `db` | Stateless apart from the cooldown (`data.tables: []`). |
| egress | `egress: []`; the reply goes out over the `relay` host import to the event's own origin platform. |

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

This is third-party-attributed content (`provider: thirdparty`,
`app_id: waddles.integrations.superpenguin.roll`), not a
`waddles.core.*` bundle -- `hub_api/cli/seed_core_bundles.py`'s
`_guard_core_namespace` HARD-refuses (non-zero exit) any catalog entry
outside `services.vendor_bundle_authz.CORE_NAMESPACE_PREFIX`
("waddles.core."), by deliberate design: "vendor bundles must never be
seedable, no matter what a (compromised or mistaken) catalog file says"
(that module's own docstring, Justin's 2026-09-27 vendor-separation ruling --
a vendor SUBMITS, only a GLOBAL ADMIN APPROVES). Adding this `app_id` to
`bundles/core-bundles.yaml` would either break the core-bundle-seeder Job
outright or require weakening that guard -- neither is acceptable.

Until a dedicated third-party/vendor-content activation path exists, publish
and activate this bundle the same way any vendor submission is onboarded:
`POST /apps/{app_id}/versions` (the standard `bundle_version_service.py` /
`bundle_component_validator.py` pipeline this bundle's component already
passes) followed by a `platform:admin`-scoped
`POST /apps/{app_id}/versions/{version}/approve`. No Helm hook currently
automates this for `waddles.integrations.*` bundles -- tracked as follow-up
work, not done here.
