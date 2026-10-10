# announce (Python)

`!announce` -- saved per-community announcement messages. Community-scoped, first-party
(`provider: builtin`, `app_id: waddles.core.example.announce`), **inspired by** the general
"mods save a short message under a name, viewers recall it later" shape common to many chat
bots, including superpenguintv (Psychoboy)'s
[PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) -- not a literal port. No
original source code or text is reused; the command grammar, storage shape, and all logic are
written fresh for this bundle. See `bundle.yaml`'s `author`/`notice` fields for the credit, and
contrast `bundles/csharp/superpenguin-roll`, which *is* a line-for-line port and carries the
full verbatim MIT notice because it reuses original code.

**Command-prefix overlap (flagged, not resolved here).** The pre-existing, DB-backed
`core/svc_process/bundles/community_announcements_process.py` /
`core/svc_action/bundles/community_announcements_action.py` script bundles already parse
`!announce publish <announcement_id>` against the legacy `flask_core`/`svc_process`/
`svc_action` runner architecture (not this repo's newer WASI-component `waddle_sdk` bundle SDK
this bundle is built on), to broadcast a web-UI-authored `announcements` DB row. Both this
bundle and that one consume the same `!announce` command-text prefix. This bundle only
recognizes `set`/`remove`/`list`/a bare key -- `!announce publish ...` resolves to a `"usage"`
reply here (an unrecognized verb, same fail-loud rule as every other grammar-legal-but-
unimplemented verb), never an attempt to interpret it as the legacy `publish` command. Surfaced
in this PR description for the migration owner to resolve (e.g. retiring the legacy broadcast
command, or renaming one side) -- not silently papered over.

Uses the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`,
merged in #618) for the `set`/`remove`/`list` verb shapes, extended with a documented bare-key-
read fallback for the grammar's own "unknown option or sub-module" case (mirrors `music`'s own
documented "a positional argument has no slot in the shared grammar" extension).

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!announce <key>` | bare key (not a declared verb) | **Get.** Replies with the saved message for `key`, or a "no saved announcement" reply. Open to any caller. |
| `!announce list` | `list` verb, no args | Replies with every saved key, sorted. Open to any caller. |
| `!announce set <key> <message>` | `set` verb | Broadcaster/moderator only. Creates or overwrites `key`'s saved message (max 500 characters). |
| `!announce remove <key>` | `remove` verb | Broadcaster/moderator only. Deletes `key`'s saved message, if any. |

Any other grammar-legal verb (`add`/`sub`/`enable`/`disable`/`delete`/`reset`) or a malformed
`!announce ...` replies with usage text -- never silently dropped. Announcement keys are
case-insensitive, limited to letters/digits/`_`/`-`, max 32 characters.

```text
!announce set welcome Welcome to the stream!   (mod) -> saved announcement 'welcome'
!announce welcome                               -> Welcome to the stream!
!announce list                                  -> Announcements: welcome
!announce remove welcome                        (mod) -> removed announcement 'welcome'
!announce set rules be kind                     (viewer) -> only moderators/broadcasters can configure !announce
```

Platforms: Twitch + Discord (`chat.message`). Mod/broadcaster gate **fails closed** -- with no
`is_mod`/`is_broadcaster` on the event (Discord today) `set`/`remove` are denied.

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-announce` |
| `storage.kv` | persists the per-community announcement registry |

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. No per-caller state at all (unlike `fish`/`music`): every saved announcement is
channel-wide, so there is no actor-hashing/pseudonym concern here.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `announce.registry` | per-community | none | JSON object (`key -> message`) of every saved announcement. Max 100 entries. |

Colon-free (gh-631) -- `waddle_sdk.kv.validate_key()` (called by every `community_kv`/`kv`
function) rejects any key containing `:`, the real `kv` host capability's own reserved
namespace separator. Individual announcement keys are themselves JSON object keys inside that
one value, not `kv` keys on their own, but are further restricted to a conservative
letters/digits/`_`/`-` charset regardless.

## Feature flag

Gated behind `waddles.command-announce`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap `!announce` command-name match and
before the real grammar resolution.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, attribution metadata |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer, see its own header comment) |
| `src/app.py` | `transform`/`dispatch` -- the full get/set/remove/list logic |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring (see `pyping`'s own) |
| `tests/` | Host-native pytest suite (fake `wit_world`, no wasmtime) -- `kv` faked via the shared `waddle_sdk.testing.FakeKvHost` |

## Build

```bash
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir componentize-py==0.25.1 && \
    componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/announce/src \
      waddle_sdk._component_entry -o /tmp/announce.wasm"
```

Same `componentize-py` invocation `bundles/Dockerfile.core-bundles`'s `python-bundles-builder`
stage uses (driven by `bundles/core-bundles.yaml`), run standalone to confirm this bundle
compiles to a real WASI 0.2 component.

## Test

```bash
cd bundles/python/announce
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`tests/conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build or
SDK install needed. Covered: every verb, set/remove lifecycle, 100-entry cap, mod-gate
fail-closed, corrupt-registry fail-loud (invalid UTF-8 / wrong shape), kv charset (gh-631),
PII-free logs (gh-674).

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.announce`).
