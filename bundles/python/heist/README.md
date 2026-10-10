# heist (Python)

`!heist <amount>` -- a timed group gamble. First-party (`provider: builtin`,
`app_id: waddles.core.example.heist`). Concept inspired by superpenguintv (Psychoboy)'s
[PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) heist (Apache-2.0 here,
original implementation -- no source or text reused, same convention as `fish`).

| Command | Behavior |
|---|---|
| `!heist <amount>` | Stake points (debited immediately) to join, or open, the community heist. One stake per player. |
| `!heist` | Show the open heist (crew, pot, time left). |
| `!heist set window <seconds>` | Broadcaster/moderator only; join window `30`-`600`s (default `120`). |

Success chance = 35% + 5% per extra crew member (cap 75%). Success returns 1.5x each stake;
a bust forfeits the pot.

**Lazy resolution:** bundles have no timers, so an expired heist resolves on the first `!heist`
command after the window closes (exactly-once via an atomic kv claim).

## Points store

Same as `gamble`: `loyalty`'s balances are app-scoped `db` rows this bundle cannot reach with
`storage.kv` + `flags.read`, so balances sit behind the `_Ledger` seam (community-scoped,
pseudonymous kv, new players seeded with 100). Replace only `_Ledger` when a platform points
capability exists.

## Logging

PII-free: `op`/`outcome`/`crew_size`/`amount_bucket`/error-case only. Gated by PostHog flag
`waddles.command-heist` (default OFF).

## Tests

`python3 -m pytest` from this directory.
