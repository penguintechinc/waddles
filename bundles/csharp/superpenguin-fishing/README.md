# superpenguin-fishing

C# port of the SuperPenguinTV fishing game from
[Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) `Bot/Commands/Fishing/*`
(MIT, used with permission; notice verbatim in `bundle.yaml`). Author `github/psychoboy`, collection `superpenguintv`,
category `alternatives`. App id `waddles.integrations.superpenguin.fishing`. The app owns every game rule; the only
service it calls is the economy service's `adjust_balance` (tournament point rewards).

## Why one bundle, not three

The brief suggested fishing / shop / tournament bundles. They share state (a player's gold, inventory and catches; the
tournament boards that scoring a catch feeds) and **bundle kv is isolated per app id**, app-to-app calls are forbidden, and
there is no inventory or shared-state service. Splitting would leave the shop unable to see gold or items and the
tournament unable to see catches. So the three concerns are separate source modules behind one persistence interface and
ship as one component (rules in `FishingEngine`, `FishingTournament`; persistence behind `IFishingStore`, kv-backed
`KvFishingStore`). When bundle storage v2 / shared storage lands, only `IFishingStore` needs a new implementation before
the modules can be published separately. Per-invocation limits are about host calls and runtime, not code size: a cast costs
about 15-26 kv ops (cap 64) regardless.

## Commands (flag `waddles.command-superpenguin-fishing`, default OFF)

| Command | Does |
|---|---|
| `!fish [1-10]`, `!cast` | cast 1-10 times (upstream clamps attempts to 1..10); 20 s per-user cooldown (silent drop) |
| `!fishinv` / `!fishinventory` | items with `#id`, `[E]` equipped, uses, durability |
| `!fishgold`, `!fishlog`, `!fishtop`, `!fishhelp` | gold and totals, species collection, lifetime leaderboards, help |
| `!fishshop [section]`, `!fishbuy <item> [qty]` | browse (tier + upgrade/downgrade hint), buy with gold |
| `!fishsell <#>`, `!fishequip <#>`, `!fishunequip <#>`, `!fishrepair [#\|all]` | inventory management |
| `!fishtourney` | current tournament standings, or the next run |

Tournaments (second flag `waddles.superpenguin-fishing-tournaments`, default OFF) run automatically in recurring UTC windows
(defaults: Heavyweight Hour 18:00-19:00, Game Fish Rush 20:00-22:00). Starting/ending by hand is not offered: no bundle can see
a caller's role, so an unauthenticated start command would be an unauthenticated mint.

## What is faithful, what is not

Faithful (ported rule for rule, see the file headers): rarity weights 50/30/15/4/1/0.2 renormalised to the rarities present,
boost mode, general-rarity / specific-fish / specific-category boosts, stars (5% / 20% bands + star boost), weight
(0.8-1.13 x star x boost, floor 0.01), gold (star bands 0.75-1.0 / 1.0-1.25 / 1.25-1.41 x weight factor 0.9-1.065), accident order
(reel jam, tackle box, net, rod snap 0.05%, line snap 2%) and exactly which gear each destroys, bait/lure use loss, use and
durability consumption with spare auto-equip, sell rate 15% (durability scaled), repair cost, the full default shop (31 items,
verbatim names/costs/boosts), quartile tiers, tournament scoring (all 7 categories), eligibility, ranking tie-breaks and reward
placement selection.

Deviations (all deliberate):

1. **Fish catalog is authored here.** Upstream fish are admin-edited database rows that are not in the source. The 30 shipped fish
   use the upstream rarity thresholds (35/60/110/201/300 on base gold); a test enforces it.
2. **Gold is a closed-loop game resource** held in the player state (upstream keeps it in its own `FishingGold` table, never
   convertible to points). It is *not* community currency, so there is no private points ledger and no mint per catch. The only
   community-currency movement is tournament point rewards, minted through the economy service tool with a stable key
   (`fishing.tournament.<window>` + placement). If you want gold to be real currency, swap the gold fields for economy calls.
3. **RNG** draws one 64-bit seed per command from `System.Random.Shared` and expands it with SplitMix64, so a redelivered event
   reprints the identical casts. Upstream uses a CSPRNG for every roll; `System.Security.Cryptography` is unsupported in the
   NativeAOT-wasi build (the real-executor test trapped on it), so odds are faithful but not cryptographically unpredictable.
   Gold is not currency, but tournament rewards are mint-capped by the economy service; revisit if a host CSPRNG import lands.
4. **Chat commands and wording** are new (upstream chat triggers/messages live in admin-configured actions).
5. **Not ported:** admin editing UIs (Blazor pages), analytics/balance simulation, help data pages, admin item gifting,
   tournament entry fees (upstream defines the field but never collects it in the ported code), SignalR/WebSocket overlay pages.
6. Durability/repair exist and are tested but the default shop has no durable items and repair is off, as upstream defaults.

## Replay and money safety

* Every mutating command records its result under `message id + platform + actor + command` **before** writing state and marks
  the key applied inside the single-value player write (last 24 keys). A redelivery reprints the record; a crash between record
  and write is repaired from the recorded seed. A per-player lock (10 s TTL) serializes mutations (kv has no CAS).
* A blank message id throws (no replay key, no mutation).
* Tournament aggregates are idempotent per event; boards and leaderboards are monotone/idempotent upserts.
* Settlement uses `TickScheduler.Pending()` + `Cancel()` (not `TakeDue()`): payout is idempotent (economy key; per-player applied
  marks for gold) and the task is only cancelled after success, so a failure or crash retries on the next event instead of
  losing the payout. Economy failures are logged at error level and never fail the unrelated command that triggered settlement.

## Limits and risks (measured, see PR)

* kv: 64 KiB/value (player blob is a few KB; inventory capped at 200 items), 10k keys/app: roughly one key per player plus
  per-window per-participant aggregates (4 day TTL) - a very large community will approach the key cap.
* Windows only settle when another fishing event reaches the bundle (TickScheduler limitation); a quiet channel settles late.
* Tournament boards are bounded to the top 25; `Average` scoring can be slightly stale for players that fell out of the top 25.
* Measured: wasm 10,832,476 bytes (roll is 7.3 MB; +48% from System.Text.Json source-gen contexts, LINQ and the catalog); exports
  process-stage/action-stage/streaming-lifecycle verified. Real-executor run (`SUPERPENGUIN_ONLY=fishing`, flag-off + `!fish 3` +
  replay) passed in 186 s wall on a *debug* wasmtime build, almost all of it one-time component compile/instantiate - the
  per-replica cold start is the risk against the 2 s per-invocation budget (see `docs/sdk-cs.md`); warm invocations are a handful
  of kv ops. The host `overlay` import is `not_implemented` in `core/bundle_executor` today, so the alert push is logged and skipped.

## Overlay

On every fresh cast the bundle registers (once a day) and pushes one declarative `alert_box` widget
`waddles.integrations.superpenguin.fishing.catch` through the `overlay` import (permission `overlay.media`): title = fish or
accident, body = stars/weight/gold, `alert.user` = tokenized user uuid (never a name), `alert.amount` = gold. Add it in the
overlay designer palette ("Fishing catch") or use the community's `alert_box` browser-source URL
(`/overlay/<community>/alert_box`) in OBS. A dead or not-yet-implemented overlay never fails a cast (warn log
`fishing.overlay_failed`).

**Upstream overlay assets.** Reviewed `wwwroot` at the pinned revision: the fishing overlay is `fishing.html`, `js/fishing.js`,
`js/fishingSignalR.js`, `fishing-tournaments.html`, `js/fishing-tournaments-overlay.js` (all MIT, repository license). They are
executable HTML/JS bound to the bot's own WebSocket topics, and Waddles overlays are declarative-only (widget descriptors are
host-validated JSON, never HTML/CSS/JS; custom-code widgets do not exist yet), so they are **not shipped**; the animation timing
(5 s display) is carried in the widget descriptor. Upstream ships **no fish images, sprites or fishing sounds** in the repository
(fish images are admin-uploaded per fish; the `wwwroot/media` and `wwwroot/gifs` assets belong to other games), so there is
nothing to package or license-check for fish. Nothing with an unclear license was copied.

## Build and test

```bash
make test-superpenguin-fishing          # xUnit + 90% line-coverage floor (docker, pinned .NET SDK)
make build-superpenguin-fishing-bundle  # pinned Dockerfile -> /tmp/superpenguin-fishing-out/superpenguin_fishing.wasm
make test-superpenguin-ports-e2e        # all C# ports through the real bundle-executor (includes fishing)
```
