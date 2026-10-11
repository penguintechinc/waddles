# boop (Python)

`!boop [target]` -- a lighthearted, harmless flavor-text reply that boops someone on the nose.
First-party (`provider: builtin`, `app_id: waddles.core.example.boop`), stateless, WASI
component. Structural twin of `wave`; flavor bank written fresh for this bundle.

| | |
|---|---|
| Flag | `waddles.command-boop` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Behavior |
|---|---|
| `!boop` | Solo reply (one of 4 canned lines). |
| `!boop <target>` / `!boop @<target>` | Targeted reply (one of 4); a leading `@` is stripped. |

`<target>` must look like a username (`[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}`) and be a single token.
Anything else (`!boop two words`, `!boop !!!bad`, `!boop @`) replies with usage -- never
silently dropped. No verbs: the only argument is the free-text target, so the shared
`waddle_sdk.command` grammar is deliberately not used (same deviation as `eightball`/`wave`).

```text
!boop            -> 🐽 Boops the air. Nobody was there, but the intent was pure.
!boop @penguin   -> 🐽 penguin is booped. Squeak!
!boop two words  -> Usage: !boop [target] -- target must look like a plausible username
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | reads the `waddles.command-boop` PostHog flag to gate the command |

All logic runs in `transform()`; `dispatch()` is a pure relay to the event's own platform.

## Behavior notes

- **Flag fails closed.** Queried with `default=False`; flag OFF is indistinguishable from an
  unrecognized command (no reply).
- **PII-free logs.** Logs carry only platform + reply shape (`solo`/`targeted`/`usage`) --
  never the actor, the target, or any typed token (gh-674). The reply text itself may echo the
  target back into the same public channel; that is the command's purpose.

## Test

```bash
cd bundles/python/boop
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
