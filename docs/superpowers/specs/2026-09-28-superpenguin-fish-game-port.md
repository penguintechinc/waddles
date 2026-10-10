# SuperPenguinTV Fishing Game — Port to Waddles C# App Bundles

**Status:** DESIGN — not started. **Author credit:** original game design and implementation by **superpenguintv**
(MIT license, ported with permission). **Source:** `PenguinTwitchBot/Bot/Commands/Fishing/` (~21 files, C#/.NET,
EF Core + SignalR overlay). **Target:** Waddles WASM app bundles (`waddle:bundle@1.0.0`, C# via `componentize-dotnet`),
listed under category **Alternatives**.

## 0. Why this is an early/experimental port

The C# bundle SDK has **no shipped 1.0.0 precedent** — `bundles/csharp/csping` is the only C# bundle in the repo
and targets the not-yet-merged `stage-v1_1` world (`docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md` §5,
row "C# SDK"). `hub_api/services/bundle_manifest_v2.py:40` (`_ALLOWED_LANGUAGES`) does not include `"csharp"` today
— a manifest-validator gap that blocks any C# bundle registration until fixed (trivial, called out in Phase 0).
This spec assumes that gap closes in parallel; it is not this port's blocker to fix, but nothing here ships without it.

## 1. Feature Inventory (original game)

The original is **not** a chat-command bot in the traditional sense — `FishingHandler.cs` (`Bot/Actions/SubActions/Handlers/FishingHandler.cs`)
is a generic `ISubActionHandler` wired into the bot's Action/Trigger framework; actual chat-command text/cooldowns are
configured *outside* the Fishing folder (`BotCommandsRegistry.cs`) and are **not part of this port's source material**.
Player-facing feedback is a SignalR broadcast (`MainHub`, event `ReceiveFishCatch`) plus a WebSocket overlay queue
(`WsTopics.Fishing`) — i.e. this is a stream-overlay minigame, not raw IRC commands.

### 1.1 Core loop

| Step | File:Line | Behavior |
|---|---|---|
| Entry point | `FishingGameplayService.cs:31` `PerformFishingAttempt(userId, username)` | One "cast" |
| Rod snap check | `FishingGameplayService.cs:65-103` | `WASI/crypto rand < RodSnapChance` (default `0.0005`, `FishingSettings.cs:8`) → lose Rod+Line+Hook, no catch |
| Line snap check | `FishingGameplayService.cs:105-137` | `rand < LineSnapChance` (default `0.02`, `FishingSettings.cs:7`) → lose Line+Hook, no catch |
| Fish select | `FishingGameplayService.cs:139` → `FishingCalculations.SelectRandomFish` | Rarity-weighted roll (§1.3) |
| Stars roll | `FishingGameplayService.cs:140` → `CalculateStars` | 1-3 stars |
| Weight roll | `FishingGameplayService.cs:141` → `CalculateWeight` | |
| Gold roll | `FishingGameplayService.cs:142` → `CalculateGold` | |
| Persist + tournament link + gold credit + broadcast | `FishingGameplayService.cs:144-231` | One EF Core unit of work |
| Multi-cast | `FishingHandler.cs:65-70` | `attempts` clamped `1..10` per invocation, `Task.Delay` between casts for overlay pacing |

**Cooldowns:** none inside these files. Rate-limiting is the bot's generic Action/Trigger cooldown config
(out of scope — not present in the ported source). **Waddles needs its own cooldown** (§5).

### 1.2 RNG

Exclusively `System.Security.Cryptography.RandomNumberGenerator` (a CSPRNG) — `FishingCalculations.cs:2,29,74,159,181,198`
and `StaticTools.cs:7-40`. **`System.Random` is never used anywhere in the ported files.** This is load-bearing for
fairness (`FishingAnalyticsService` computes exact probabilities and audits real-world drift against them) and must
carry over 1:1 — see §5.

### 1.3 Rarity / weight / value math (exact formulas)

**Base rarity weights** (`FishingRarityWeightProfiles.cs:9-18`), normalized to only rarities present among enabled
fish, optionally boosted:

| Rarity | Base weight |
|---|---|
| Common | 50.0 |
| Uncommon | 30.0 |
| Rare | 15.0 |
| Epic | 4.0 |
| Legendary | 1.0 |
| Mythical | 0.2 |

`BoostMode` (admin setting) multiplies every non-Common weight by `BoostModeRarityMultiplier` (default `2.0`) —
`FishingRarityWeightProfiles.cs:65-74`. Per-user equipment can further multiply non-Common weights
(`GeneralRarityBoost`, `FishingCalculations.cs:140-149`) or re-weight fish *within* the chosen rarity toward a
specific fish/category (`SpecificFishBoost`/`SpecificCategoryBoost`, `FishingCalculations.cs:90-125`, multiplicative:
`multiplier *= 1.0 + boostAmount` per matching boost).

**Stars** (`FishingCalculations.cs:152-163`):
```
threeStarChance = 5.0  + boostAmount*100   // StarBoost equipment
twoStarChance   = 20.0 + boostAmount*100
roll = crypto_rand[0,100)
3★ if roll < threeStarChance; 2★ if roll < threeStarChance+twoStarChance; else 1★
```

**Weight** (`FishingCalculations.cs:165-186`):
```
starMultiplier   = 3★:1.5, 2★:1.2, 1★:1.0
boostMultiplier  = max(0.01, 1 + WeightBoost sum)   // floor prevents negative-stack zeroing
weightMultiplier = 0.8 + crypto_rand[0,1) * (1.13 - 0.8)
weight = round(BaseWeight * weightMultiplier * starMultiplier * boostMultiplier, 2), floor 0.01
```

**Gold** (`FishingCalculations.cs:188-209`):
```
(min,max) by star = 3★:(1.25,1.41), 2★:(1.0,1.25), 1★:(0.75,1.0)
randomValue = crypto_rand[min*1000, max*1000) / 1000
weightMultiplier = clamp(0.9 + ((actualWeight/BaseWeight - 0.8) / 0.33 * 0.165), 0.9, 1.065)
gold = max(1, floor(BaseGold * randomValue * weightMultiplier))
```

**Rarity-from-gold reverse mapping** (admin/legacy tool, `FishingRarityThresholdRules.cs:7-18`), strictly-increasing
thresholds enforced (defaults: Uncommon 35, Rare 60, Epic 110, Legendary 201, Mythical 300 gold —
`FishingSettings.cs:20-24`). **Validation** (`FishingValueRules.cs`): `BaseWeight` rounded/non-negative; boost
amounts clamped to `[-0.8, 5.0]`.

### 1.4 Inventory & sell rules

- `UserFishingBoost` = one owned item instance. **Flat 15% sell rate** of shop `Cost` (`FishingInventorySellRules.cs:16,20`).
- Cannot sell: equipped items, items with `RemainingUses >= 0` (limited-use in progress), or consumables
  (`FishingInventorySellRules.cs:23-51`).
- Tier comparison (New/Equipped/Upgrade/Downgrade/Sidegrade) drives "is this better than what I have" UI copy
  (`FishingTierComparisonRules.cs`), keyed off a dynamically computed `EquipmentTier` map.
- **Concurrency:** per-user `KeyedSemaphore` lock wraps purchase/sell (`FishingInventoryService.cs:18,51,58,111`) —
  gold debit + item creation share one `SaveChangesAsync` (atomic).

### 1.5 Shop

`FishingShopService.cs` — CRUD, dynamic tier computation (`CalculateDynamicTiers`/`GetDynamicTier`, cached,
invalidated on mutation), bulk price multiplier ops. `FishingShopItemGenerator.cs` — default catalog generator:
rods, reels, lines, hooks, tackle boxes, nets, baits, lures (`AddRods`..`AddLures`, template-based idempotent upsert
via `ApplyTemplate`). Items can carry up to 3 stacked boost slots (`BoostType`/`BoostType2`/`BoostType3` +
amounts), an `EquipmentSlot`, and optional `MaxUses` (consumable vs. permanent).

### 1.6 Tournaments

- **Scheduling:** `FishingTournamentScheduler` (`BackgroundService`, 1-minute poll) auto-flips
  `Scheduled→Active` at `StartsAtUtc` and `Active→Completed` at `EndsAtUtc` (`FishingTournamentScheduler.cs:7-89`).
- **Eligibility:** per-tournament allowlist of fish types and/or categories; empty allowlist = all fish eligible
  (`FishingCalculations.cs:232-257`).
- **Scoring** — `FishingTournamentScoreCategory` (`FishingService.cs:840-871`):

| Category | Score |
|---|---|
| `Largest` / `SpecificFish` | max(weight) |
| `Smallest` | min(weight), sorted ascending |
| `MostValuable` | max(gold earned) |
| `Average` | mean(weight) |
| `MostCatches` | count |
| `TotalWeight` | sum(weight) |

- **Rewards:** `SettleFishingTournamentRewards` (`FishingService.cs:656-745`) pays per enabled `RewardRule` in
  placement order — either points on the platform's own currency (`_pointsSystem.AddPointsByUserId`, an *external*
  points system, not fishing Gold) and/or fishing Gold, or a percentage of a paid entry fee
  (`FishingTournamentRewardKind.EntryFeePercentage`). Gold credit is clamped to `int32.MaxValue`. Settlement and the
  `Completed` status flip share one `SaveChangesAsync` (idempotent against double-settlement — status checked first).

### 1.7 Leaderboards

`FishingLeaderboardService.cs` — read-only projections, no built-in reset cadence (all-time; an admin
`ResetAllUserData()` exists for a hard wipe): total-gold, snap-loss, most-valuable-catch, recent-catches (global and
per-user).

### 1.8 Analytics / fairness (`FishingAnalyticsService.cs`, 1617 lines — the largest file)

Admin-only economy tooling, not directly player-facing (though `FishingHelpDataService` surfaces some of its
baseline numbers to players):

| Method | Purpose |
|---|---|
| `SimulateFishing(iterations, shopItemIds)` | Monte Carlo run of the exact §1.3 formulas — rarity/star/fish distribution, snap rates, net gold |
| `CalculateCatchProbabilities` / `CalculateRarityProbabilities` | Closed-form probability given current shop state/boosts (no simulation) |
| `AnalyzeGameBalance(startDate?, endDate?)` | Real-DB economy report: gold economics, snap-adjusted net gold, per-item affordability by engagement percentile, projected progression windows, free-text balance recommendations |

This is the fairness/audit surface the original relies on to keep the RNG-driven economy honest — the port should
preserve the *capability* (simulate + real-data reconciliation) even if the first cut ships a smaller report.

### 1.9 Persistence

EF Core + `IUnitOfWork`/generic-repository over a relational DB: `FishCatch`, `FishingGold`, `FishingShopItem`,
`UserFishingBoost`, `FishingTournament` (+`FishingTournamentCatch`, +`FishingTournamentRewardRule`),
`FishingSettings`. This maps cleanly onto Waddles' bundle-owned-table model (§3) — schema is already normalized and
small.

## 2. Bundle Split (3-4 parallel-buildable C# bundles)

| Bundle (`app_id`) | Owns | Depends on | Ports from |
|---|---|---|---|
| `waddles.integrations.superpenguin.fishing-core` | Casts, catch math, gold ledger, inventory, sell | nothing (foundation) | `FishingCalculations`, `FishingRarityWeightProfiles`, `FishingRarityThresholdRules`, `FishingValueRules`, `FishingGameplayService`, `FishingInventoryService`, `FishingInventorySellRules`, `FishingTierComparisonRules` |
| `waddles.integrations.superpenguin.fishing-shop` | Shop catalog, item generation, dynamic pricing/tiers, boost application data | reads fishing-core's `data.tables` (own role, cross-bundle read needs a shared/exported table or an event, not a raw cross-bundle DB grant — see §3) | `FishingShopService`, `FishingShopItemGenerator` |
| `waddles.integrations.superpenguin.fishing-tournaments` | Tournament lifecycle, standings, reward settlement, leaderboards | fishing-core's catch events (`process-stage` consumes fishing-core's `platform-event`, or both read the same `fish_catches` table if co-owned — see §3 open question) | `FishingTournamentScheduler`, `FishingService` tournament methods, `FishingTournamentStanding`, `FishingTournamentRewardStanding`, `FishingLeaderboardService` |
| `waddles.integrations.superpenguin.fishing-admin` *(optional, cut-able from MVP)* | Fish-type/settings CRUD, `AnalyzeGameBalance`/`SimulateFishing` reports, help-page data | reads fishing-core + fishing-shop tables | `FishingAnalyticsService`, `FishingHelpDataService`, fish-type/settings CRUD from `FishingService` |

All four share `feature: waddles.integrations.superpenguin` (module `integrations`, matches
`libs/flask_core/flask_core/app_manifest.py:71` `KNOWN_MODULES`) and `provider: thirdparty` — the closest existing
manifest field to crediting an external author (§7 proposes the actual attribution fields, which don't exist yet).

**Why this split parallelizes:** fishing-core has no bundle dependency and can be built/tested standalone against a
synthetic event feed. fishing-shop only needs fishing-core's *schema* (table names/shapes), not its running code, to
build against in parallel. fishing-tournaments needs a stable "catch happened" event contract from fishing-core
(exists on day 1 as a `process-stage` export) but not its full implementation. fishing-admin is cleanly separable
and cuttable.

## 3. Data Model on Waddles `db`/`kv` — and the schema/migration BLOCKER

Read: `wit/waddle-bundle/stage.wit:110-153`, `core/bundle_executor/src/host/imports.rs:1-44,244-261`,
`hub_api/services/bundle_manifest_v2.py:34-35,266-274`, `docs/plans/2026-09-21-m15-bundle-dal-inventory.md`.

### 3.1 How `db`/`kv` are scoped today (v1.0, live)

- `kv` (`stage.wit:112-120`): bundle-scoped key/value under the bundle's own `...:state` key. Always granted.
- `db` (`stage.wit:122-153`): **single parameterized statement**, `$1..$n` placeholders only (string interpolation
  impossible by construction). Comment (`stage.wit:122-125`) states execution runs "under the bundle's own Postgres
  role, restricted to the manifest's `data.tables` and row-level-security scoped to the envelope's tenant/community."
  Granted only when `data.tables` is non-empty.
- **`bundle_executor` is a thin pass-through, not the enforcement point.** `core/bundle_executor/src/host/imports.rs:244-261`
  (`impl db::Host for ExecState`) just serializes `(statement, params)` to JSON and calls
  `HostBridge::call(app_id, call_id, CapabilityKind::Db, "execute", args)` (`imports.rs:31-44`) — nothing is
  fabricated or enforced locally. RLS/tenant/table-allowlist enforcement is *designed* to happen stage-side, in
  `core/svc_process`/`core/svc_action`.
- **HARDER FACT: `db` and `kv` are unconditionally denied on both stages today — not "unproven," non-functional.**
  `core/svc_process/src/capabilities.rs:221-227` and `core/svc_action/src/capabilities.rs:582-588` each match
  `CapabilityKind::Db`/`CapabilityKind::Kv` and return `Err(denied(..., "db/kv capability is not wired in this
  build -- TODO(M4+)"/"TODO(M3+)"))` **unconditionally, for every call, regardless of manifest `data.tables`**.
  There is no code path today where a bundle's `db.execute` or `kv.get/set` call succeeds — this is independent of
  and more fundamental than §3.2's schema/migration gap: even a bundle whose tables already existed and were
  correctly declared would still get `denied` on every call. (`imports.rs:77-82`'s `TODO(M4)` comment is a
  narrower, separate note about the `context` row's tenant/community plumbing, not this dispatch-level denial.)
- `hub_api/services/bundle_manifest_v2.py:266-274` validates `data.tables` entries are lowercase-snake, ≤63 chars,
  and not in a small reserved set (`users`, `tenants`, `communities`, `app_catalog`, `app_activations`,
  `app_tenant_availability`) — **that is the entire check**. It validates the *name is well-formed*; it does not
  create anything.

### 3.2 BLOCKER: no bundle-owned schema/migration mechanism exists

**Confirmed absent, not just unfound:** searched `core/bundle_executor`, `core/svc_process`, `core/svc_action`,
`hub_api/services/bundle_manifest_v2.py`, and the v1.1 design doc
(`docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md`, which extends `db` with `execute-batch` §3 and adds
`moderation`/`scheduled-stage`, but **adds no schema/DDL/migration capability**) for `migration`, `CREATE TABLE`,
`schema`, `provision` — no hit tied to a bundle-initiated table-creation path. Every existing table a bundle touches
today (e.g. `loyalty_balances` for the loyalty system) was created by a **core-repo Alembic migration**
(`alembic/versions/0015_loyalty_core_tables.py` et al.) — i.e. a PenguinTech-engineer-authored, hub-repo-side PR,
not anything the bundle or its manifest can do itself. `data.tables` in `bundle.yaml` is purely an **allowlist of
already-existing tables**; nothing provisions the tables it names.

**Consequence for this port — two stacked blockers, not one:** even setting the schema-provisioning gap aside,
§3.1's dispatch-level `denied` on every `db`/`kv` call means **no bundle of any kind can read or write a table
today**, full stop. Fishing needs both fixed: (a) §3.1's hardcoded denial replaced with real stage-side
implementation (wiring work already flagged `TODO(M4+)`/`TODO(M3+)` in the code itself — not net-new scope, but
currently zero-progress), and (b) a way to provision `fish_catches`/`fishing_gold`/`fishing_shop_items`/
`user_fishing_boosts`/`fishing_tournaments`/etc. without a hand-authored core-repo Alembic PR per table. (a) blocks
every stateful bundle in the platform, not just fishing; (b) is a third-party-bundle-onboarding blocker fishing
happens to be first to need at this scale (half a dozen new owned tables).

**Minimum proposed mechanism (not built — flagging for a separate spec/PR):** a manifest-declared
`data.schema: <path-to-.sql-or-alembic-migration-file>` that hub-api's bundle-approval flow (already a human-review
gate, `hub_api/services/bundle_approval_service.py`) runs through a **sandboxed, reviewed** migration step at
approval time — scoped to only the tables the same manifest lists in `data.tables`, with the same table-name
validation `bundle_manifest_v2.py` already does. This keeps the "a human reviews every bundle" security property
Waddles already has for `data.tables`/`egress`/`consumes`, while removing the "and then also hand-author an Alembic
migration in the core repo" step for every third-party bundle. **Out of scope to design fully here — call this out
explicitly to the reviewer as the port's real go/no-go blocker**, separate from the C#-SDK-maturity caveat in §0.

### 3.3 Proposed tables (pending §3.2 resolution)

| Bundle | Table | Key columns |
|---|---|---|
| fishing-core | `fishing_gold` | `user_uuid` (PK), `total_gold` |
| fishing-core | `fish_catches` | `id`, `user_uuid`, `fish_type_id`, `stars`, `weight`, `gold_earned`, `caught_at` |
| fishing-core | `user_fishing_boosts` | `id`, `user_uuid`, `shop_item_id`, `is_equipped`, `remaining_uses` |
| fishing-shop | `fish_types` | `id`, `name`, `rarity`, `base_weight`, `base_gold`, `enabled` |
| fishing-shop | `fishing_shop_items` | `id`, `cost`, `equipment_slot`, `boost_type[1-3]`, `boost_amount[1-3]`, `max_uses` |
| fishing-tournaments | `fishing_tournaments` | `id`, `status`, `starts_at`, `ends_at`, `primary_score_category` |
| fishing-tournaments | `fishing_tournament_catches` | `tournament_id`, `user_uuid`, `fish_type_id`, `weight`, `gold_earned`, `caught_at` |

**All `user_uuid` columns reference the platform's canonical user UUID only** — never a username, never a
platform-native `platform_user_id` — per the PII-tokenization rule. This is a deliberate deviation from the
original's `UserId`/`Username` string pairs (`FishingTournamentStanding.cs:6-7` carries both; the port keeps
`Username` only as an ephemeral display value resolved at render time, never persisted in bundle tables).

## 4. Currency/Points Integration — BLOCKER (partial)

Read: `action/interactive/loyalty_interaction_module/services/currency_service.py:33-101`, `core/svc_process/builtin_handlers/community_loyalty_process.py`,
`core/svc_action/builtin_handlers/community_loyalty_action.py`, `wit/waddle-bundle/stage.wit` (full file), `core/*/src/capabilities.rs`.

**Bundles cannot debit/credit the platform's native loyalty/points currency today.** `CapabilityKind` (referenced at
`core/bundle_executor/src/host/imports.rs:13`, matched exhaustively in `core/svc_process/src/capabilities.rs:411-414`
and `core/svc_action/src/capabilities.rs`) has exactly 8 variants — `Context, Http, Kv, Db, Relay, Flags, Log,
Clock` — one per `stage.wit` import. There is no `Loyalty`/`Points`/`Currency` kind, and `stage.wit` declares no such
interface. The v1.1 design doc adds `moderation` and `db.execute-batch` but nothing for currency either. The
existing loyalty system (`action/interactive/loyalty_interaction_module`'s `CurrencyService`) is Python, called
in-process by the **native** (non-WASM) `core/svc_process/builtin_handlers/community_loyalty_process.py` — a different,
legacy bundle mechanism from the WASM component model fishing will use, with no host-capability bridge between them.

Additional wrinkle: `CurrencyService.get_balance` (`currency_service.py:53-58`) keys balances by
`(community_id, platform, platform_user_id)` — the platform-native user id, **not** the canonical Waddles user
UUID. Any new capability needs a UUID→platform-identity resolution step, or the loyalty schema itself needs a UUID
column added first.

**Scope reduction that avoids the blocker for MVP:** fishing's own "Gold" is a **bundle-internal currency** (its own
`fishing_gold` table, §3.3) — it never needs to touch the platform loyalty system. Only the *original's* tournament
reward path that also pays real platform points (`_pointsSystem.AddPointsByUserId`, `FishingService.cs:697`) needs
cross-system currency access. **Recommendation: cut cross-currency tournament rewards from MVP** (tournaments pay
fishing Gold only); propose a `loyalty` capability (mirroring how `moderation` was added in v1.1 — new WIT
interface, new `CapabilityKind` variant, `action-stage`-only, rate-limited, audited) as a **follow-up spec**, not a
blocker for shipping fishing-core/shop/tournaments.

## 5. RNG, Cooldowns, Anti-Abuse

**RNG is not a blocker.** `core/bundle_executor/src/engine.rs:120` calls
`wasmtime_wasi::p2::add_to_linker_async(&mut linker)` — this links the full **WASI Preview 2** world, which includes
`wasi:random/random` (host-CSPRNG-backed), into every bundle instance automatically, independent of the custom
`waddle:bundle` imports (`stage.wit` itself declares no random interface — it doesn't need to). .NET's
`componentize-dotnet` toolchain maps `System.Random`/`RandomNumberGenerator` calls onto this WASI import for a
compiled component. This preserves the original's fairness property (CSPRNG throughout, §1.2) with no new host
work.

**Cooldowns are not built anywhere in the ported source** (§1.1) — the original relies on an external,
non-fishing-specific framework. Waddles has no direct equivalent for *bundle-invoked* commands today. Recommended
mechanism, buildable with existing v1.0 capabilities only: `kv.increment(key: "cooldown:{user_uuid}", delta: 1,
ttl_seconds: N)` (`stage.wit:119`) as a token-bucket — first cast in the window returns `1` (allow), reject while the
counter is non-zero and the TTL hasn't expired. No new capability needed.

**Anti-abuse carried over 1:1 from the original, all portable with v1.0 capabilities:**

| Control | Mechanism | Waddles equivalent |
|---|---|---|
| Rod/line snap (equipment loss) | Pre-catch RNG gate | Same, using `wasi:random` |
| Per-user purchase/sell atomicity | `KeyedSemaphore` + single `SaveChangesAsync` | Single `db.execute` per mutation is already atomic per-statement; multi-step debit+create needs `db.execute-batch` (v1.1, not live — §6) or two sequential statements with a compensating check |
| Gold overflow | `Math.Clamp(..., 0, int.MaxValue)` | Same clamp in bundle logic; consider `bigint`/`numeric` column to sidestep the cap entirely |
| Tournament double-settlement | Status-checked-before-settle idempotency | Same pattern; `db` RLS/tenant scoping (§3.1) provides the isolation the original got from EF Core's unit of work |

## 6. Tournament Timers — Needs WIT v1.1 `scheduled-stage` (not live)

Read: `docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md` §1, §5, §6.

The original's tournament start/end is a 1-minute-poll `BackgroundService` (§1.6) — Waddles' closest fit is the
proposed `scheduled-stage` interface (`interface scheduled-stage { tick: func(schedule-id, scheduled-for, fired-at,
fire-id) -> result<option<platform-event>, unsupported-stage>; }`, design doc §1), manifest-declared
(`stages.scheduled.cron`/`interval-seconds`), fired by a new `core/svc_ingest` scheduler role. **Status: design
"APPROVED-WITH-CONDITIONS", merge gated on 8 conditions (§7 of that doc), targets the new `stage-v1_1` world, and is
not implemented** (increment plan §6 lists it as step 6 of 9, after DB-batch and moderation land). There is **no
target date** in the doc.

**Workaround if v1.1 isn't live when fishing-tournaments is ready to ship:**

1. **External cron, same pattern as today's `FishingTournamentScheduler`, but outside the bundle.** A
   platform-side (core-repo) scheduled job — not bundle code — calls the bundle's existing `action-stage.dispatch`
   export on a timer, passing a synthetic `stage-envelope` that fishing-tournaments interprets as "check start/end
   transitions now." This requires zero WIT changes but needs a small core-repo cron hook (outside this bundle's own
   manifest) — flag to the platform team as a shared dependency, not something the bundle can self-schedule.
2. **Manual/webhook-triggered start-end**, if the cron hook above is also not desired pre-v1.1: an admin or a
   platform event (e.g. a chat command) calls `action-stage.dispatch` to start/end a tournament explicitly instead
   of on a timer. Loses the "auto-starts at a configured future time" UX but ships with v1.0 only.

**Recommendation:** build fishing-tournaments against option 1, and swap to `scheduled-stage` once v1.1's increment
6 ships — the bundle's internal start/end logic (standings, reward settlement) is identical either way; only the
trigger source changes.

## 7. Attribution (Manifest Fields — Need to Be Added)

Read: `hub_api/services/bundle_manifest_v2.py` (full parse function), `bundles/csharp/csping/bundle.yaml`.

**No `author`, `license`, `notice`, or `category` field exists in the manifest schema today.** `BundleManifestV2`
(`bundle_manifest_v2.py:84-104`) has: `schema_version, app_id, name, version, feature, module, provider, language,
artifact, execution_model, is_default, stages, egress, data_tables, limits, permissions, routes_to, consumes` — no
attribution fields at all. `provider: thirdparty` (`bundle_manifest_v2.py:215`, one of exactly two allowed values)
is the closest existing signal, but it's a boolean-ish flag, not a byline.

**Proposed new optional top-level fields** (additive, backward-compatible — every existing `bundle.yaml` still
validates with them omitted):

```yaml
author: "superpenguintv"
license: "MIT"
notice: "Ported from superpenguintv/PenguinTwitchBot with permission. Original design and implementation © superpenguintv."
category: "alternatives"   # new enum, e.g. {core, alternatives, community, experimental}
```

`hub_api/services/bundle_manifest_v2.py` would need: 4 new optional string fields on `BundleManifestV2`, parsed at
`bundle_manifest_v2.py:284` (the `return BundleManifestV2(...)` call), with `category` validated against a new
small enum alongside the existing `_ALLOWED_LANGUAGES`-style frozenset. This is a small, additive schema change —
not a blocker, just not built yet. **Also fix in the same change:** add `"csharp"` to `_ALLOWED_LANGUAGES`
(`bundle_manifest_v2.py:40`) — currently only `{python, rust, javascript, typescript, other}`, so `csping`'s own
`language: csharp` manifest would fail this validator as written (confirmed by direct read, not inferred).

## 8. Phased Build Plan (agent-sized tasks, ≤30 min each)

| Phase | Task | Bundle | Depends on |
|---|---|---|---|
| 0 | Add `"csharp"` to `_ALLOWED_LANGUAGES`; add `author`/`license`/`notice`/`category` manifest fields | platform (hub-api) | none — unblocks everything else |
| 0 | Write `bundle.yaml` for all 3-4 fishing bundles (manifest only, no code) | all | Phase 0 schema fields |
| 1 | Port `FishingCalculations`/`FishingRarityWeightProfiles`/`FishingRarityThresholdRules`/`FishingValueRules` as pure C# (no host calls) + unit tests against original formulas | fishing-core | none |
| 1 | Port `FishingInventorySellRules`/`FishingTierComparisonRules` as pure C# + unit tests | fishing-core | none |
| 2 | Wire `process-stage`/`action-stage` exports for a single cast: rod/line snap → fish/star/weight/gold → `db.execute` insert into `fish_catches`/`fishing_gold` | fishing-core | Phase 1, §3.1 db/kv wiring + §3.2 table provisioning |
| 2 | Add `kv`-based per-user cooldown (§5) | fishing-core | none |
| 2 | Purchase/sell/equip flows against `user_fishing_boosts` | fishing-core | Phase 1 (sell rules), §3.2 |
| 3 | Port `FishingShopItemGenerator` default catalog as seed data + `fishing_shop_items`/`fish_types` schema | fishing-shop | §3.1 db/kv wiring + §3.2 table provisioning |
| 3 | Dynamic tier calculation (`CalculateDynamicTiers`) as pure C# + unit tests | fishing-shop | none |
| 4 | Tournament CRUD + eligibility filter (`IsFishEligible`) as pure C# + unit tests | fishing-tournaments | none |
| 4 | Standings calculation (`CalculateStandings`, all 6 score categories) + unit tests | fishing-tournaments | none |
| 4 | Start/end trigger via §6 option 1 (external cron → `action-stage.dispatch`) | fishing-tournaments | platform cron hook (cross-team dependency, flag separately) |
| 4 | Reward settlement — fishing Gold only, no cross-currency (§4 scope cut) | fishing-tournaments | fishing-core's gold table |
| 5 *(optional/cuttable)* | `SimulateFishing` Monte Carlo report as pure C# | fishing-admin | Phase 1 formulas |
| 5 *(optional/cuttable)* | `AnalyzeGameBalance` against real catch data | fishing-admin | Phase 2 catch data existing |
| 6 | Leaderboards (5 read-only queries) | fishing-tournaments (or fishing-core) | Phase 2 catch data |
| 6 | End-to-end smoke test: cast → catch → sell → tournament reward, count-verified (per `critical-rules.md` Verification Integrity) | all | Phases 1-4 |

Phases 1 and 3's "pure C#, no host calls" tasks can start immediately and in parallel with Phase 0 — they only
depend on the original source, not on any Waddles platform work landing first.
