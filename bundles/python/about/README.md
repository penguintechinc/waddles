# about (Python)

`!about` / `!bot` -- static bot name + version plus a per-community custom blurb. First-party
(`provider: builtin`, `app_id: waddles.core.example.about`), kv-only, WASI component.

| | |
|---|---|
| Flag | `waddles.command-about` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (`community_id=None` = valid tenant-wide sentinel) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Verb | Who | Behavior |
|---|---|---|---|
| `!about` / `!bot` | bare | anyone | Replies `Waddles v3.0 -- <blurb>`; default blurb until one is set. |
| `!about set <text>` | `set` | mod/broadcaster | Stores the blurb (1-300 chars). Blank -> usage; >300 -> length error. |

Any other grammar-legal verb (`add`/`sub`/`list`/`reset`/...) or malformed input replies with
usage -- never silently dropped. `!bot` is a pure alias, normalized onto `!about` before parsing.

```text
!about                      -> Waddles v3.0 -- A modular, multi-platform community bot.
!about set We stream Tue/Thu 8pm   (mod)  -> Updated the about blurb.
!bot                        -> Waddles v3.0 -- We stream Tue/Thu 8pm
!about set nope             (viewer) -> only moderators/broadcasters can set the about blurb
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-about` |
| `storage.kv` | persists the per-community custom blurb |

No egress, no DB tables.

## State

| Key | Scope | TTL | Value |
|---|---|---|---|
| `about.blurb` | per-community | none | UTF-8 blurb text |

Colon-free key (gh-631): the `kv` host rejects `:`; `.` only.

## Behavior notes

- **Mod gate fails closed.** `set` needs `is_mod`/`is_broadcaster` on the event; if neither
  field is present (Discord's normalizer today) it is denied, never implicitly allowed.
- **Fail loud.** A `kv` backend error logs `about.kv_error`, replies "temporarily unavailable"
  to chat, then raises. A corrupt (non-UTF-8) blurb raises instead of falling back to the default.
- **PII-free logs.** Logs carry only command, platform, community id and error text from the
  host -- never the actor, the blurb text, or any typed argument (gh-674).

## Test

```bash
cd bundles/python/about
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
Build: `componentize-py ... -p bundles/python/about/src waddle_sdk._component_entry` (see
`bundles/Dockerfile.core-bundles`, driven by `bundles/core-bundles.yaml`).
