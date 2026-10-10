# pyping (Python)

`!pyping` -> `pong (py)` -- the minimal, real Python app bundle: the `componentize-py`
equivalent of `bundles/rust/ping` (Rust) and `bundles/csharp/csping` (C#). First-party,
**original Waddles content** (`provider: builtin`, `app_id: waddles.core.example.pyping`,
`author: PenguinTech/waddles`, Apache-2.0) -- no third-party attribution applies.

It exists as the smallest end-to-end proof that a Python bundle compiles to a WASI 0.2
component against `wit/waddle-bundle/stage.wit`, loads in the bundle executor, and round-trips a
chat message (the `!ping` / `!pyping` / `!csping` "big three" language check). It is also the
**input of the core-bundle reproducible-build gate**
(`.github/workflows/core-bundles-reproducible.yml`; see gh-521 for the
`componentize-py` snapshot non-determinism it tracks), so keep it deliberately tiny.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!pyping` | anyone | Replies `pong (py)` on the event's own origin platform + channel. |

Matching is **exact**: the trimmed message text must equal `!pyping`. No arguments, no case
folding -- `!PYPING`, `!pyping extra`, `!pypingpong` and `! pyping` produce no reply. There is no
verb grammar (`waddle_sdk.command` is intentionally not used), no usage reply, and nothing to
mis-parse.

```text
viewer> !pyping
bot>    pong (py)
```

## Stages

| Stage | Entry | What it does |
|---|---|---|
| `process` (`transform`) | `waddle:bundle/process-stage#transform` | `!pyping` -> reply `PlatformEvent` (`pong (py)`, same platform/channel/actor/timestamp). Anything else -> `None` (dropped, never raises). |
| `action` (`dispatch`) | `waddle:bundle/action-stage#dispatch` | Relays the reply over the `relay` host import to **`envelope.event.platform`** (never a hardcoded provider). A missing `channel_id` raises `ValueError` (fail-loud, nothing relayed). |

## Permissions (V2 structured)

`permissions: []` in both `bundle.yaml` and `hub-manifest.yaml` -- **none requested**:

| Capability | Why not needed |
|---|---|
| `flags.read` | Deliberately **not feature-flagged** -- this is a fixture bundle; a flag would make the "big three" smoke test depend on PostHog. |
| `storage.kv` / `db` | Stateless (`data.tables: []`). |
| egress / `net.http.*` | `egress: []`; replies go over the `relay` queue, never HTTP. |

## Feature flag

None, by design (see above). Every other chat-command bundle in this directory is gated behind
`waddles.command-<name>`; `pyping` is the documented exception.

## Platforms

`consumes`: **Twitch** and **Discord** `chat.message` events with `command_prefix: ["!pyping"]`.
The reply always goes back to the platform the message arrived on (regression-tested for both).

## Logging / PII

The bundle emits **no log lines** and never copies `event.actor` into the relayed payload (only
`{"channel", "text"}` cross the relay boundary) -- covered by
`tests/test_backfill.py::test_dispatch_relay_payload_has_only_channel_and_text`.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer) |
| `src/app.py` | `transform` / `dispatch` / `DispatchResult` |
| `src/_entry_wiring.py` | Hand-authored stand-in for `bundle_compiler`'s `generate_entry_wiring()` |
| `tests/` | Host-native pytest suite (fake `wit_world`; no wasmtime) |

## Test

```bash
cd bundles/python/pyping
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```

`tests/conftest.py` puts `src/` and `sdk/waddle-sdk/src` on `sys.path`, so no package install is
needed.

## Build

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir componentize-py==0.25.1 && \
    componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/pyping/src \
      waddle_sdk._component_entry -o /tmp/pyping.wasm"
```
