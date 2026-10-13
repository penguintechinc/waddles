# pronouns (Python)

`!pronouns` -- per-user pronouns: set your own, read your own, read someone else's, clear.
First-party (`provider: builtin`, `app_id: waddles.core.example.pronouns`), kv-only, WASI component.

| | |
|---|---|
| Flag | `waddles.command-pronouns` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, one hashed key per user, community-scoped by the host |
| Catalog | registered in `bundles/core-bundles.yaml` |

> **`set` is self-only and deliberately NOT mod-gated.** Pronouns are the user's own profile:
> the bundle never reads `is_mod`/`is_broadcaster`, and there is no command to set or clear
> anyone else's. Reading another user's pronouns is public.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!pronouns` | anyone | Your own pronouns, or a "you haven't set pronouns" hint. |
| `!pronouns set <text>` | anyone (own only) | Stores your pronouns; replies with the stored value. |
| `!pronouns @user` / `!pronouns user` | anyone | Reads that user's pronouns (Twitch handle). |
| `!pronouns <@id>` / `<@!id>` | anyone | Reads that user's pronouns (Discord mention). |
| `!pronouns clear` (`remove`/`delete`/`reset`) | anyone (own only) | Removes your pronouns. |
| anything else | anyone | Replies with usage -- never silently dropped. |

Validation for `set <text>` (explicit reply on rejection; the rejected text is never echoed):

| Rule | Limit |
|---|---|
| Not empty | after trimming |
| Length | at most 32 characters |
| Charset | printable, single line |
| Forbidden | `@`, `<`, `>` (a stored value is echoed to other users -- no `@everyone`/mention injection) |

Grammar via `waddle_sdk.command` (`CommandSpec(name="pronouns")`); `clear` is matched by the bundle
itself because it is not an SDK verb. A Twitch handle that equals a reserved verb (`list`, `add`,
`enable`, ...) cannot be looked up directly.

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | persists each user's pronouns under a hashed per-user key |
| `flags.read` | gates the command behind `waddles.command-pronouns` |

## State

| Key | Scope | Value |
|---|---|---|
| `pronouns.<sha256 hex>` | per-community, per-user | the pronouns text, UTF-8, no TTL |

The hash input is platform-scoped and identical for storage and lookup:

| Platform | Basis hashed (`sha256`) |
|---|---|
| Discord | `discord:<author_id>` (caller's `author_id`, or the id in a `<@id>` mention) |
| Other (Twitch, ...) | `<platform>:name:<lowercased handle>` (caller's `actor`, or the typed `@name`) |

The raw handle/id is hashed and dropped -- never stored, logged, or put in a key. Colon-free keys
(gh-631 -- the `kv` host rejects `:`).

## Behavior notes

- **Per-community.** The host scopes `kv` by tenant/community/app_id, so a user sets their pronouns
  once per community (a bundle cannot write the platform's cross-community user-details store).
- **Discord typed names are not resolved.** `!pronouns @name` on Discord replies "use a real
  @mention" -- name -> id needs identity tokenization (#429); no hashed guess, no silent default.
- **Fail loud.** A `kv` backend error or a corrupt stored value logs `pronouns.kv_failure` at ERROR
  and replies "something went wrong"; stored data is never overwritten on a failed/corrupt read.
- **PII-free logs.** Only op names, command kinds, reason codes and exception types -- never the
  pronouns text, the actor, or a typed target.
- Eager host imports (`kv`, `log`, `relay`, `feature_enabled`) at module top, required for
  componentize-py wizening (`docs/reference/bundle-host-imports.md`).
- `transform()` runs the command and builds the reply; `dispatch()` is a pure relay.

## Test

```bash
uv venv -p 3.13 .venv   # at the repo/worktree root, never under bundles/
uv pip install --python .venv/bin/python3 -e sdk/waddle-sdk -e libs/flask_core pytest-cov mypy ruff
cd bundles/python/pronouns
../../../.venv/bin/python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
../../../.venv/bin/mypy --strict src tests
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
