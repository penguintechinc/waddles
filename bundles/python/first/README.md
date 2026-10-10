# first (Python)

`!first` -- a per-community, per-UTC-day "who typed first" claim race. The first `!first` of the
day wins; everyone after gets "you're #N to try". First-party (`provider: builtin`,
`app_id: waddles.core.example.first`), kv-only, WASI component. No scheduler: the UTC date is
baked into the day's keys, so a new day is simply a fresh empty window.

| | |
|---|---|
| Flag | `waddles.command-first` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped; **community is required** (no tenant-wide fallback) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Verb / sub-module | Who | Behavior |
|---|---|---|---|
| `!first` | bare | anyone | Claims today (winner is attempt #1) or replies `you're #N to try`. Every call counts as an attempt. |
| `!first list` | `list` | anyone | Today's winner (`user-<8 hex>`) + attempt count; never counts as an attempt. |
| `!first leaderboard` | sub-module | anyone | All-time top 10 claimers by wins (`user-<8 hex> (wins)`). |
| `!first reset` | `reset` | mod/broadcaster | Clears *today's* winner + attempts only; all-time wins/leaderboard untouched. |

Any other grammar-legal verb, `leaderboard` with a trailing verb, or malformed input replies with
usage -- never silently dropped.

```text
!first                  -> 🎉 viewer-1 claimed first today! (win #1 for them)
!first                  (other) -> First was already claimed today -- viewer-2, you're #2 to try. ...
!first list             -> First today: user-3f9a1c2e. 2 tries so far.
!first leaderboard      -> First leaderboard: user-3f9a1c2e (4), user-b81d0aa7 (2)
!first reset            (viewer) -> only moderators/broadcasters can reset !first
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-first` |
| `storage.kv` | persists per-community first-claimer state and the leaderboard registry |

## State

| Key | Scope | TTL | Value |
|---|---|---|---|
| `first.winner.<YYYYMMDD>` | per-community | 3 days | winning pseudonym (SHA-256 of actor) |
| `first.attempts.<YYYYMMDD>` | per-community | 3 days | attempt counter (`kv.increment`) |
| `first.wins.<pseudonym>` | per-community | none | all-time win count |
| `first.leaderboard.registry` | per-community | none | JSON array of every pseudonym that ever won |

Colon-free keys (gh-631) -- the `kv` host rejects `:`. The 3-day TTL is storage hygiene only;
the date token in the key is the window boundary.

## Behavior notes

- **Mod gate fails closed.** `reset` needs `is_mod`/`is_broadcaster`; absent fields (Discord
  today) are denied, never implicitly allowed.
- **Fail loud on backend errors.** A `kv` error logs `first.kv_error` at ERROR (error class
  only), replies "temporarily unavailable", then raises.
- **Corrupt state self-heals, but loudly.** A non-integer counter reads as 0, a corrupt winner
  reads as "no winner", a corrupt leaderboard registry reads as empty -- each logs
  `first.state_corrupt` at ERROR (a display degradation, never a wrong claim).
- **PII-free.** State holds only SHA-256 pseudonyms; replies about *other* people show only a
  short non-reversible handle; logs never include the actor, its pseudonym, or typed text
  (gh-674: usage-error logging once echoed `str(exc)`). The caller's own name appears only in
  their own claim reply.

## Test

```bash
cd bundles/python/first
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
