# wave (Python)

`!wave [target]` -- a wholesome, wave-themed greeting. Stateless chat fun, first-party
(`provider: builtin`, `app_id: waddles.core.example.wave`, `author: PenguinTech/waddles`,
Apache-2.0), **original Waddles content** -- flavor strings written fresh, nothing reused from a
third-party bot. Deliberately distinct from `bundles/python/social` (`!hug`/`!highfive`/`!pat`,
which keep per-user given/received kv counters): `!wave` has **no state, no cooldown, no tally**,
and no `command_prefix` overlap. Structural twin of `bundles/python/slap`.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!wave` | anyone | A solo greeting from the 4-string solo bank ("a big friendly wave goes out to the whole chat"). |
| `!wave <target>` | anyone | A targeted greeting from the 4-template bank, `{target}` substituted. A leading `@` is stripped. |

`<target>` must look like a plausible username: optional `@`, then 1-32 chars of letters,
digits, `_`, `.`, `-`, **starting with a letter/digit/underscore** -- a *shape* check, not a
roster lookup. Anything else (`!wave bob!`, `!wave @@x`, a 33-char name, two words) replies with
the usage line and touches no state -- never silently dropped:

```text
viewer> !wave @bob
bot>    👋 Waves cheerfully at bob!
viewer> !wave a b
bot>    Usage: !wave [target] -- target must look like a plausible username
```

No `waddle_sdk.command` grammar (no verbs / sub-modules): the only argument is one free-text
target. All logic runs in `transform()`; `dispatch()` is a pure relay.

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `flags.read` | Reads the `waddles.command-wave` PostHog flag via `feature_enabled` to gate the command. |

Nothing else: no `storage.kv`/`db` (stateless), no egress. Replies go over the `relay` host
import.

## Feature flag

`waddles.command-wave`, **default OFF** -- checked after the cheap `!wave` match and before target
classification, requested with `default=False` so a flag outage or missing `wit_world` fails
closed. While off the bundle neither replies, draws, nor logs.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` (`command_prefix: ["!wave"]`); the reply is
relayed to the event's own origin platform + channel.

## Logging / PII

The rendered reply may echo the caller-typed target (it returns to the same public channel it came
from), but **log lines carry only `platform` and the resolved `shape`** (`solo` / `targeted` /
`usage`) -- never the target, `event.actor`, or the raw text (enforced repo-wide by
`make check-bundle-hygiene` -> `scripts/ci/check-bundle-source-hygiene.py --check log-pii`).
Regression: `tests/test_backfill.py::test_logs_contain_only_platform_and_shape_never_user_text`.

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest + hub-api install-pipeline manifest |
| `src/app.py` | `transform` (classify + pick) / `dispatch` (relay) |
| `src/_entry_wiring.py` | Static entry wiring (see `pyping`'s) |
| `tests/` | Host-native pytest suite (fake `wit_world`; no wasmtime) |

## Test

```bash
cd bundles/python/wave
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```
