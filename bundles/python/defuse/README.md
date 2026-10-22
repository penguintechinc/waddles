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
and unlinked accounts reply a clear message and move nothing.

## Permissions (V2)

`storage.kv`, `economy.read`, `economy.wager`, `identity.resolve`,
`chat.send:discord`, `chat.send:twitch`.

## State and privacy

kv keys (`defuse.last.<p>`, `defuse.plays.<p>`, `defuse.wins.<p>`, `defuse.config.odds`) use `.`
separators (never `:`) with `p = sha256(community:actor_uuid)`. No usernames in keys or logs.

## Gating

Activation-gated only -- no feature flag (installed -> enabled -> activated).

## Output safety

`event.actor` is interpolated into replies. The host-minted `{user:<token>}` actor stays live (egress renders the display name); any other value, and every typed echo, goes through `waddle_sdk.safe_text.sanitize_user_text`.
