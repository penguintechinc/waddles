# socials (Python)

`!socials` -- per-community social-media links. Mods set `!socials set twitter https://twitter.com/me`;
anyone reads `!socials` ("Follow us: twitter → <url>, youtube → <url>"). First-party
(`provider: builtin`, `app_id: waddles.core.example.socials`), kv-only, WASI component.
Distinct from the `social` bundle (`!hug`/`!highfive`/`!pat`/`!social stats`).

| | |
|---|---|
| Flag | `waddles.command-socials` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (platform names + URLs) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!socials` / `!socials list` | anyone | Lists every configured link, sorted by platform; "No socials have been set yet." when empty. |
| `!socials set <platform> <url>` | mod/broadcaster | Upserts `platform` -> `url` (`Set ...` on create, `Updated ...` on overwrite). |
| `!socials remove <platform>` | mod/broadcaster | Deletes one link; reports if it doesn't exist. |

Platform names: lowercase letters/digits/`_`/`-` (input is lower-cased), max 32, any name (no
allowlist), at most 20 links per community. URLs: `https://` or `http://`, must have a host, no
`user:pass@`, no control characters, max 300 chars -- anything else is rejected with a clear reply.
Unknown option, wrong argument count or an unimplemented grammar verb replies with usage, never silence.

```text
!socials set twitter https://twitter.com/me   (mod)    -> Set twitter → https://twitter.com/me
!socials set youtube ftp://x                  (mod)    -> links must start with https:// (or http://)
!socials                                      (anyone) -> Follow us: twitter → https://twitter.com/me
!socials remove twitter                       (viewer) -> Only the broadcaster or a moderator can remove social links.
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-socials` |
| `storage.kv` | persists per-community social links and their platform registry |

## State

| Key | Scope | Value |
|---|---|---|
| `socials.registry` | per-community | JSON array of platform names |
| `socials.link.<platform>` | per-community | the URL as UTF-8 text |

Colon-free keys (gh-631) -- the `kv` host rejects `:`. A read costs `1 + N` kv gets. Write order
keeps state consistent on partial failure: `set` writes the link then the registry; `remove`
updates the registry then deletes the link.

## Behavior notes

- **Mod gate fails closed.** `set`/`remove` need a strict `bool` `True` `is_mod` or `is_broadcaster`;
  missing, non-bool (`"true"`, `1`) or false is refused (checked before argument validation). Discord
  does not forward role data today, so Discord mutations are denied.
- **Fail loud.** A `kv` backend error, corrupt registry, or registered platform with no stored link
  logs `socials.kv_failure` at ERROR and replies "something went wrong".
- **PII-free logs.** Only op, platform-name, flags/counts and exception type are logged -- never
  URLs, the actor, or raw message text.
- Business logic lives in `transform()`; `dispatch()` is a pure relay.

## Test

```bash
cd bundles/python/socials
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed. Venv
recipe: `docs/APP_BUNDLE_AUTHORING.md` Appendix (own `.venv`, editable `sdk/waddle-sdk` + `libs/flask_core`).
