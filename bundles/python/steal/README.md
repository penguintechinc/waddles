# steal (Python)

`!steal @user` -- a risk/reward points steal over the shared community economy. First-party
(`provider: builtin`, `app_id: waddles.core.example.steal`).

> Attribution: inspired by PenguinTwitchBot by SuperPenguinTV (with permission) -- ideas/design
> only, no code copied.

| Command | Behavior |
|---|---|
| `!steal @user` | Attempt a steal: 40% success. Success -> `+loot` (random, bounded by `min(100, target balance)`); fail -> you pay a 10-point penalty to the victim. 60s per-actor cooldown. |
| `!steal list` | Your own attempts/successes. |
| `!steal set cooldown <0-3600>` | Broadcaster/moderator only (fail-closed on absent badges). |

No mention, self-target, an unlinked/ambiguous target, an empty victim or an actor with < 10
points all reply a clear message and move no money.

## Economy caveat

The host's `economy.transfer`/`wager` only move the *invoking actor's* money (`actor_mismatch`
otherwise), so a literal target->actor transfer is impossible by design. Success is therefore
paid as an actor `wager` payout (host-bounded by the daily mint cap) with the victim's balance as
the loot *bound*; the failure penalty is a real `transfer` actor -> victim.

## Permissions (V2)

`storage.kv`, `economy.read`, `economy.wager`, `economy.transfer`,
`identity.resolve`, `chat.send:discord`, `chat.send:twitch`.

## State and privacy

kv keys (`steal.last.<p>`, `steal.attempts.<p>`, `steal.wins.<p>`, `steal.config.cooldown`) use
`.` separators (never `:`) with `p = sha256(community:actor_uuid)`. No usernames in keys or logs.

## Gating

Activation-gated only -- no feature flag (installed -> enabled -> activated).

## Output safety

`event.actor` is interpolated into replies. The host-minted `{user:<token>}` actor stays live (egress renders the display name); any other value, and every typed echo, goes through `waddle_sdk.safe_text.sanitize_user_text`.
