# superpenguin-giveaway (C#)

Provably-fair ticket giveaway and keyed raffles, ported from [Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)
(`Features/GiveawayFeature.cs`, `Features/GiveawayFairnessReport.cs`, `TicketGames/RaffleRuntimeService.cs`; MIT, used with permission,
see `bundle.yaml` `notice`). App `waddles.integrations.superpenguin.giveaway`, author `github/psychoboy`, collection `superpenguintv`.
It is an **app** bundle: it owns giveaway policy (eligibility, fairness, exclusions); the economy **service** owns the ledger.

| | |
|---|---|
| World | `stage-next-tools` (C# NativeAOT-LLVM component, see `docs/sdk-cs.md`) |
| Flag | PostHog `waddles.command-superpenguin-giveaway`, default OFF |
| State | `kv`, community-scoped (no `:` in keys) |
| Services consumed | economy `points_view` (read), `adjust_balance` (write) |
| Platforms | Twitch, Discord `chat.message` |

## Commands

Root `!tgw` (alias `!ticketgiveaway`). Upstream's bare `!enter`/`!draw`/`!open`... collide with other bundles, so everything is namespaced.
Moderator rows need the `is_mod` / `is_broadcaster` badge on the event; absent or non-boolean badges deny (fail-closed).

| Command | Who | Behaviour |
|---|---|---|
| `!tgw enter <n\|max\|all>` | anyone | Buys `n` tickets at 1 point each (economy `adjust_balance` debit). `max`/`all` = whole balance (cap 1,000,000 per command, 100,000,000 per person). |
| `!tgw entries` / `status` | anyone | Own ticket count / round state, prize, entrants, tickets, commitment when closed. |
| `!tgw audit` | anyone | The last draw's audit record (below). |
| `!tgw open` / `close` / `reset` | mod | Reopen entries / freeze the pool and publish the commitment / start a fresh round (tickets are per round). |
| `!tgw draw` | mod | Reveal the seed and draw one winner; repeat draws exclude earlier winners of the round. |
| `!tgw prize <text>` / `tier <1-3>` | mod | Prize text (<=200 chars) / cooldown tier: the winner sits out that many following rounds (upstream's three cooldown tiers). |
| `!tgw exclude <user> [hours]` / `unexclude <user>` | mod | Explicit exclusion list (permanent or expiring). Users are mentions resolved host-side to uuids. |
| `!tgw fairness [n]` | mod | Monte Carlo report of the draw algorithm against the current pool (n clamped 1,000..200,000, default 20,000). |
| `!tgw overlay on\|off\|register` | mod | Enable overlay messages (default off) / register the widget in the overlay designer. |
| `!tgw raffle enter\|count <key>` | anyone | Free single entry / entry count. |
| `!tgw raffle start <key> <winners> <totalAward>`, `end <key>`, `winners <key> <n>`, `award <key> <n>` | mod | Raffle administration (winners 1-50, award 0-1,000,000). |

Eligibility: moderators, the broadcaster and `is_bot`-badged users cannot enter; neither can users on the exclusion list or recent
winners still inside their cooldown. Exclusions are re-applied when the pool is frozen. **Tickets of a user excluded after entering are
not refunded** (the moderator decides whether to refund through the economy service).

## Fairness protocol (version `tgw1`)

1. **Close.** The eligible pool, sorted by user uuid, is hashed: `fingerprint = SHA-256("tgw1\n{round}\n" + "uuid:tickets" lines joined by "\n")`.
   A secret 32-byte seed is generated (32 random bytes mixed with clock, message id and round through SHA-256) and kept private in kv.
   The bot publishes `commitment = SHA-256(seed || ascii(fingerprint))`, the entrant and ticket counts and the fingerprint.
2. **Draw.** The seed is revealed. For draw number `d` (1-based) the winning ticket is the first accepted value of
   `u64_big_endian(SHA-256(seed || ascii(fingerprint) || BE32(d) || BE32(counter))[0..8])`, `counter` = 0, 1, 2..., where a value above the
   largest multiple of `total` is rejected (no modulo bias), taken `mod total`. The entrant whose cumulative ticket range (uuid order) contains
   the index wins. The published line is `Audit round R draw D: fp=... commit=... seed=... total=T index=I range=A-B protocol=tgw1`.
3. **Verify** (anyone, any SHA-256 tool): (a) `SHA-256(seed || ascii(fp))` equals the commitment that was published before the draw;
   (b) re-derive `index` from `seed`, `fp`, `D`, `total`; (c) your own ticket count and range are consistent with `total`; (d) with the sorted
   `uuid:tickets` list (an operator can export it) recompute `fp`. Changing the seed after publishing the commitment, or choosing a different
   pool after the draw, fails (a) or (d).
4. **Re-closing.** Closing an unchanged pool reuses the same commitment; a changed pool necessarily publishes a new commitment (and
   `draw` refuses with "pool changed since it was closed" when entries, exclusions or expiries changed after the close). Viewers should
   treat more than one commitment per round as a signal to ask why. Draws after the first are determined by the same committed values.
5. **Raffles.** The seed is committed at `raffle start` (`commit = SHA-256(seed || ascii("tgw1-raffle|key|round"))`, before anyone has entered) and
   revealed at `raffle end`; winners are picked without replacement, pick `p` (1-based) using the same derivation over the remaining sorted
   uuids with fingerprint `tgw1-raffle|key|round|SHA-256(sorted uuids joined by "\n")`.

The seed's unpredictability rests on the host's `random_get` (the bundle seeds from `System.Random`), so the commitment proves the bot did not
choose the seed after seeing the draw; it does not make the seed secret from a compromised host. SHA-256 is implemented in the bundle (managed code,
tested against `System.Security.Cryptography.SHA256` and the FIPS vectors) because platform crypto is not reliable in WASI.

## Money, replay and limits

* An entry is recorded under the event's replay key **before** the single debit; the host de-duplicates the debit per event; tickets are applied
  with an event tag stored in the entrant's pool entry (last four tags), so a redelivery neither double-charges nor double-credits.
* Pool = 16 kv shards (`tgw.p.<round>.<hex>`, 400 entrants each, ~6,400 entrants). kv has no compare-and-swap: each write is read back and
  retried up to three times; after that the command throws and the redelivered event re-applies exactly once. Two users racing in the same shard
  can still overwrite each other between the write and the read-back (the loser is detected and retried, never silently lost).
* A redelivered entry that lands after the pool was frozen still applies its tickets, which changes the fingerprint; `draw` then refuses until a new close.
* Raffle payouts: winners and the per-winner award (`ceil(total / winners)`) are persisted before any credit and each paid winner is marked immediately;
  credits use a stable per-raffle id, a per-raffle lease answers `ending_in_progress`, a winner who left the community or hit a cap is reported as
  `payout_failed`, any other economy failure propagates and a retry resumes. The host's idempotency key also includes the invoking actor, so a retry by a
  different moderator relies on the persisted paid list rather than the host key.
* No timer exists for app bundles: nothing happens until a command arrives. 2 s / 64 MB per invocation, 64 kv operations per invocation (a draw reads 16 shards).
* Logs carry counts and ids only (no names, no text). Messages name people with the host `{user:uuid}` placeholder.

## Overlay (browser source)

There is **no upstream giveaway overlay** (the upstream giveaway UI is Razor viewer pages), so `overlay/` is original work (MIT):

| File | Purpose |
|---|---|
| `overlay/index.html`, `overlay/giveaway.js` | Browser-source page; subscribes to the community's `alert_box` live channel, renders `superpenguin.giveaway.{open,closed,winner}` and `superpenguin.raffle.winner`, hides after 15 s |

OBS setup: add a **Browser Source**, URL
`https://<overlay-assets-host>/index.html?origin=https://overlay.penguincloud.io&code=<overlay code>&key=<view key>`, size 1920x1080, "Shutdown source when not visible" off.
`origin` is the presentation service, `code` the community's 16-hex overlay code and `key` its VIEW key (never put a PUSH token here). The page uses
`textContent` only, loads nothing from the network except the SSE stream and honours `prefers-reduced-motion`.
Winners are shown by the display name the presentation service resolves from the alert's `user` uuid; the bundle never sees a raw name.
The files are declared in the manifest `overlay_assets` section (path, sha256, type, origin, license) by `scripts/gen-superpenguin-manifests.py`.

Overlay pushes are **opt-in** (`!tgw overlay on`) and then **fail loud**: a refused push throws and the command fails (it is redelivered).
Chat announcements work without any overlay. Upstream assets: none used, none shipped (no giveaway/raffle media exists upstream).

## Host needs and honest status

| Need | Status today |
|---|---|
| `overlay` host handler (`push`, `register-widget`) in svc-process/svc-action | **Not wired**: the executor answers `not-implemented` (`core/bundle_executor/src/host/stage_next_imports.rs`). With overlays off the bundle is fully functional; with them on, close/draw/open/reset/raffle end fail loudly until the handler lands. |
| Overlay asset serving (`overlay_assets` -> a URL the OBS page can load) and a presentation-side consumer | Manifest section is declarative only; no host reads it yet. Host the two files yourself (any static host) until it does. |
| Moderator badges (`is_mod`, `is_broadcaster`, `is_bot`) on `chat.message` payloads | Provided for Twitch; Discord's normalizer may omit them, in which case moderator commands are denied (fail-closed) and bots cannot be detected (use `!tgw exclude`). |
| App config forwarding | Settings (points per entry, rules/terms markdown, passive earnings) are fixed defaults; upstream's settings UI has no counterpart. |
| `catalog row` for third-party ports | Not added (core seeder refuses non-`waddles.core.*` rows); vendor path only, same as the other SuperPenguin ports. |

## Deviations from upstream

* Namespaced commands; default prize text `No Prize`; no follower check in the winner line (no follower capability for apps); winners are not whispered and
  have no 48 h claim timer; no web UI, rules/terms/cooldowns markdown settings, SignalR hub or Prometheus gauge.
* Upstream's draw used `RandomNumberGenerator`; here it is the committed-seed derivation above (so it can be audited).
* Upstream recorded winners by display name in a DB table; here a bounded (100) uuid log in kv drives cooldowns.
* Raffles: upstream raffles are driven by action sub-actions with a point-type argument; here they are chat commands paying the community currency.

## Build and test

```bash
make test-superpenguin-giveaway     # xUnit + 90% line-coverage floor
make build-superpenguin-giveaway-bundle   # pinned Dockerfile -> /tmp/superpenguin-giveaway-out/superpenguin_giveaway.wasm
python3 -I scripts/gen-superpenguin-manifests.py giveaway   # regenerate bundle.yaml + hub-manifest.yaml from manifest.src.yaml
```
