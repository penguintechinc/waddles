# Reputation accrual app bundle (`waddles.core.app.reputation-accrual`)

Default reputation LOGIC bundle (`provides: reputation-logic`), Rust/WASM on `stage-next-tools`.
Turns platform events into reputation deltas and calls the reputation SERVICE bundle's `adjust`
(activity, |delta| <= 5) and `apply_moderation` (|delta| <= 200) tools. The service enforces
soft/hard/tenant floors and bans; this bundle only passes the event `platform`.

Gates: flag `waddles.reputation-accrual` (default OFF); kill switch `waddles.disable-bundle-reputation`.

## Default weights (legacy v2)

| Event | Delta | Notes |
|---|---|---|
| chat message | +1 | 60s per-user cooldown |
| `!command` | +1 | 10s per-user cooldown |
| follow | +1 | |
| subscribe / tier2 / tier3 | +5 / +10 / +20 | clamped to +5 (host activity cap) |
| gift subscription | +3 x max(1, total) | clamped to +5 |
| cheer | +1 per 100 bits | half-away rounding, 0 skips |
| raid | +2 | |
| donation / tip | +1 per dollar | |
| boost | +5 | |
| giveaway / raffle entry | -1 | |
| moderation warn / timeout / kick / ban | -25 / -50 / -75 / -200 | to the moderated user |

Delta = round-half-away-from-zero(weight x multiplier), rounded once. Host `daily_cap_exceeded`,
`quota_exceeded`, `rate_limited` are expected skips (outcome `capped`).

## Config (kv key `accrual`, JSON, per community)

Absent = defaults. Present-but-invalid (unknown key, out-of-range value) = loud ERROR log and skip.
Keys: `weights.*`, `cooldowns.{chat_message,command_usage}` (seconds, 0 disables),
`low_score_notice_at` (450), `low_score_hard_floor` (400).

## Behaviour notes

- Subject user: `identity.resolve_actor` (non-moderation); moderation uses payload `target_user_id`
  / `target_user` / `moderated_user_id` (UUID). Platform-inbound moderation delivery is a known gap.
- Idempotency key: stage `message_id` + `:rep` (hash fallback); replays are host-deduped.
- Low-score WARN once per user per 24h (score <= notice line, above hard floor). No decay, no
  tier-change notifications (legacy had neither).
- Telemetry: counter `reputation_accrual_events{outcome,reason_class}`, histogram
  `reputation_accrual_duration` (ms); metric names use `_` because the host forbids `.`.
  Logs carry outcome labels only, never ids, handles or text.

## Build and test

```bash
cargo test --locked && cargo clippy --all-targets --locked -- -D warnings
cargo component build --release --target wasm32-wasip2 --locked
```
