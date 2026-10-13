# donate (Python)

`!donate` -- show, set or clear this community's donation (tip) link. First-party
(`provider: builtin`, `app_id: waddles.core.example.donate`), kv-only, WASI component. A
community that wants `!tip` aliases it with the existing `alias` bundle (one prefix per bundle).

| | |
|---|---|
| Flag | `waddles.command-donate` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (one key: the link) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!donate` | anyone | Shows `Support the stream: <url>`, or that no link is set yet. |
| `!donate set <url>` | mod/broadcaster | Validates and stores the link (overwrites any previous one). |
| `!donate reset` | mod/broadcaster | Clears the link (idempotent). |
| `!donate <anything else>` | anyone | Replies with usage -- never silently dropped. |

```text
!donate                          -> No donation link is set yet. A mod can add one with !donate set <url>
!donate set https://ko-fi.com/me (mod) -> Donation link set: https://ko-fi.com/me
!donate                          -> Support the stream: https://ko-fi.com/me
!donate set https://x.com/a      (viewer) -> Only the broadcaster or a moderator can change the donation link.
!donate set ftp://x.com/a        (mod) -> That link must start with https:// or http://
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
| `storage.kv` | persists the per-community donation link |
| `flags.read` | gates the command behind `waddles.command-donate` |

## State

| Key | Scope | Value |
|---|---|---|
| `donate.url` | per-community | the validated URL, UTF-8 |

Colon-free key (gh-631 -- the `kv` host rejects `:`). Host scopes by tenant/community/app_id.
No actor identity is stored.

## Behavior notes

- **Mod gate fails closed.** `set`/`reset` need a real `bool` `True` in `is_mod` or
  `is_broadcaster`; missing, `False`, or any non-bool (e.g. the string `"false"`, Discord today)
  is refused with an explicit reply and `donate.denied` is logged. The gate runs before URL
  validation and before any `kv` access. Bare `!donate` needs no badge.
- **Fail loud.** A `kv` backend error or a corrupt stored value logs `donate.kv_failure` at
  ERROR and replies "something went wrong"; a corrupt value is never overwritten.
- **PII-free logs.** Only op names, reason codes and exception types -- never the URL, message
  text or the actor.
- Eager host imports (`kv`, `log`, `relay`, `feature_enabled`) at module top, required for
  componentize-py wizening (`docs/reference/bundle-host-imports.md`).
- `transform()` builds the reply; `dispatch()` is a pure relay.

## Test

```bash
uv venv -p 3.13 .venv                     # at the repo/worktree root, never under bundles/
uv pip install --python .venv/bin/python3 -e sdk/waddle-sdk -e libs/flask_core pytest pytest-cov mypy ruff
cd bundles/python/donate
../../../.venv/bin/python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
../../../.venv/bin/python3 -m mypy --strict src tests
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
