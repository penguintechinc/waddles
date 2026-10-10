# choose (Python)

`!choose a | b | c` -- pick one of the caller's own options at random. First-party
(`provider: builtin`, `app_id: waddles.core.example.choose`), stateless, WASI component.

| | |
|---|---|
| Flag | `waddles.command-choose` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Behavior |
|---|---|
| `!choose a \| b \| c` | Splits on `\|` (options may contain spaces) and replies with one at random. |
| `!choose heads tails` | No `\|` present: splits on whitespace instead. |

Bounds (all fail loud with a reply -- never truncated or dropped): fewer than 2 non-empty options
-> usage; more than `MAX_OPTIONS` (20) -> "too many options"; any option over `MAX_OPTION_LEN`
(200 chars) -> "too long". No verbs: the whole argument is free text, so the shared
`waddle_sdk.command` grammar is deliberately not used (same deviation as `eightball`).

```text
!choose pizza night | movie night   -> movie night
!choose heads tails                 -> heads
!choose onlyone                     -> Usage: !choose option1 | option2 | ... (or space-separated: !choose heads tails)
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | reads the `waddles.command-choose` PostHog flag to gate the command |

All logic runs in `transform()`; `dispatch()` is a pure relay to the event's own platform.

## Behavior notes

- **Flag fails closed** (`default=False`); flag OFF = no reply, same as an unrecognized command.
- **PII-free logs.** Only platform + option *count* are logged -- never the option text, the
  actor, or the raw argument (gh-674). The chosen option goes back to the same public channel.

## Test

```bash
cd bundles/python/choose
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
