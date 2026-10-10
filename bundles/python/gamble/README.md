# gamble (Python)

`!gamble <amount>` -- a single-user points bet. First-party (`provider: builtin`,
`app_id: waddles.core.example.gamble`), net-new Waddles content (no attribution required).
Points only; no real-money ties.

| Command | Behavior |
|---|---|
| `!gamble <amount\|all>` | Bet points: win `+amount` with probability `odds`% (default 45), else lose `amount`. 10s per-caller cooldown. |
| `!gamble` | Show your balance. |
| `!gamble set odds <1-95>` | Broadcaster/moderator only; sets the community win percentage. |

## Points store

`loyalty`'s `!points` balances live in a `db` table + kv index scoped to that app, and this
bundle only holds `storage.kv` + `flags.read`, so it cannot read or write them (no cross-bundle
points capability exists yet). Balances sit behind the `_Ledger` seam in `src/app.py`
(community-scoped, pseudonymous kv, new players seeded with 100 points). When a platform points
capability lands, only `_Ledger` changes.

## Logging

PII-free: `op`/`outcome`/`amount_bucket`/error-case only -- never actor, username or message.
Gated by PostHog flag `waddles.command-gamble` (default OFF).

## Tests

`python3 -m pytest` from this directory (host-native, fake `wit_world`).
