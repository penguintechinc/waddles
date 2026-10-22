# defuse (Python)

`!defuse [amount|all]` -- a solo wager game over the shared community economy. First-party
(`provider: builtin`, `app_id: waddles.core.example.defuse`).

> Attribution: inspired by PenguinTwitchBot by SuperPenguinTV (with permission) -- ideas/design
> only, no code copied.

| Command | Behavior |
|---|---|
| `!defuse` / `!defuse <amount>` / `!defuse all` | Stake (default 10; `all` = host `max_bet`); win with `odds`% (default 45) for 2x payout (net `+stake`), else lose the stake. 10s cooldown. |
| `!defuse list` | Your own plays/wins. |
| `!defuse set odds <1-95>` | Broadcaster/moderator only (fail-closed on absent badges). |

Each game settles in one atomic `economy.wager`. Bad/zero stake, insufficient funds, over-cap
and unlinked accounts reply a clear message and move nothing (funds/cap are pre-checked before the
cooldown is claimed, so a refusal never burns it).

## Cooldown and replay safety

- The cooldown is one `kv.increment` on a TTL'd key (`kv.increment` is the only compare-and-swap):
  exactly one of any number of concurrent plays by one actor wins the window. The host re-applies
  the TTL on every call, so a refused attempt restarts the window (the reply says so).
- The pipeline is at-least-once. `transform` forwards the platform `message_id`; `dispatch` records
  it (`waddle_sdk.event_dedupe`, one ring key per actor) after the cooldown and **before the roll,
  the wager and the stat counters**, and skips a redelivery silently (no re-roll, no second wager,
  no double-counted plays/wins, no reply). At-most-once by design: a crash after the claim loses
  one play, never repeats it. A payload with no `message_id` fails loud and moves nothing.

## Permissions (V2)

`storage.kv`, `economy.read`, `economy.wager`, `identity.resolve`,
`chat.send:discord`, `chat.send:twitch`.

## State and privacy

kv keys (`defuse.cd.<p>`, `defuse.plays.<p>`, `defuse.wins.<p>`, `dedupe.defuse.<p>`,
`defuse.config.odds`) use `.` separators (never `:`) with `p = sha256(community:actor_uuid)`. No
usernames, uuids or platform message ids in keys or logs.

## Gating

Activation-gated only -- no feature flag (installed -> enabled -> activated).

## Output safety

`event.actor` is interpolated into replies. The host-minted `{user:<token>}` actor stays live (egress renders the display name); any other value, and every typed echo, goes through `waddle_sdk.safe_text.sanitize_user_text`.
