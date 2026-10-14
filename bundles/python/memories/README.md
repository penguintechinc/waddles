# memories (Python)

`!remember <text>` / `!memories [@user]` / `!forget <id|all>` -- per-user remembered facts.
**Per-user, self-managed, public read, 20-item cap.** First-party (`provider: builtin`,
`app_id: waddles.core.example.memories`), kv-only, WASI component.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, one hashed per-user JSON-blob key |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!remember <text>` | anyone (not mod-gated) | Appends `<text>` to the **caller's own** list -> `Remembered as #3 (3/20).` |
| `!memories` | anyone | Lists the caller's own memories: `1. foo \| 2. bar` |
| `!memories @user` / `!memories <@id>` | anyone (**public read**) | Lists that user's memories |
| `!forget <id>` | the owner | Removes one of the **caller's own** memories by the id shown in the list |
| `!forget all` | the owner | Clears the caller's whole list (also recovers a corrupt blob) |

Nobody can add to, edit or delete another user's list -- only read it.

## Rules and limits

| Rule | Value | On violation |
|---|---|---|
| Max memories per user | 20 | explicit reply: "You already have 20 memories - use !forget <id>..." |
| Max text length | 200 chars | explicit "too long" reply |
| Text shape | single printable line (no newline/tab/control chars) | explicit "single line" reply |
| Reply length | 450 chars | list is truncated with `(+N more)` (Twitch's chat limit is 500) |

Ids are stable and monotonic per user (`max(id) + 1`): after `!forget 2` the remaining entries keep
their ids and the next `!remember` gets a fresh one, so `!forget 3` always hits what you were shown
as `3`.

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | persists each user's memories as one hashed JSON blob |

## State

| Key | Scope | Value |
|---|---|---|
| `memories.<sha256(platform:identity)>` | per-user (host scopes by tenant/community/app_id) | JSON `[{"id": int, "text": str, "ts": str}]`, no TTL |

- Colon-free key (gh-631 -- the `kv` host rejects `:`); a raw id/handle never appears in a key.
- `ts` is the inbound event's `occurred_at`. An empty list deletes the key.
- No `db` facade, no SQL migration, no `data.tables`.

## Identity and `@user` resolution

| Platform | Caller identity | `!memories <target>` accepts |
|---|---|---|
| Discord | numeric `author_id`/`user_id` (actor as fallback) | `<@id>` / `<@!id>` mention only -- a bare name cannot be mapped to an id, so it gets an explicit "couldn't identify that user" |
| Twitch / other | case-folded `actor` handle (ids as fallback) | `@name` / `name` handle -- a `<@id>` mention is not resolvable and gets the same explicit reply |

Target resolution is a shape check, not a roster lookup (`kv` has none): a well-formed target that
nobody has written to simply reads as "They have no memories yet."

## Behavior notes

- **Fail loud.** A `kv` backend error logs `memories.kv_failure` at ERROR and replies "Something
  went wrong with memory storage". A stored blob that is not UTF-8 JSON of the expected shape logs
  `memories.state_corrupt` at ERROR and replies that the memories couldn't be read -- it is **never
  treated as empty and never overwritten** (a `!remember` on top would destroy the user's data).
  `!forget all` deletes the key without parsing it, so it is the explicit recovery path.
- **PII-free logs.** Only op names, counts and exception types are logged -- never the memory text,
  the actor or the `@target` (CI: `make check-bundle-hygiene`).
- **Not atomic.** Read-modify-write on the blob can lose one of two *simultaneous* writes from the
  *same* user. Accepted for chat-rate traffic; documented rather than hidden.
- Memory text is user-authored and publicly readable -- do not `!remember` anything private.
- Eager host imports (`kv`, `log`, `relay`) at module top, required for
  componentize-py wizening (`docs/reference/bundle-host-imports.md`).
- The shared `waddle_sdk.command` grammar is not used: it models one command name with a fixed verb
  set, while this bundle owns three command heads with free-text / id / mention arguments.
- `transform()` does the work and builds the reply; `dispatch()` is a pure relay.

## Test

```bash
# from the repo root (venv lives at the worktree root, never under bundles/)
uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python3 -e sdk/waddle-sdk -e libs/flask_core pytest-cov mypy ruff
cd bundles/python/memories
../../../.venv/bin/python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
../../../.venv/bin/mypy --strict src/ tests/
../../../.venv/bin/ruff check . && ../../../.venv/bin/ruff format --check .
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
