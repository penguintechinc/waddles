# love (Python)

`!love <user>` / `!ship <userA> <userB>` -- a fun compatibility-% meter. Community-scoped
fun content, first-party (`provider: builtin`, `app_id: waddles.core.example.love`). Net-new
Waddles content -- not a port of, or inspired by, any external project (contrast
`bundles/python/fish`, which credits a specific inspiration in its own `bundle.yaml`).

Built on the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`,
first adopted by `fish`, #618) for `!love`'s own `list` sub-grammar; `!love <user>` itself falls
back to a single-token target parse when the grammar parser rejects the first token as an
unrecognized verb, the same pattern `duel` uses -- see `src/app.py::_resolve_love()`'s own
docstring for the full rationale and its one documented limitation (a target username that
collides with a reserved verb word can't be paired directly). `!ship <a> <b>` has no grammar of
its own -- it just requires exactly two tokens.

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!love <user>` | single non-verb token | **Pair.** Computes a deterministic compatibility % between the caller and `<user>` and replies with it, updating the caller's own ship history. |
| `!love` | bare, no target | Usage reply -- a pairing needs a target, never a silent no-op. |
| `!love list` | `list` verb, no args | Reads the caller's own ship count + best match. |
| `!ship <userA> <userB>` | exactly two tokens | **Lookup.** Stateless compatibility % between the two named users -- neither needs to be the caller. |
| `!ship` / `!ship <one>` / `!ship <a> <b> <c>` | wrong token count | Usage reply. |

Any other grammar-legal `!love` verb (`set`/`add`/`sub`/`enable`/`disable`/`remove`/`delete`/`reset`)
or a malformed message replies with usage text -- never silently dropped.

### Self-pairing and unknown targets

- `!love <your-own-name>` (case-insensitive) is **not** rejected -- it's a legitimate, deterministic
  100% result ("self-love is important!") and is recorded into the caller's own history like any
  other pairing. This is a deliberate difference from `duel`'s self-challenge rejection, documented
  in `src/app.py`'s module docstring.
- `!ship <a> <a>` (same name twice, case-insensitive) gets the same 100% self-love reply, but writes
  no state (`!ship` is always stateless -- see below).
- A target that doesn't look like a plausible username (`_normalize_target()`'s shape check -- e.g.
  a bare `@`, empty string, or disallowed characters) replies with a clear "I don't know who that is"
  message, touching no state.

## Compatibility algorithm

Deterministic per unordered pair per day -- chosen over random so a result is sharable/repeatable
within a day (asking again gets the identical answer; the next day, a new one). One SHA-256 digest
over the sorted pseudonym pair + the current UTC calendar day (`waddle_sdk.clock`) drives both the
0-100% and the flavor line (5 tiers of 3 lines each, both tier and in-tier line chosen from the same
digest). See `src/app.py::_compute_match()`.

**No cooldown.** Unlike `duel`/`fish`, there is no per-caller rate-limit: the result is a pure
function of its inputs, so repeating the same call costs nothing extra and there's nothing to spam.

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. Per-caller keys use a SHA-256 hash as the pseudonym. **Keys use `.` as their own
internal separator, never `:`** (gh-631) -- `waddle_sdk.kv.validate_key()` rejects any guest key
containing `:`, and this bundle's own tests use the shared, charset-enforcing
`waddle_sdk.testing.install_fake_kv_host()` fake instead of a hand-rolled one, so a regression to a
colon-containing key fails the test suite immediately rather than only in production (the mistake
`duel`/`fish`'s own hand-rolled test fakes let slip past, and `count`/`lurk` had to ship a 1.0.4 to
fix after it reached production).

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `love.ships.<pseudonym>` | per-(community, caller) | none | Running total of `!love <user>` pairings (`kv.increment`). |
| `love.best.<pseudonym>` | per-(community, caller) | none | Highest compatibility % the caller has ever seen via `!love <user>`. |

`!ship <a> <b>` writes **no** state at all -- there is no single caller identity to attribute a
record to (neither named user need be the caller). Deliberate scope boundary, not a missing feature.

## Deferred to v2 (not stubbed)

**Cross-community leaderboards / global rankings.** Every key above is scoped to one community
(`community_kv`'s own rule: reputation and user-details are the platform's only two
cross-community exceptions, and this bundle is neither). `kv` has no scan/list-keys primitive, so
a leaderboard needs a real `db` capability with an `order_by`/pagination surface -- same deferral
`duel`/`fish` document for their own leaderboards. No `!love leaderboard` command is declared.

**Real username resolution against a roster.** `_normalize_target()` is a shape check, not a
lookup against an actual community member list -- `kv` has no such roster. A shape-valid but
nonexistent target still resolves as a normal pairing; documented v1 limitation, not a bug.

## Feature flag

Gated behind `waddles.command-love`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap `!love`/`!ship` command-name match and
before the real grammar classification.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, attribution metadata |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer, see its own header comment) |
| `src/app.py` | `transform`/`dispatch` -- the full game |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring (see `pyping`'s own) |
| `tests/` | Host-native pytest suite (fake `wit_world`, no wasmtime; shared `FakeKvHost` for `kv`) |

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
      componentize -p sdk/waddle-sdk/src -p bundles/python/love/src \
      waddle_sdk._component_entry -o /tmp/love.wasm"
```

## Test

```bash
cd bundles/python/love
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
