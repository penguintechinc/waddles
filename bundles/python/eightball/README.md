# eightball (Python)

`!8ball [question]` -- a random canned answer from a generic, non-trademarked 17-line bank
(affirmative / non-committal / negative). First-party (`provider: builtin`,
`app_id: waddles.core.example.eightball`), stateless, WASI component. The reference shape the
other stateless command bundles (`boop`, `choose`, ...) are modeled on.

| | |
|---|---|
| Flag | `waddles.command-8ball` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Behavior |
|---|---|
| `!8ball` | Replies `🎱 <answer>` (the question is optional). |
| `!8ball <question>` | Same -- the question's content is never inspected, stored or logged. |

Matched by `^!8ball(?:\s+.*)?$` (case-insensitive); `!8balling` / `!eightball` do not match.
No verbs, so the shared `waddle_sdk.command` grammar is deliberately not used.

```text
!8ball will it rain tomorrow?   -> 🎱 Signs point to yes.
!8ball                          -> 🎱 Ask again later.
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | reads the `waddles.command-8ball` PostHog flag to gate the command |

## Behavior notes

- **Flag fails closed.** Checked only *after* the cheap command match so unrelated events never
  pay a host round trip; queried with `default=False`. Flag OFF = no reply, no action-stage run.
- **PII-free logs.** Only the platform is logged -- never the actor or the question text (gh-674).
- Randomness is stdlib `random` (CPython-on-WASI seeds from `wasi:random`); a game reply, not a
  security decision.

## Test

```bash
cd bundles/python/eightball
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
