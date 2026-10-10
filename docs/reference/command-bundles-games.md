# Games and Gambling Commands

Chat games ship as first-party App Bundles. Every command is gated by a `waddles.command-<name>` flag, default OFF. Shared behavior (platforms, flags, mod checks, logging) is in [Command Bundles Reference](command-bundles.md).

## Word and number games

Each community has one active round at a time. State lives in kv and expires one hour after the last update. No player identity is stored.

### `!scramble`

Unscramble a word. Flag: `waddles.command-scramble`.

| Command | Effect |
|---|---|
| `!scramble` or `!scramble start` | Starts a round and shows the scrambled word. If a round is active it re-shows the current word and does not overwrite it. |
| `!scramble word` | Guess. The first correct guess wins and clears the round. A wrong guess adds one attempt. |
| `!scramble giveup` (alias `reset`) | Ends the round and reveals the word. |

Any word other than `start`, `giveup` or `reset` is treated as a guess, so a word that equals another bundle verb is still a guess.

### `!guess`

Guess a secret number from 1 to 100. Flag: `waddles.command-guess`.

| Command | Effect |
|---|---|
| `!guess` or `!guess start` | Starts a round. If one is active it re-shows the current bounds. |
| `!guess n` | Guess. Replies higher or lower and narrows the shown bounds. An exact match wins and clears the round. A non-integer or out-of-range value returns usage and changes nothing. |
| `!guess giveup` (alias `reset`) | Ends the round and reveals the number. |

## Points gambling

`!gamble` and `!heist` bet points. Points only, no real money.

> **Separate balance.** These bundles hold their own balance in community kv. They do **not** read or spend the `!points` balance of the `loyalty` bundle, because the platform has no cross-bundle points capability yet. New players start with 100 points. Do not present the two balances as one currency.

### `!gamble`

Bet points on a win probability. Flag: `waddles.command-gamble`.

| Command | Effect | Who |
|---|---|---|
| `!gamble amount` | Bet `amount` points. Win with probability `odds` percent for `+amount`, otherwise lose `amount`. | Anyone |
| `!gamble all` | Bet the whole balance. | Anyone |
| `!gamble` | Show your balance. | Anyone |
| `!gamble set odds N` | Set the win percentage, 1-95 (default 45). | Moderator or broadcaster |

A per-user cooldown of 10 seconds applies by default. If concurrent bets drive a balance negative it is reset to 0 rather than left as debt.

### `!heist`

Timed group gamble. Flag: `waddles.command-heist`.

| Command | Effect | Who |
|---|---|---|
| `!heist amount` | Stakes `amount` points and joins, or opens, the community's heist. The stake is taken immediately. One stake per player per heist. | Anyone |
| `!heist` | Shows the open heist (crew size, pot, time left) or how to start one. | Anyone |
| `!heist set window seconds` | Sets the join window, 30-600 seconds (default 120). | Moderator or broadcaster |

Success chance is 35% plus 5% per extra crew member, capped at 75%. On success each player gets `stake x 1.5` back (net +0.5x stake). On a bust all stakes are lost. The crew is capped at 200.

> **A heist resolves late.** Bundles have no timers. A heist resolves on the first `!heist` command after its join window ends, and that reply carries the result. In an idle channel an expired heist sits unresolved until someone next types `!heist`. If that command was a join attempt, the caller is told to start a fresh heist.

The heist concept is inspired by superpenguintv (Psychoboy)'s PenguinTwitchBot. The implementation is original.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `!scramble`/`!guess` shows an old round | Round is still active (expires 1 hour after its last update). | `giveup` or `reset` to end it. |
| `!gamble set odds` refused | Platform did not report moderator/broadcaster role (fails closed). | Run as the broadcaster. |
| Heist result never appears | No `!heist` command since the window closed. | Type `!heist`. |
