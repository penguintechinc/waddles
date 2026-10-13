# duel (Python)

`!duel <user>` -- a 1v1 challenge minigame. Community-scoped fun content, first-party
(`provider: builtin`, `app_id: waddles.core.example.duel`). Net-new Waddles content -- not a
port of, or inspired by, any external project (contrast `bundles/python/fish`, which credits a
specific inspiration in its own `bundle.yaml`).

Built on the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`,
first adopted by `fish`, #618) for the `list`/`set` sub-grammar; `!duel <user>` itself falls back
to a single-token challenge-target parse when the grammar parser rejects the first token as an
unrecognized verb -- see `src/app.py::_resolve_command()`'s own docstring for the full rationale
and its one documented limitation (a target username that collides with a reserved verb word
can't be challenged directly).

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!duel <user>` | single non-verb token | **Challenge.** Rolls a 50/50 winner between the caller and `<user>`, updates both participants' win/loss records, and replies announcing the outcome. Subject to the caller's per-community cooldown. |
| `!duel` | bare, no target | Usage reply -- a challenge needs a target, never a silent no-op. |
| `!duel list` | `list` verb, no args | Reads the caller's own win/loss record. |
| `!duel set cooldown <seconds>` | `set` verb | Broadcaster/moderator only. Sets the community's challenge cooldown, `5`-`3600` seconds (default `30`). |

Any other grammar-legal verb (`add`/`sub`/`enable`/`disable`/`remove`/`delete`/`reset`) or a
malformed `!duel ...` (e.g. a two-word target) replies with usage text -- never silently dropped.

### Self-challenge and unknown targets

- `!duel <your-own-name>` (case-insensitive) replies "you can't duel yourself!" and touches no
  state -- no cooldown consumed, no record changed.
- A target that doesn't look like a plausible username (`_normalize_target()`'s shape check --
  e.g. a bare `@`, empty string, or disallowed characters) replies with a clear
  "I don't know who that is" message, also touching no state.

Neither case consumes the caller's cooldown, so a typo doesn't cost a real challenge attempt.

```text
!duel @penguin           -> ⚔️ viewer-1 challenges penguin to a duel... swords clash in a blur of steel! penguin wins!
!duel @penguin           -> ⚔️ slow down, viewer-1! try again in 27s.
!duel viewer-1           -> you can't duel yourself!
!duel list               -> Record: 3W - 1L.
!duel set cooldown 60    (mod) -> duel cooldown set to 60s
!duel set cooldown 60 (viewer) -> only moderators/broadcasters can configure !duel
```

Platforms: Twitch + Discord (`chat.message`). The mod/broadcaster gate **fails closed** -- with
no `is_mod`/`is_broadcaster` on the event (Discord today) `set cooldown` is denied.

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-duel` |
| `storage.kv` | persists per-community duel state and win/loss tallies |

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. Per-participant keys use a SHA-256 hash as the pseudonym -- the challenger's from
`event.actor`, the target's from their (validated, case-folded) chat-typed name, since the
inbound event carries no `actor` identity for the opposing participant. See `src/app.py`'s module
docstring for the full identity/pseudonymization trade-off.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `duel.lastduel.<pseudonym>` | per-(community, challenger) | `cooldown` seconds | Cooldown gate -- presence + elapsed time decide allow/deny. |
| `duel.wins.<pseudonym>` | per-(community, participant) | none | Running total wins (`kv.increment`). |
| `duel.losses.<pseudonym>` | per-(community, participant) | none | Running total losses (`kv.increment`). |
| `duel.config.cooldown` | per-community | none | Admin-configured challenge cooldown in seconds. |

Colon-free keys (gh-631) -- the `kv` host rejects `:`; `.` only.

## Deferred to v2 (not stubbed)

**Cross-community leaderboards / global rankings.** Every key above is scoped to one community
(`community_kv`'s own rule: reputation and user-details are the platform's only two
cross-community exceptions, and this bundle is neither). `kv` has no scan/list-keys primitive, so
a leaderboard needs a real `db` capability with an `order_by`/pagination surface -- same
deferral `fish` documents for its own leaderboard. No `!duel leaderboard` command is declared.

**Real username resolution against a roster.** `_normalize_target()` is a shape check, not a
lookup against an actual community member list -- `kv` has no such roster. A shape-valid but
nonexistent target still resolves as a normal duel; documented v1 limitation, not a bug.

Also out of scope for this kv-only v1: wagers/stakes, a ranking ladder, and tournaments.

## Feature flag

Gated behind `waddles.command-duel`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap `!duel` command-name match and before
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
# `PermissionError: '/.local'`) -- confirmed end-to-end against this bundle's own source.
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir --quiet --user componentize-py==0.25.1 && \
    export PATH=\$HOME/.local/bin:\$PATH && \
    componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/duel/src \
      waddle_sdk._component_entry -o /tmp/duel.wasm"
```

## Test

```bash
cd bundles/python/duel
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90
MYPYPATH=../../../sdk/waddle-sdk/src mypy --strict src   # waddle-sdk ships no py.typed marker
```

`tests/conftest.py` puts the bundle's `src/` and the SDK's `src/` on `sys.path` directly -- no
SDK install, no WASM build. Covered: every verb, challenge/self/unknown-target/cooldown paths,
mod-gate fail-closed, kv errors fail loud, corrupt-state self-heal logged at ERROR, PII-free
logs (gh-674), kv key charset over every key touched (gh-631).

## Behavior notes

- **Fail loud.** A `kv` backend error logs `duel.kv_error` at ERROR (error class only), replies
  "temporarily unavailable", then raises. Corrupt counters/cooldown state self-heal (0 /
  default) but always log ERROR (`duel.*_corrupt`).
- **PII-free logs.** Logs never include the challenger, the typed `<user>` target, either
  pseudonym, or typed text (gh-674). Names appear only in the public chat reply.

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.duel`). Related: a shared
economy/points capability across bundles is tracked in gh-714; duel deliberately has no wager
or balance today.
