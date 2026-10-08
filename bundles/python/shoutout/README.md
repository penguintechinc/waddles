# shoutout (Python)

`!so <user>` -- a customizable, per-community shoutout command, migrated from the `bot_process`
monolith's `!so` feature. Community-scoped fun/promo content, first-party (`provider: builtin`,
`app_id: waddles.core.example.shoutout`), **inspired by** superpenguintv (Psychoboy)'s
[PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) `!so` -- not a literal port. No
original source code or text is reused; the command grammar, template system, auto-shoutout list,
and sub-module gating are written fresh for this bundle. See `bundle.yaml`'s `author`/`notice`
fields for the credit.

Uses the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`) and is
the first production bundle to use `waddle_sdk.sub_modules.SubModuleGate` for real (not just
illustrative) sub-module gating.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!so <user>` | mod/broadcaster | Posts a shoutout for `<user>`, rendering the community's configured (or default) template. |
| `!so set <template>` | mod/broadcaster | Sets the community's shoutout template. Supports `$(username)`. |
| `!so enable\|disable auto` | mod/broadcaster | Toggles the `auto` sub-module (default OFF). |
| `!so auto add\|remove\|list` | mod/broadcaster | Manages the auto-shoutout list (requires `auto` enabled first). |
| `!so enable\|disable ai` | mod/broadcaster | Toggles the `ai` sub-module (default OFF). **`enable` requires a Professional+ license.** |

Every command except posting a shoutout is config, so all of them (shoutout-posting included) are
restricted to moderators/broadcasters -- fails CLOSED when the normalized event carries neither
`is_mod` nor `is_broadcaster` (e.g. Discord's normalizer today), never an implicit allow. Any
other grammar-legal shape replies with usage text, never a silent drop.

One deliberate grammar extension: `!so <user>` is a bare command plus a single positional
username, which has no slot in the shared grammar (`parse_command`'s bare case takes no argument).
`src/app.py::_resolve()` special-cases a first token that is neither a verb nor a sub-module name
as the shoutout target, before `parse_command` ever sees it -- every other shape delegates to
`parse_command` unchanged. See the module docstring for the full rationale, including why the
chat-invoked command is `!so` while `SubModuleGate`'s kv-namespace string is `"shoutout"`.

## Sub-modules (both default OFF)

- **`auto`** -- manages a per-community auto-shoutout list. Entries are stored as SHA-256
  pseudonyms only (never raw usernames), so `!so auto list` reports a count, not the original
  usernames -- a deliberate PII-tokenization trade-off. Automatically *triggering* a shoutout for
  a listed user (e.g. on raid/join) is not built -- `auto` today is pure list CRUD; see module
  docstring's DO-NOT-BUILD section.
- **`ai`** -- license-gated AI-generated shoutout text. `!so enable ai` requires the tenant's
  license tier to be at least Professional (`waddle_sdk.flask_core.feature_flags.tier_at_least`),
  fails CLOSED on an unavailable/stale tier binding, never env-overridable. The toggle and its
  license gate are real; the actual AI-generation call is a tracked, documented follow-up (not a
  stub): https://github.com/penguintechinc/waddles/issues/626. Until then, `!so <user>` always
  renders the configured template, logging at DEBUG that AI was requested but is pending.

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `shoutout.config.template` | per-community | none | Configured shoutout template (`$(username)` placeholder). |
| `shoutout.auto.list` | per-community | none | JSON array of SHA-256 pseudonyms on the auto-shoutout list. |
| `submodule:shoutout:auto` | per-community | none | `auto` sub-module enabled flag (`waddle_sdk.sub_modules.SubModuleGate`). |
| `submodule:shoutout:ai` | per-community | none | `ai` sub-module enabled flag (same gate). |

## Feature flag

Gated behind `waddles.command-shoutout`, defaulted OFF -- checked in `transform()` after the cheap
`!so` command-name match and before the real grammar resolution.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, attribution metadata |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer, see its own header comment) |
| `src/app.py` | `transform`/`dispatch` -- the full command set |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring (see `pyping`'s own) |
| `tests/` | Host-native pytest suite (fake `wit_world`, no wasmtime) -- 94 tests, 100% `src/app.py` coverage |

## Build

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir --user componentize-py==0.25.1 && \
    PATH=/tmp/.local/bin:\$PATH componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/shoutout/src \
      waddle_sdk._component_entry -o /tmp/shoutout.wasm"
```

Confirmed building a real `.wasm` component today. **Not yet wired into
`bundles/Dockerfile.core-bundles` or `bundles/core-bundles.yaml`** -- catalog registration is a
batched step done separately (see this bundle's PR description).

## Test

```bash
cd bundles/python/shoutout
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../../../sdk/waddle-sdk
pip install pytest==8.3.3 mypy==1.14.1 ruff==0.14.1
pytest --cov=src --cov-report=term-missing
mypy --strict src
ruff check .
```
