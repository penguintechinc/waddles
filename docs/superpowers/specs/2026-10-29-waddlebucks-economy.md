# Waddlebucks Economy — Program Design

**Date:** 2026-10-29
**Status:** APPROVED (owner, 2026-10-29) — Phase-1-first; proceeding to implementation plan.
**Owner:** justin@penguintech.io
**Interim notes:** `~/.claude/projects/-home-penguin-code-waddles/memory/giveaway-raffle-dungeon-economy.md`

---

## 1. Intent & Why

Communities want a **points economy** as a first-class engagement loop: viewers *earn* a community currency for showing up and participating, and *spend* it on giveaways, raffles, and a themed "dungeon raid." Today the mechanics are scattered across half-built bundles and a second, parallel currency (`loyalty`), and the money-safety-critical parts aren't unified. This program consolidates everything onto the **existing `core/bundle_host_economy`** host capability (balances, ledger, atomic wager/transfer, 24h caps, idempotency, leaderboard — already built, mirroring `bundle_host_reputation`) and fills the genuine gaps: a privileged mint path, the game/earn bundles, shared anti-abuse, and an event source for automation.

The currency is **"waddlebucks"** by default, with a **per-community admin-settable alias**.

## 2. Goals / Non-Goals / Anti-Goals

**Cross-platform scope (owner-confirmed):** the economy and the giveaway/raffle/dungeon games are **community-wide and cross-platform** — balances key by (tenant, community, community-user-UUID), identical across Twitch/Discord/YouTube; entries resolve a platform handle → community user UUID (PII tokenization) before touching balances. **Only lurker-economy is Twitch-only** (active-viewer presence is Twitch-specific).

**Goals**
- One unified, per-community, **cross-platform** currency on `bundle_host_economy` — no second ledger, no per-platform split.
- A **core economy SERVICE bundle** exposing `!give` / `!points` / `!points view` / `!points leaders` / `!points set`.
- A merged **giveaway + raffle + dungeon APP bundle** (dungeon = multi-winner raffle reskin), with optional entry cost, single/multi-winner pot splits, and layered anti-abuse.
- A **lurker-economy APP bundle**: passive Twitch watch-time earning + opt-in `!loot` active-claim.
- Earn paths: manual admin grant, Twitch channel-point→waddlebucks redeem, lurker drip/`!loot`, and (later) money-events.
- A **repo-level anti-abuse library in `waddle-sdk`** reusable by any bundle.

**Non-Goals (this program)**
- Real-money purchase of waddlebucks (currency is earned/granted only).
- Cross-community or global currency — balances are strictly per (tenant, community, user).
- Non-Twitch passive earning in v1 (Discord/YouTube watch-time deferred).

**Anti-Goals (failure even if it "works")**
- A third parallel currency, or leaving `loyalty`'s private table live alongside the host.
- Any non-atomic balance mutation, or minting outside the privileged writer (inflation/theft risk).
- Predictable winner selection (seeded RNG) — must be CSPRNG.
- Raw PII (handles) in logs, ledger, or bundle state — UUID/pseudonym only.
- Bot-farmable earning with no opt-in defense available to admins.

## 3. Constraints (house standards that bind this design)

- **App bundles are Python**; core data-plane/host stays Rust (`bundle_host_economy` is Rust and already exists).
- **Winner selection uses `random.SystemRandom()`**, never seeded `random.Random` (the current `giveaway` bundle's weakness).
- **Numeric-id keying**: economy state keyed by numeric (tenant, community, user) ids, never slug/handle (`[[data-plane-keys-on-numeric-ids]]`).
- **PII-free**: bundle logs/state use community_id/op/counts and pseudonymous actor hashes; no raw handles (`[[bundle-logs-must-be-pii-free]]`, PII tokenization).
- **Bundle version bump** on any bundle change (`[[kv-key-charset-no-colon]]`).
- **Feature-flagged**: each new user-facing feature behind a PostHog flag, defaulted off.
- **Licensing**: community-scoped premium features gate on the team/community tier; confirm which (if any) economy features are tier-gated vs free (see Open Questions).
- **Non-PRC dependencies**; any new dep pinned + hashed.

## 4. Current State (what already exists — recon 2026-10-29)

| Piece | Status | Where |
|---|---|---|
| Currency engine (balance/ledger/`wager`/`transfer`/`leaderboard`/24h caps/idempotency/theft-guard) | **Built** | `core/bundle_host_economy` (Rust host), WIT `economy` interface, `waddle_sdk/economy.py` |
| Pay-into-pot wagering pattern | **Built** | `jackpot`, `lottery` bundles (template to mirror) |
| `economy.transfer` (basis for `!give`) | **Built** | `steal` bundle |
| `economy.leaderboard` read (basis for `!points leaders`) | **Built** | `top` bundle |
| `!points` command grammar | **Built but wrong store** | `loyalty` bundle — private `loyalty_balances` table; **migrate onto the host, retire the table** |
| Raffle (manual, single-winner, `SystemRandom`, no pot) | Partial | `raffle` bundle |
| Giveaway (timed, free, single-winner, **seeded RNG**) | Partial (RNG weak) | `giveaway` bundle |
| Privileged **mint** writer (fund/earn) | **Missing** | deliberately outside the economy crate — net-new, hub-side |
| **Event source** for sub/donation/timer | **Missing** | everything fires on `chat.message`; `timer` bundle notes "no scheduler event exists" |
| Twitch **active-viewer** roster + follower/sub lookup | **NET-NEW** (recon 2026-10-29) | only membership *events* exist (EventSub follow/sub/gift/cheer → reputation); NO viewer-presence roster, NO is-follower/is-sub lookup. Reusable: EventSub pipeline (`core/svc_ingest/eventsub.py`), `activity_watch_sessions` table (`config/postgres/migrations/044`), reputation follow/sub weight model. Must add Helix `get_chatters` polling + `channels/followers` + `subscriptions` lookups to `core/twitch_helix` / `svc_action/services/twitch_helix.py`. |

**Verdict:** ~70% of the money-critical substrate exists. Net-new = assembly (one service bundle, two app bundles) + two shared-infra pieces (mint writer, event source) + an SDK anti-abuse library.

## 5. The Design

### 5.0 Shared: `waddle-sdk` anti-abuse library (foundation, benefits all bundles)
A repo-level shared library in `waddle-sdk`:
- **Entry challenge**: admin-selected mode — (1) rotating random challenge word (changes per round), or (2) echo-your-own-username (no `@`). Validates an appended token on `!enter <token>` / `!loot <token>`.
- **Eligibility predicate**: Twitch-membership check (follower / sub / both / all-active) — likely also SDK-level, reused by lurker-economy and money-event earn.
- Failure behavior configurable per community: **reject-with-message** or **silently-drop** (the latter mirrors the shadowban).

### 5.1 Shared infra: privileged TREASURY writer (hub-side)
The single capability that **creates** currency and **moves pot/escrow funds**. Two jobs:
- **Mint** — all earn paths are callers: `!points set`, Twitch-redeem, lurker drip/`!loot`, money-events.
- **Pot payout** — resolves the actor-only limitation of the host (`wager`/`transfer` only move the invoking actor's money, so a winner — not the actor at draw — can't be paid via `wager`). Model: a **per-community POT account**; entries `transfer` into it (pot = Σ entries); at draw the treasury **atomically debits the pot and credits the winner(s)**.

Lives outside `bundle_host_economy` by design (a privileged writer). Writes through the host's ledger so every mint/payout is audited, idempotent, and caps-aware. **Hardest-gated surface** — minting is the inflation/abuse risk.

**Per-community money-supply cap.** The admin sets the **total waddlebucks that exist in their economy** (default **1,000,000**). **Minting** draws against this ceiling; **transfers/wagers/pot-payouts** move existing currency (no effect); **burn sinks** (e.g. a giveaway entry fee) free headroom back. At the cap, minting **pauses (logged)** until currency is spent/burned or the admin raises the cap. The treasury tracks circulating supply per community and refuses/clamps a mint that would exceed the cap. (Decision for the plan: new host op/counter vs. hub-side aggregate — the ledger already exists to sum against.)

### 5.2 Core economy — SERVICE bundle
Currency management, consumed by other bundles. Commands:

| Command | Who | Host op |
|---|---|---|
| `!points` (alias `!waddlebucks`) | anyone → own balance | `balance` |
| `!points view <user>` | anyone; admin can lock to admins / admins+mods | `balance(other)` |
| `!points leaders [N=5]` | anyone | `leaderboard` |
| `!give <target> <amount>` | anyone → gift own points | `transfer` |
| `!points set <target> <amount>` | admins (+ mods if enabled) | **privileged treasury write** |

`!points set` semantics: **absolute** (`set <user> 500`), **relative** (`set <user> +10`/`-10`), and **bulk targets** (`set all +100`, `set active +100`). Recorded as signed ledger deltas; bypasses 24h earn-caps (audited admin action) but still bounded by the community money-supply cap. Not self-targetable.

- Migrate `loyalty`'s `!points` grammar onto the host; retire `loyalty_balances`.
- Per-community permission config for `view` and `set` (`set` defaults admins-only, not self-targetable).

### 5.3 Giveaway / Raffle / Dungeon — APP bundle (one bundle, merged)
Mirror the `jackpot` pattern (pot + `economy.wager` + `SystemRandom` + replay-safety).
- **Modes:** giveaway (external-object prize, e.g. game key / gift card), raffle (points pot), dungeon (= multi-winner raffle reskinned, "y'all raided the dungeon, everyone came out with different loot").
- **Optional entry COST** (admin-set; 0 = free). Raffle/dungeon entry `transfer`s the cost **into the per-community POT account** (pot = Σ entries); giveaway cost is a pure **burn sink** (frees supply-cap headroom). At draw, the **treasury** (§5.1) atomically debits the pot and credits winner(s) — NOT via `wager` (which is actor-only and 100×-capped).
- **Winner modes:** single-winner and multi-winner; multi-winner **randomly splits the pot** among winners (`SystemRandom`).
- **Anti-abuse (two independent, opt-in layers):**
  1. **Reputation-score filter at draw** (NOT a stored ban list) — entry always accepted ("you entered"); then at draw, `eligible = [u for u in entrants if reputation_score(u) >= admin_floor]` (query `bundle_host_reputation`) and `SystemRandom` picks from `eligible`. No ban table; must not leak to the filtered user. PII-free.
  2. **Entry challenge** — the SDK anti-abuse library (`!enter purpleshuffle` / `!enter <username>`).
- Commands unify the old `giveaway`/`raffle` surfaces (resolves the `!enter` collision by merging, not renaming).

### 5.4 Lurker-economy — APP bundle (Twitch-only v1)
Passive + active earning for active Twitch viewers.
- **Passive drip:** active viewers earn over time; admin-set cadence + amount + eligibility (all-active / followers / subs / combos).
- **Active `!loot` claim (opt-in):** on a cadence (hourly default), prompt eligible viewers to `!loot <challenge>` for an admin-set amount — doubles as proof-of-presence + bot defense (SDK challenge library).
- **Needs a NET-NEW Twitch active-viewer + membership data source** (recon confirmed none exists): Helix `get_chatters` polling → populate `activity_watch_sessions`; Helix `channels/followers` + `subscriptions` for eligibility. Build on the existing EventSub pipeline + `activity_watch_sessions` table. This is the bulk of Phase 2's new work.
- All minting via the privileged treasury writer.

### 5.5 Twitch channel-point → waddlebucks redeem exchange
A Twitch channel-point redemption converts to waddlebucks at an **admin-set rate** (default **1000 waddlebucks : 500 Twitch points**). Consumes channel-point redemptions (Twitch connection/redeem integration), mints via the privileged writer. Where the redeem handler lives (core economy service vs the Twitch connection layer) is an Open Question.

### 5.6 Event source for automation (net-new, largest lift)
No sub/donation/timer event exists today. Net-new event source feeding:
- **Auto-run triggers**: raffles/dungeons that start on a "money event" (Twitch sub/resub/gift, donation, YouTube join) or on a **timer** (every X minutes).
- **Money-event earn**: sub/donation rewards paid in waddlebucks.
This is the heaviest net-new infra and is sequenced last.

## 6. Phased Delivery (dependency-ordered)

**Architecture (owner-refined 2026-10-29):** the **economy & reputation are SYSTEM SERVICE bundles** that own the "how" — member/table lookups, ledger read/write, atomic adjustment with caps/idempotency/supply-cap — and hold the privileged host grants. **App bundles own only the "what"** — they compute a *delta* (pot split, loot amount, entry cost, rep change) and ask the service bundle to apply it. This requires a **service-bundle-invocation framework** (app bundle requests a permission to call a system service bundle; admin approves) — NET-NEW, since bundle-to-bundle calls don't exist today.

```
Phase 1a  Treasury host op + migrations (Rust)      mint/payout/supply-cap on bundle_host_economy
Phase 1b  Service-bundle-invocation framework       app→service calls, permission-request + admin-approval  [NET-NEW platform]
Phase 1c  Economy SYSTEM SERVICE bundle (Python)    !give/!points/set; holds host grants; exposes apply-delta ops
Phase 1d  Retire loyalty                            migrate loyalty_balances → host ledger; drop legacy table
Phase 2   Earn: Twitch-redeem + lurker-economy app  [+ NET-NEW Twitch viewer/membership data source]
Phase 3   Spend: giveaway/raffle/dungeon app        computes deltas, calls economy service; rep-filter + SDK anti-abuse
Phase 4   Event source → auto-run triggers + money-event rewards   [largest net-new]
```
Each ships independently. 1a is the base dependency; 1b (service-bundle IPC) is what lets later app bundles stay thin (compute-only). SDK anti-abuse library lands in Phase 3 (its first consumer).

## 7. Open Questions (for the owner)

0. ~~Pot payout model~~ — **RESOLVED (owner 2026-10-29):** per-community POT account + privileged treasury payout; plus a per-community **money-supply cap** (default 1,000,000) that minting draws against. `!points set` supports absolute/relative/bulk(all,active).
1. **v1 cut** — ship Phase 1 first (confirmed default), or pull Phase 3 (the game) earlier so there's something to spend on sooner?
2. **Tier-gating** — are any economy features licensed (e.g. premium = multi-winner, lurker-economy, redeem exchange), or all free in every tier? (Statutory/core-engagement features stay free.)
3. **Redeem handler home** — does the Twitch channel-point→waddlebucks handler live in the core economy service bundle, or in the Twitch connection/ingest layer?
4. ~~Twitch active-viewer extension reuse~~ — **RESOLVED (recon): does NOT exist.** Lurker-economy Phase 2 includes building the Twitch active-viewer roster (Helix `get_chatters` polling) + follower/sub lookups. Confirm the polling cadence/rate-limit approach is acceptable (Helix `get_chatters` requires a mod token + is rate-limited).
5. **Shadowban + challenge failure UX** — per-community choice of reject-with-message vs silent-drop: default to which?
6. **Caps** — should the economy's existing 24h per-user/per-scope caps apply to mint (earn) rates by default, and are the defaults admin-tunable per community?

## 8. What's Left to the Builder

- Exact command grammar/args and help text per command (follow `[[core-command-verb-vocabulary]]`).
- DB/migration details for the mint-writer audit path (reuse `economy_ledger`; alembic single-head).
- WIT/grant definitions for any new host surface the mint writer needs.
- Feature-flag keys per feature; PostHog defaults off.
- Tests: real-path economy ops (atomic debit/credit, caps, idempotency), SystemRandom winner distribution, anti-abuse accept/reject, shadowban silent-removal, redeem-rate math, lurker eligibility.
- Marketing screenshots for any UI (settings pages) before release.

---

*Interim capture during brainstorm. On approval, this becomes the committed spec and the per-phase implementation plans follow (superpowers:writing-plans).*
