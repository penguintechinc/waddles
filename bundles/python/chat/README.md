# chat (Python)

`!chat-history [list]` / `!channels [list]` -- community chat lookup commands. First-party
(`provider: builtin`, `app_id: waddles.core.example.chat`), stateless, WASI component.

**Status: BUILD-ONLY / INERT, and the read path is unavailable by design.** Both commands
recognize, stay flag-gated, and always reply with an explicit "not available from this bundle
yet" message citing [#678](https://github.com/penguintechinc/waddles/issues/678) -- never a
crash, never a silent drop. The data (`hub_chat_messages`) is hub-api-owned; the structured
`waddle_sdk.db` facade only reaches a bundle's own table and no host capability grants a
cross-service read. Cut-over from the `community_chat_process` monolith is a later change.

| | |
|---|---|
| Flag | `waddles.command-chat` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress) |
| Catalog | **not registered** in `bundles/core-bundles.yaml` (inert) |

## Commands

| Command | Verb | Behavior |
|---|---|---|
| `!chat-history` / `!chat-history list` | bare / `list` | Replies the "unavailable, tracked #678" message. |
| `!channels` / `!channels list` | bare / `list` | Same. |

Any other grammar-legal verb (`set`/`add`/...) replies `Usage: !<cmd> [list]`; an unknown option
replies with the parser's usage error. Open to anyone (read-only, no mod gate).

```text
!chat-history       -> Chat lookups aren't available from this bundle yet -- ... Tracked: .../issues/678
!channels list      -> (same)
!chat-history set   -> Usage: !chat-history [list]
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | reads the `waddles.command-chat` PostHog flag to gate the command |

Deliberately no `storage.kv` / `storage.db` -- the bundle uses neither.

## Behavior notes

- **No silent fallback.** The unavailable reply is the honest result of a missing capability;
  tests pin that every recognized invocation produces a non-empty reply and a WARN log.
- **Flag fails closed** (`default=False`); flag OFF = no reply.
- **PII-free logs.** Only the command name (`chat-history`/`channels`) and platform are logged
  -- never the actor, message text, or any typed argument (gh-674).

## Test

```bash
cd bundles/python/chat
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`tests/conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
