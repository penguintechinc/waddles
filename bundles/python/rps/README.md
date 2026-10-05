# rps (Python)

`!rps <rock|paper|scissors>` -- play one round of rock-paper-scissors against the bot.
Community-scoped fun content, first-party (`provider: builtin`, `app_id:
waddles.core.example.rps`). Net-new Waddles content -- not a port of, or inspired by, any
external project (contrast `bundles/python/fish`, which credits a specific inspiration in its own
`bundle.yaml`).

Built on the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`,
first adopted by `fish`, #618) for the `list`/`set` sub-grammar; `!rps <choice>` itself falls back
to a single-token move parse when the grammar parser rejects the first token as an unrecognized
verb -- same fallback shape `duel` (#628) uses for its own challenge-target argument. See
`src/app.py::_resolve_command()`'s own docstring for the full rationale.

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!rps <rock\|paper\|scissors>` (or `r`/`p`/`s`, case-insensitive) | single non-verb token | **Play.** Rolls a uniformly random bot move, replies win/lose/tie, and updates the caller's W/L/T record. Subject to the caller's per-community cooldown. |
| `!rps` | bare, no move | Usage reply -- a play needs a move, never a silent no-op. |
| `!rps list` | `list` verb, no args | Reads the caller's own win/loss/tie record. |
| `!rps set cooldown <seconds>` | `set` verb | Broadcaster/moderator only. Sets the community's play cooldown, `0`-`3600` seconds (default `10`). |

Any other grammar-legal verb (`add`/`sub`/`enable`/`disable`/`remove`/`delete`/`reset`), an
unrecognized move (e.g. `!rps lizard`), or a malformed `!rps ...` (e.g. a two-word move) replies
with usage text -- never silently dropped. An unrecognized move does not consume the caller's
cooldown, so a typo doesn't cost a real play.

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. The caller's key uses a SHA-256 hash of `event.actor` as the pseudonym (same
rationale as `fish`/`duel`'s own `_pseudonym()`). See `src/app.py`'s module docstring for the full
identity/pseudonymization trade-off.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `rps:lastplay:<pseudonym>` | per-(community, caller) | `cooldown` seconds | Cooldown gate -- presence + elapsed time decide allow/deny. |
| `rps:wins:<pseudonym>` | per-(community, caller) | none | Running total wins (`kv.increment`). |
| `rps:losses:<pseudonym>` | per-(community, caller) | none | Running total losses (`kv.increment`). |
| `rps:ties:<pseudonym>` | per-(community, caller) | none | Running total ties (`kv.increment`). |
| `rps:config:cooldown` | per-community | none | Admin-configured play cooldown in seconds. |

## Deferred to v2 (not stubbed)

**Cross-community leaderboards / global rankings.** Every key above is scoped to one community
(`community_kv`'s own rule: reputation and user-details are the platform's only two
cross-community exceptions, and this bundle is neither). `kv` has no scan/list-keys primitive, so
a leaderboard needs a real `db` capability with an `order_by`/pagination surface -- same deferral
`fish`/`duel` document for their own leaderboards. No `!rps leaderboard` command is declared.

Also out of scope for this kv-only v1: best-of-N matches, wagers/stakes, and a ranking ladder.

## Feature flag

Gated behind `waddles.command-rps`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap `!rps` command-name match and before
the real grammar classification.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, attribution metadata |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer, see its own header comment) |
| `src/app.py` | `transform`/`dispatch` -- the full game |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring (see `pyping`'s own) |
| `tests/` | Host-native pytest suite (fake `wit_world`, no wasmtime) |

## Build

```bash
# `-e HOME=/tmp` + `--user`-scoped pip install: the mapped non-root uid has no writable
# home dir in the stock image otherwise (`pip install` without `--user` fails with
# `PermissionError: '/.local'`) -- confirmed end-to-end against duel's own source.
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir --quiet --user componentize-py==0.25.1 && \
    export PATH=\$HOME/.local/bin:\$PATH && \
    componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/rps/src \
      waddle_sdk._component_entry -o /tmp/rps.wasm"
```

## Test

```bash
cd bundles/python/rps
python3 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==6.0.0 mypy==1.14.1 ruff==0.14.1
# No `pip install -e ../../../sdk/waddle-sdk` needed -- tests/conftest.py puts both the
# bundle's src/ and the SDK's src/ on sys.path directly. Deliberately NOT pip-installing the
# SDK for mypy either: waddle-sdk ships no `py.typed` marker, so an installed copy is invisible
# to strict mode (import-untyped) -- point MYPYPATH at its source tree instead for a clean run.
pytest --cov=src --cov-report=term-missing --cov-fail-under=90
MYPYPATH=../../../sdk/waddle-sdk/src mypy --strict src
ruff check .
```

## Activation

**Not yet registered in `bundles/core-bundles.yaml`.** This bundle is built standalone, awaiting
batched catalog registration alongside other in-flight command bundles -- intentionally out of
scope for this PR (see PR description).
