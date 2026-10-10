# dadjoke (Python)

`!dadjoke` -- a random family-friendly dad joke from a built-in pool of 14 plus the community's
own custom jokes. First-party (`provider: builtin`, `app_id: waddles.core.example.dadjoke`),
kv-only, WASI component. Built-in jokes are original to Waddles; structural twin of `joke`
with a distinct bank.

| | |
|---|---|
| Flag | `waddles.command-dadjoke` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped; **community is required** (no tenant-wide fallback) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Verb | Who | Behavior |
|---|---|---|---|
| `!dadjoke` | bare | anyone | Posts one random joke; best-effort no immediate repeat (`dadjoke.last`). |
| `!dadjoke list` | `list` | anyone | Lists only the community's custom jokes (`#id: text \| ...`). |
| `!dadjoke add <text>` | `add` | mod/broadcaster | Adds a custom joke (1-300 chars) with a sequential id. |
| `!dadjoke remove <id>` | `remove` | mod/broadcaster | Removes custom joke `#id` (digits only). |

Any other grammar-legal verb, `list` with trailing args, or malformed input replies with usage --
never silently dropped. A pool of exactly one joke still replies.

```text
!dadjoke                         -> I'm reading a book about anti-gravity. It's impossible to put down.
!dadjoke add Why did the chicken... (mod) -> Added dad joke #1 to the pool.
!dadjoke list                    -> Custom dad jokes: #1: Why did the chicken...
!dadjoke remove 1          (mod) -> Removed dad joke #1.
!dadjoke add nope       (viewer) -> only moderators/broadcasters can manage the dad joke pool
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-dadjoke` |
| `storage.kv` | persists custom dad jokes and the last-served joke per community |

## State

| Key | Scope | Value |
|---|---|---|
| `dadjoke.custom.registry` | per-community | JSON `{id: text}` |
| `dadjoke.custom.next_id` | per-community | integer counter (`kv.increment`; ids are never reused) |
| `dadjoke.last` | per-community | ref of the last joke served (`b<n>` / `c<id>`) |

Colon-free keys (gh-631) -- the `kv` host rejects `:`. No per-caller state.

## Behavior notes

- **Mod gate fails closed.** `add`/`remove` need `is_mod`/`is_broadcaster`; absent fields
  (Discord today) are denied, never implicitly allowed.
- **Fail loud.** A `kv` backend error, or a corrupt custom registry (bad JSON / not a
  `str -> str` object), logs `dadjoke.kv_error` at ERROR, replies "temporarily unavailable",
  then raises -- never a silent reset to the built-ins, and corrupt bytes are never overwritten.
- **PII-free logs.** Logs carry command, community id, numeric joke id and the host error text
  -- never the actor or any typed joke text (gh-674).

## Test

```bash
cd bundles/python/dadjoke
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
