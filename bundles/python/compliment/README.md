# compliment (Python)

`!compliment [@user]` -- a random, family-friendly compliment addressed to the caller or a named
user, from a built-in pool of 14 plus the community's own custom entries. First-party
(`provider: builtin`, `app_id: waddles.core.example.compliment`), kv-only, WASI component.
Built-in lines are original to Waddles (no third-party text).

| | |
|---|---|
| Flag | `waddles.command-compliment` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (`community_id=None` = valid tenant-wide sentinel) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Verb | Who | Behavior |
|---|---|---|---|
| `!compliment` | bare | anyone | `<caller>, <compliment>` (falls back to `friend` if no actor). |
| `!compliment <user>` / `@<user>` | positional | anyone | Same, addressed to `<user>` (1-32 chars `[A-Za-z0-9_.-]`; one `@` stripped). |
| `!compliment add <text>` | `add` | mod/broadcaster | Adds a custom compliment (1-300 chars, sequential id) to the pool. |

Best-effort no immediate repeat (`compliment.last`); a pool of one still replies. Any other
grammar-legal verb, a multi-word target, or a malformed command replies with usage -- never
silently dropped. The bare `<user>` shape is the one documented grammar extension (the shared
parser has no positional-argument slot): a first token that is not a verb is the target.

```text
!compliment            -> viewer-1, you light up every room you walk into!
!compliment @penguin   -> penguin, your kindness never goes unnoticed.
!compliment add you rock   (mod) -> Added compliment #1 to the pool.
!compliment add nope    (viewer) -> only moderators/broadcasters can manage the compliment pool
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-compliment` |
| `storage.kv` | persists custom compliments and the last-served compliment per community |

## State

| Key | Scope | Value |
|---|---|---|
| `compliment.custom.registry` | per-community | JSON `{id: text}` |
| `compliment.custom.next_id` | per-community | integer counter (`kv.increment`) |
| `compliment.last` | per-community | ref of the last compliment served (`b<n>` / `c<id>`) |

Colon-free keys (gh-631) -- the `kv` host rejects `:`. No per-caller state is stored.

## Behavior notes

- **Mod gate fails closed.** `add` needs `is_mod`/`is_broadcaster`; absent fields (Discord
  today) are denied, never implicitly allowed.
- **Fail loud.** A `kv` backend error, or a corrupt custom registry (bad JSON / not a
  `str -> str` object), logs `compliment.kv_error` at ERROR, replies "temporarily unavailable"
  to chat, then raises -- never a silent reset to the built-ins.
- **PII-free logs.** The caller and `<user>` handle appear only in the chat reply (that is the
  command's purpose); logs carry command, community id, compliment id and the exception *class*
  only -- never the actor, target or typed text (gh-674: the grammar-error log once echoed the
  raw remainder).

## Test

```bash
cd bundles/python/compliment
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
