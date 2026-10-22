# blackjack (Python)

`!blackjack <stake>` / `!hit` / `!stand` -- a solo game of blackjack against the dealer-bot over
the shared community economy. First-party (`provider: builtin`,
`app_id: waddles.core.example.blackjack`).

| Command | Behavior |
|---|---|
| `!blackjack <stake>` / `!blackjack all` | Deal a hand (`all` = the host's `max_bet`, capped by the community max). |
| `!hit` / `!stand` | Draw a card / let the dealer play. 21 stands automatically. |
| `!blackjack` | Show the open hand, collect a won-but-unpaid hand, or print usage. |
| `!blackjack set minbet\|maxbet\|timeout <n>` | Broadcaster/moderator only (strict `is True` badges; absent or stringy badges deny). |

Rules: infinite deck (each draw independent, `random.SystemRandom`), dealer stands on every 17,
natural 21 pays 3:2 (rounded down; a dealer natural against a player natural is a push), no
split/double/insurance. Defaults: bets 1-100 (hard ceiling 500 = the manifest's `max_bet`),
abandoned hand forfeited after 180 s (30-3600 configurable).

## Where it runs (verified against the host)

`economy` and `identity` are served by the **process** stage only (`core/svc_process/src/
capabilities.rs` routes `economy.*` / `identity.*` db-ops to dedicated handlers; `core/svc_action/
src/capabilities.rs:2263-2267` sends the same ops to `handle_db`, whose first act is a
`storage.tables` gate, so they are unavailable there). `relay` is **action**-stage only (the
process stage hard-denies it). So every game step runs in `transform`, and `dispatch` only relays
the reply `transform` built. The tests model this stage split and fail if a capability is used from
the wrong stage.

> **Heads-up for the existing economy bundles.** `defuse` / `steal` / `jackpot` / `top` call
> `economy.*` and `identity.*` from `dispatch` (the action stage). Per the code above those calls
> cannot be served there; they are worth a real-path check on alpha (not changed here).

## Economy permissions and the mint cap

`storage.kv`, `economy.read`, `economy.wager` (`params.max_bet: 500`), `identity.resolve`,
`chat.send:discord`, `chat.send:twitch`. No `economy.transfer` (the game never pays a third
party). The mint/burn economy has no house account and `wager` always debits `stake >= 1` and
credits `payout <= 100 x stake`, so settlement is built around that:

- **Escrow at the deal.** `stake - reserve` is burned (`wager(actor, stake - reserve, 0)`); the
  player keeps `reserve = ceil(stake * 2.5 / 100)` points (1..13). Settling only at the end would
  let a player move their stake away mid-hand and dodge every loss.
- **Settlement** is `wager(actor, reserve, total_return)` with `total_return` = `0` (loss), `stake`
  (push), `2 x stake` (win) or `stake + stake * 3 // 2` (natural). Net = `total_return - stake`,
  and the payout counts against the daily mint cap like any other (`DeniedError quota_exceeded`).
  The reserve covers the host's `payout <= 100 x stake` rule for every stake 1..500 (tested).
- The only thing a player can still dodge is the reserve (<= 2.6% of the stake): spending it away
  just makes the *winnings* unpayable.
- **At-most-once.** The hand record is deleted BEFORE the settling wager: a crash loses a payout
  and can never repeat one. A refusal the host guarantees did not apply (reserve spent, cap,
  daily mint cap, denied) re-stores the hand as `due` (24 h) and `!blackjack` collects it; a
  `BackendError` (may or may not have committed) is never retried -- logged at ERROR, the reply
  says the payout status is unclear.

## Idempotency and concurrency

- The pipeline is at-least-once. `transform` claims the inbound `message_id` through
  `waddle_sdk.event_dedupe` (ONE atomic TTL'd `kv.increment`) after the read-only prechecks and
  before any stake, state change or payout; a redelivery is dropped silently, an event older than
  `event_dedupe.MAX_EVENT_AGE_SECONDS` is dropped by the age fence, and a missing `message_id` /
  `occurred_at` fails loud. Needs the host kv-quota fix `4ad3eaae` (per-event markers), as the
  other economy bundles do.
- `kv.increment` is the only compare-and-swap, so a per-player lock (increment on a 15 s TTL key,
  released after the step) serializes deal/hit/stand and the hand is re-read under it: two
  concurrent `!hit`s cannot both draw (the loser is told its last move is still processing).
  Tested with genuinely interleaved coroutines and mutation-checked.
- Abandoned hands: kv TTL = timeout + 60 s; a later action past `expires_ms` forfeits it (the
  stake was already burned -- no money moves) and a new deal in the same message is allowed.

## State and privacy

kv keys (`blackjack.hand.<p>`, `blackjack.lock.<p>`, `blackjack.seen.<d>`, `blackjack.cfg.minbet|
maxbet|timeout`) use `.` separators (never `:`) with `p = sha256(community:actor_uuid)` and
`d = sha256(platform|message_id)[:32]`. Everything is TTL'd except the three mod settings, so no
per-player key outlives its hand. Logs carry `op` / `outcome` / amount bucket / error-class
fields only -- never a handle, uuid, card, message id or text.

## Output safety

`event.actor` goes through `_actor_label` (host-minted `{user:<token>}` stays live, anything else
through `waddle_sdk.safe_text.sanitize_user_text`). Replies otherwise carry only integers, card
glyphs and static text; grammar errors are never echoed.

## Gating

Activation-gated only -- no feature flag (installed -> enabled -> activated).

## Registration (not done in this change)

Needs a `bundles/core-bundles.yaml` row with **`wit_world: stage-next`** (it declares
`economy.*` / `identity.resolve`; a `stage` build has no such import and the build script fails):

```yaml
  - app_id: waddles.core.example.blackjack
    version: "1.0.0"
    language: python
    wit_world: stage-next
    source_dir: python/blackjack
    manifest_path: blackjack.manifest.yaml
    artifact_path: blackjack.wasm
    activation_targets:
      - tenant_slug: global
        community_id: null
```

`economy.wager` is a `dangerous` permission the seeder auto-grants tenant-wide at activation
(same product decision as the other economy bundles).

## Follow-ups

- Split / double-down / insurance; a multi-deck shoe with a persisted cut card.
- A host `economy.credit` (mint without a debit) would remove the reserve trick.
- Other bundles still use the module-level `random` generator; a componentize-py snapshot seeds it
  once at import, so `random.SystemRandom` (as used here) is the safe pattern.
