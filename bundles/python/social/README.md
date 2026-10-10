# social (Python)

`!hug`/`!highfive`/`!pat <user>` -- fun social interaction commands, plus `!social stats`.
Community-scoped fun content, first-party (`provider: builtin`,
`app_id: waddles.core.example.social`). **Inspired by** superpenguintv (Psychoboy)'s
[PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) "fun"/social command
category -- not a literal port. No original source code or text is reused; the command set,
flavor text, counters, and all dispatch logic are written fresh for this bundle. See
`bundle.yaml`'s `author`/`notice` fields for the credit, and contrast
`bundles/csharp/superpenguin-roll`, which *is* a line-for-line port and carries the full
verbatim MIT notice because it reuses original code.

One shared dispatch path, one registry (`src/app.py::_INTERACTIONS`) -- adding a new
interaction command is a new registry entry plus one `command_prefix` filter in
`bundle.yaml`, not a structural change.

## Commands

| Command | Behavior |
|---|---|
| `!hug <user>` | **Interact.** Posts a random flavor-text reply and increments both the caller's "given" and `<user>`'s "received" `hug` counter. |
| `!highfive <user>` | Same shape as `!hug`, `highfive` flavor text/counters. |
| `!pat <user>` | Same shape as `!hug`, `pat` flavor text/counters. |
| `!hug` / `!highfive` / `!pat` (bare, no target) | Usage reply -- an interaction needs a target, never a silent no-op. |
| `!social stats` | Reads the caller's own given/received tally across *every* registered interaction and replies with one summary line. |

Any other `!social ...` subcommand, or a malformed interaction (e.g. a two-word target),
replies with usage text -- never silently dropped.

### Self-interaction and unknown targets

- `!hug <your-own-name>` (case-insensitive) replies with a dedicated self-interaction message
  (e.g. "you hug yourself. Self-care counts too!") and touches no state.
- A target that doesn't look like a plausible username (`_normalize_target()`'s shape check --
  e.g. a bare `@`, empty string, or disallowed characters) replies with a clear
  "I don't know who that is" message, also touching no state.

Unlike `bundles/python/duel`'s competitive coin-flip, there is **no cooldown** here -- a
hug/high-five/pat has no win/loss stake, so the counters are a harmless novelty tally, not a
resource worth rate-limiting.

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. Per-participant keys use a SHA-256 hash as the pseudonym -- the initiator's from
`event.actor`, the target's from their (validated, case-folded) chat-typed name, since the
inbound event carries no `actor` identity for the opposing participant.

**Keys are colon-free (gh-631)** -- `.` is the namespace separator throughout, never `:` (the
real `kv` host's own reserved separator; `waddle_sdk.kv.validate_key()` now rejects `:`
outright).

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `social.<interaction>.given.<pseudonym>` | per-(community, initiator) | none | Running total of that interaction given (`kv.increment`). |
| `social.<interaction>.received.<pseudonym>` | per-(community, recipient) | none | Running total of that interaction received (`kv.increment`). |

`<interaction>` is one of `hug` / `highfive` / `pat` (`src/app.py::_INTERACTIONS`).

## Deferred to v2 (not stubbed)

**Cross-community leaderboards / global rankings.** Every key above is scoped to one
community (`community_kv`'s own rule: reputation and user-details are the platform's only two
cross-community exceptions, and this bundle is neither). `kv` has no scan/list-keys primitive,
so a leaderboard needs a real `db` capability with an `order_by`/pagination surface -- same
deferral `duel`/`fish` document for their own leaderboards. No `!social leaderboard` command
is declared.

**Real username resolution against a roster.** `_normalize_target()` is a shape check, not a
lookup against an actual community member list -- `kv` has no such roster. A shape-valid but
nonexistent target still resolves as a normal interaction; documented v1 limitation, not a bug.

Also out of scope for this kv-only v1: cooldowns, reaction/combo mechanics, and any
admin-configurable state (no `!hug set ...` -- nothing here needs broadcaster/moderator
configuration).

## Examples

```text
viewer> !hug @bob
bot>    viewer wraps bob in a big warm hug! 🤗
viewer> !hug viewer
bot>    viewer hugs themselves. Self-care counts too! 🤗
viewer> !hug @@oops
bot>    I don't know who '@@oops' is -- try !hug @username.
viewer> !social stats
bot>    Your social stats -- hug 1 given / 0 received, highfive 0 given / 0 received, pat 0 given / 0 received.
```

Targets: optional leading `@`, then 1-32 chars of letters/digits/`_`/`.`/`-`, starting with a
letter/digit/underscore.

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `storage.kv` | Persists per-community social-interaction counters. |
| `flags.read` | Gates the commands behind their `waddles.command-social` feature flag. |

No `db` (`data.tables: []`), no egress, and **no moderator-gated verb** (no cooldown, no admin
config), so there is no mod gate to fail closed.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with
`command_prefix: ["!hug", "!highfive", "!pat", "!social"]`; replies go back to the event's own
origin platform + channel. A `community` context is required (no tenant-wide fallback): without
one `dispatch` logs `social.missing_community` and raises `ValueError`.

## Failure semantics (fail loud)

| Condition | Behavior |
|---|---|
| `kv` backend error | ERROR `social.kv_error` (`op` + exception type only), chat reply "that interaction is temporarily unavailable, try again shortly.", `RuntimeError("social kv <op> failed: <Type>")`. |
| Corrupt given/received counter | ERROR `social.stats_given_corrupt` / `social.stats_received_corrupt` (community only) and read as `0` -- logged loudly, never silent. |
| Missing `channel_id` / unknown command / `interact` without a known interaction + target | `ValueError`. |

## Known limitation: initiator vs. target pseudonyms

The initiator's pseudonym hashes the raw `event.actor`; the recipient's hashes the **lower-cased
typed name**. They coincide for a lower-case platform actor, but a mixed-case actor will not see
`received` counts earned under their lower-cased handle in `!social stats` -- see the module
docstring ("NOT guaranteed to collide"). Resolved once the PII-tokenization pipeline (#429)
supplies opaque actor tokens.

## Logging / PII

Logs carry only `command`, `detail` (`unknown_target`/`self`/`resolved`), `op`, `community`,
`platform` and exception type names -- never the typed target, grammar text or `event.actor`. The
public *reply* may echo the typed target (it returns to the channel it came from); logs and kv keys
may not. Regression:
`tests/test_backfill.py::test_no_log_line_or_kv_key_contains_a_typed_target_or_the_raw_actor`.

## Feature flag

Gated behind `waddles.command-social`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap command-name match and before the
real classification. One flag gates all four commands (`!hug`/`!highfive`/`!pat`/`!social`) --
they ship and roll back together.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, attribution metadata |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer, see its own header comment) |
| `src/app.py` | `transform`/`dispatch` -- the full interaction registry and dispatch |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring (see `pyping`'s own) |
| `tests/` | Host-native pytest suite (shared `waddle_sdk.testing.install_fake_kv_host` fake for `kv`, hand-rolled stand-ins for `flags`/`relay`/`log`/`clock`, no wasmtime) -- `test_app.py` (grammar/interactions) + `test_backfill.py` (target boundaries, counter hygiene, PII, gh-631); 100% line + branch |

## Build

```bash
# `-e HOME=/tmp` + `--user`-scoped pip install: the mapped non-root uid has no writable
# home dir in the stock image otherwise (`pip install` without `--user` fails with
# `PermissionError: '/.local'`) -- confirmed end-to-end against this bundle's own source.
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir --quiet --user componentize-py==0.25.1 && \
    export PATH=\$HOME/.local/bin:\$PATH && \
    componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/social/src \
      waddle_sdk._component_entry -o /tmp/social.wasm"
```

## Test

```bash
cd bundles/python/social
python3 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0 mypy==1.14.1 ruff==0.11.0
# No `pip install -e ../../../sdk/waddle-sdk` needed -- tests/conftest.py puts both the
# bundle's src/ and the SDK's src/ on sys.path directly. Deliberately NOT pip-installing the
# SDK for mypy either: waddle-sdk ships no `py.typed` marker, so an installed copy is invisible
# to strict mode (import-untyped) -- point MYPYPATH at its source tree instead for a clean run.
pytest --cov=src --cov-branch --cov-report=term-missing --cov-fail-under=90   # 100% line + branch
MYPYPATH=../../../sdk/waddle-sdk/src mypy --strict src
ruff check .
```

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.social`).
