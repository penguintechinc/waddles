# merch (Python)

`!merch` -- show, set or clear this community's merch store link. First-party
(`provider: builtin`, `app_id: waddles.core.example.merch`), kv-only, WASI component. A
community can alias another name with the existing `alias` bundle (one prefix per bundle).

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (one key: the link) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!merch` | anyone | Shows `Merch store: <url>`, or that no link is set yet. |
| `!merch set <url>` | mod/broadcaster | Validates and stores the link (overwrites any previous one). |
| `!merch reset` | mod/broadcaster | Clears the link (idempotent). |
| `!merch <anything else>` | anyone | Replies with usage -- never silently dropped. |

```text
!merch                          -> No merch store link is set yet. A mod can add one with !merch set <url>
!merch set https://merch.example.com/me (mod) -> Merch store link set: https://merch.example.com/me
!merch                          -> Merch store: https://merch.example.com/me
!merch set https://x.com/a      (viewer) -> Only the broadcaster or a moderator can change the merch store link.
!merch set ftp://x.com/a        (mod) -> That link must start with https:// or http://
```

## URL validation

Every rule rejects with an explicit reply naming the reason; nothing is silently coerced.

| Rule | Reject reply says |
|---|---|
| empty | `That link is empty.` |
| > 512 characters | `That link is too long (max 512 characters).` |
| whitespace, control or non-printable chars, `\` | `...contains spaces or control characters.` |
| scheme not `http`/`https` | `...must start with https:// or http://` |
| embedded `user:pass@` (or any `@` in the authority) | `...must not contain a username or password.` |
| host not a well-formed dotted domain (ASCII labels, no `_`, no empty/oversized labels) | `...needs a valid host name...` |
| IP-address host | `...needs a domain name, not an IP address.` |
| non-numeric / out-of-range port, malformed brackets | `...is not a well-formed URL.` |

The stored value is re-validated on read; a corrupt value fails loud (below).

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | persists the per-community merch store link |

## State

| Key | Scope | Value |
|---|---|---|
| `merch.url` | per-community | the validated URL, UTF-8 |

Colon-free key (gh-631 -- the `kv` host rejects `:`). Host scopes by tenant/community/app_id.
No actor identity is stored.

## Behavior notes

- **Mod gate fails closed.** `set`/`reset` need a real `bool` `True` in `is_mod` or
  `is_broadcaster`; missing, `False`, or any non-bool (e.g. the string `"false"`, Discord today)
  is refused with an explicit reply and `merch.denied` is logged. The gate runs before URL
  validation and before any `kv` access. Bare `!merch` needs no badge.
- **Fail loud.** A `kv` backend error or a corrupt stored value logs `merch.kv_failure` at
  ERROR and replies "something went wrong"; a corrupt value is never overwritten.
- **PII-free logs.** Only op names, reason codes and exception types -- never the URL, message
  text or the actor.
- Eager host imports (`kv`, `log`, `relay`) at module top, required for
  componentize-py wizening (`docs/reference/bundle-host-imports.md`).
- `transform()` builds the reply; `dispatch()` is a pure relay.

## Test

```bash
uv venv -p 3.13 .venv                     # at the repo/worktree root, never under bundles/
uv pip install --python .venv/bin/python3 -e sdk/waddle-sdk -e libs/flask_core pytest pytest-cov mypy ruff
cd bundles/python/merch
../../../.venv/bin/python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
../../../.venv/bin/python3 -m mypy --strict src tests
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
