# top (Python)

`!top [N]` -- the community's top-N members by economy balance. First-party (`provider: builtin`,
`app_id: waddles.core.example.top`), **read-only**, stateless, WASI component.

Idea inspired by [PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) by SuperPenguinTV
(Psychoboy), used with permission -- ideas/design only, no code copied.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress) |
| Data source | `waddle_sdk.economy.leaderboard(limit)` (host `economy.read`) |
| Catalog | Registered in `bundles/core-bundles.yaml` (`1.0.0`) with `wit_world: stage-next` (economy import) |

## Commands

| Command | Behavior |
|---|---|
| `!top` | Top 5 (`DEFAULT_LIMIT`) |
| `!top <N>` / `!top<N>` | Top N, `1 <= N <= 10` (`MAX_LIMIT`); `!top5`, `!top10` |

Anything else (`!top 0`, `!top 11`, `!top abc`, `!top set`) replies usage. `!topic`/`!toptime` do not match.

```text
!top3 -> Top balances: 1. Alice - 300 | 2. Bob - 120 | 3. Cy - 50
```

**No `toptime`/watchtime variant:** the SDK exposes no watchtime signal and `kv` cannot be scanned,
so only the balance board exists. Add one when a watchtime read capability lands.

## Permissions (V2)

| id | why |
|---|---|
| `economy.read` | `economy.leaderboard` only -- no wager/transfer |
| `chat.send:discord` / `chat.send:twitch` | reply via `relay.push` |

## Privacy

The leaderboard yields community user UUIDs only. Replies embed `{user:<uuid>}` placeholders that
the egress detokenizer renders as display names -- this bundle never sees, stores, or logs a name.
Logs carry op/platform/count/limit/exception type only. A host refusal logs the type, sends a
generic reply, and re-raises (never an empty board).

## Tests

`python3 -m pytest bundles/python/top` (fake `wit_world`: flags/economy/relay/log).
