# birthday (Python)

`!birthday` -- per-user birthdays, **month-day only, no year; set is self-only**. First-party
(`provider: builtin`, `app_id: waddles.core.example.birthday`), kv-only, WASI component.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped, one hashed key per user |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!birthday set <MM-DD>` | the caller, for **themself only** (not mod-gated) | Stores the caller's own birthday; overwrites any previous value. |
| `!birthday` | anyone | Reads the caller's own birthday, or "not set". |
| `!birthday @user` / `!birthday <@id>` | anyone | Reads another user's birthday (public). `<@id>` / `<@!id>` is the Discord mention form. |
| `!birthday clear` | the caller | Removes the caller's own birthday. |
| anything else | anyone | Usage reply -- never silently dropped. |

```text
!birthday set 03-25   -> Birthday saved: March 25 (month and day only).
!birthday             -> Your birthday is March 25.
!birthday @alice      -> @alice's birthday is September 9.
!birthday set 02-30   -> '02-30' is not a real date - February has 29 days at most
!birthday set 13-05   -> '13-05' has an invalid month - the month must be 01-12
!birthday 03-25       -> Usage: !birthday | !birthday set <MM-DD> | ... (forgot `set`)
!birthday clear       -> Your birthday has been cleared.
```

### Date validation

`MM-DD` with two ASCII digits each. Month `01`-`12`; day `01` up to the month's maximum. `02-29`
**is accepted** -- there is no year to rule a leap day out. Every rejection (`02-30`, `04-31`,
`13-05`, `abc`, `3-5`, `1990-03-25`, missing argument) gets an explicit reply and writes nothing.
A bare word without `@` is never treated as a username, so `!birthday 03-25` (forgotten `set`)
replies usage instead of looking up a user called `03-25`.

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | persists each user's own birthday under a hashed key |

## State (kv, community-scoped)

| Key | Scope | Value |
|---|---|---|
| `birthday.<sha256(platform:identity)>` | per-(community, user) | ASCII `MM-DD` (never a year) |

- **Privacy:** no year is ever stored, and the key is a SHA-256 of `platform:identity`
  (identity lower-cased) -- never a raw username. Same `_pseudonym()` scheme as `duel`/`social`.
  The caller's own actor and a typed `@Name` / `<@id>` resolve to the same key.
- Colon-free key (gh-631 -- the `kv` host rejects `:`); the `:` only appears in the hashed input.
- The host scopes every key by tenant/community/app_id, so birthdays are per-community.

## Behavior notes

- **Fail loud.** A `kv` backend error or a corrupt stored value logs `birthday.kv_failure` at
  ERROR and replies "something went wrong"; a corrupt value is never overwritten or masked as
  "not set". No `except -> return default` anywhere.
- **No caller identity** (`actor` is `None`): `set`/`clear`/read-own reply that they can't tell who
  the caller is; reading `@other` still works.
- **PII-free logs.** Only op names, platform and exception types -- never the birthday value,
  the actor, or the `@target`.
- Eager host imports (`kv`, `log`, `relay`) at module top, required for
  componentize-py wizening (`docs/reference/bundle-host-imports.md`).
- `transform()` builds the reply; `dispatch()` is a pure relay.

## Test

```bash
uv venv -p 3.13 .venv        # at the repo/worktree root, never under bundles/
uv pip install --python .venv/bin/python3 -e sdk/waddle-sdk -e libs/flask_core pytest-cov mypy ruff
cd bundles/python/birthday
../../../.venv/bin/python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
../../../.venv/bin/mypy --strict src tests && ../../../.venv/bin/ruff check .
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
