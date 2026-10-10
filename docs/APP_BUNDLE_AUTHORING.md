# App Bundle Authoring Guide (FROZEN)

**Status: FROZEN.** This is the canonical spec a fleet of porting agents follows verbatim to
port v2.x waddlebot features into v3 App Bundles. Codified from the stable exemplar bundles +
the typed pipeline contract as of `release/v3.0.X` commit `c1b5f04a`. Do not deviate without
updating this doc first.

> **Terminology — read first.** Everything this guide calls a "bundle" below is a
> **built-in stage handler**: a plain Python module under
> `core/svc_ingest/builtin_handlers/`, `core/svc_process/builtin_handlers/` or
> `core/svc_action/builtin_handlers/` that the stage-runner imports **in-process** (the stored
> entrypoint is `builtin_handlers.<module>:<function>`) and that ships with that service's
> image. These are **not installable app bundles**. Installable (WASI component) app bundles
> live under the top-level `bundles/` tree, are authored with `sdk/waddle-sdk/AUTHORING.md`
> (or the Rust/JS/C# SDKs), and run in the bundle executor. The package is named
> `builtin_handlers`, not `builtins`, because a package called `builtins` can never be
> imported (it collides with Python's own `builtins` module).

Sources of truth (read these, not memory, if this doc and code ever disagree):
- `libs/flask_core/flask_core/stream_pipeline.py` — `PlatformEvent`, `StageEnvelope`, `EnvelopeError`, `bundle_stream_key`, `BUNDLE_STAGES`
- `libs/flask_core/flask_core/stage_runner.py` — `BundlePoller`, `BundleDistribution`, `load_entrypoint`, `EntrypointLoadError`
- `libs/flask_core/flask_core/bundle_runtime.py` — `set_bundle_dal`/`get_bundle_dal`, `bundle_context`/`get_bundle_context`, `BundleContext`, `BundleRuntimeError`
- `core/svc_ingest/runner.py`, `core/svc_process/runner.py`, `core/svc_action/runner.py`
- `core/svc_process/app.py`, `core/svc_action/app.py` — DAL construction + `set_bundle_dal()` startup wiring
- `hub_api/services/distribution_service.py`
- `libs/waddle_transports/`

**Chat-command grammar, sub-modules, per-community data scoping:** see
`sdk/waddle-sdk/AUTHORING.md` — covers `waddle_sdk.command.parse_command()`'s standard
`!<command> [sub-module] [option] <input>` grammar, `waddle_sdk.sub_modules.SubModuleGate`
(default-OFF sub-modules), and `waddle_sdk.community_kv` (community-scoped state). Addendum,
not a FROZEN-status change — the pipeline/entrypoint contract below is unaffected.

---

## 1. Model Overview

A **bundle** is `waddles.<module>.<feature>.<name>` — one `app_catalog.app_id` implementing
1–3 pipeline stages (`ingest`, `process`, `action`). Each stage is a pure Python entrypoint
(`module:function`, resolved via `importlib`, never `exec()`) polled and driven by that stage's
runner container. Stages communicate only via the typed `StageEnvelope`/`PlatformEvent`
contract over per-bundle Valkey list keys — never raw dicts, never a shared queue.

```
external event                      hub-api (control plane)
      │                             ┌─────────────────────────────┐
      ▼                             │ GET /api/v1/distribution/    │
┌─────────────┐   raw dict          │   bundles?stage=ingest        │◄── polled every
│  receiver / │──LPUSH──►  :ingest  │ (app_catalog + activations)   │    ~5s by each
│  webhook    │            (Valkey) └─────────────────────────────┘    stage runner
└─────────────┘                 │
                                 ▼ RPOP + normalize()
                          ┌─────────────┐   StageEnvelope[PlatformEvent]
                          │ svc-ingest  │───────LPUSH────────► :process (Valkey)
                          └─────────────┘                            │
                                                                      ▼ RPOP + transform()
                                                              ┌──────────────┐  StageEnvelope | None
                                                              │ svc-process  │────LPUSH─────► :action (Valkey)
                                                              └──────────────┘                     │
                                                                                                     ▼ RPOP + <name>()
                                                                                             ┌─────────────┐
                                                                                             │ svc-action  │──► external platform API
                                                                                             └─────────────┘   + action_dispatch_log audit
```

Per-bundle Valkey isolation key (`flask_core.stream_pipeline.bundle_stream_key`):

```
waddles:t:{tenant}:c:{community|_tenant}:app:{app_id}:{ingest|process|action}
```

`community=None` (tenant-wide activation) renders as the literal `_tenant` segment, never
omitted. Each stage owns exactly one Valkey key per bundle per (tenant, community) — ingest
RPOPs its own `:ingest` key and LPUSHes onto `:process`; process RPOPs `:process` and LPUSHes
onto `:action`; action RPOPs `:action` and terminates the pipeline (dispatch + audit log, no
further LPUSH).

---

## 2. Entrypoint Contract Per Stage (verified against the runners)

All three signatures below are frozen. A bundle script MUST match them exactly — the runner
calls positionally/by-keyword as documented and treats any shape mismatch as a hard failure
(`EnvelopeError` / `EntrypointLoadError` / a caught-and-skipped exception, never silent
coercion).

### ingest

```python
async def normalize(raw: dict) -> PlatformEvent:
```

Verified: `core/svc_ingest/runner.py::_normalize_and_enqueue` calls `await normalize_fn(raw_event)`
where `raw_event = json.loads(raw)` (whatever your receiver pushed onto `:ingest`), and requires
the return value to be a `flask_core.PlatformEvent` instance — anything else raises
`EnvelopeError` and the one bad event is skipped (`ingest.normalize_failed`, logged, loop
continues). Raise `ValueError` for a malformed raw event.

### process

```python
async def transform(event: PlatformEvent) -> PlatformEvent | None:
```

Verified: `core/svc_process/runner.py::_transform_and_enqueue` calls
`await transform_fn(event_in)` where `event_in = envelope_in.event`. **`None` is a first-class,
supported return value** meaning "no reply" — the runner logs `process.no_reply` and drops the
event; nothing is enqueued onto `:action`. Use this for bundles that only respond to specific
commands/keywords and must not echo every message back. Raise `ValueError` for a malformed
event.

### action

```python
async def <name>(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
```

Verified: `core/svc_action/runner.py::_handle_envelope`'s `_attempt()` closure calls
`await entrypoint_fn(envelope, bundle.config, http_client=self._http_client)` and requires the
return value to be a `waddle_transports.TransportResult` instance — anything else (or any
unclassified exception) is wrapped `NonRetryableTransportError` by the runner itself. `<name>`
is whatever the bundle's own function is named (`send_message`, etc.) — `app_catalog.stages.
action.entrypoint` names it, not a fixed function name.

The action entrypoint gets the **full envelope** (tenant/community/ts), not just the event —
richer than ingest/process's pure-transform contract — because a real external dispatch needs
audit context and must report a classified success/failure via typed exceptions:
`RetryableTransportError` (transient: 429, 5xx, network/timeout — `runner.py`'s own
`retry_with_backoff` owns all backoff timing, an action bundle NEVER sleeps itself) or
`NonRetryableTransportError` (permanent: bad config, 401/403, other 4xx, SSRF-guard rejection).

### Where message data lives

| What | Where |
|---|---|
| Reply text (in, and out) | `event.payload["text"]` |
| Reply-in-place target channel | `envelope.event.payload["channel_id"]` (Discord) / `["channel_name"]` (Twitch) — **primary**; `config["channel_id"]`/`config["channel"]` is a fallback only, for a proactive/scheduled send with no triggering event |
| Platform-specific fields | `event.payload[...]` — never a second nesting; `envelope.event.payload[...]`, never `envelope.payload[...]` |
| Secrets/tokens | Never in `config` directly — `config["<name>_token_ref"]` names an **env var**, resolved at dispatch time via `waddle_transports.signing.resolve_secret(ref) -> str`; raises `SecretResolutionError` (wrap as `NonRetryableTransportError`) if unset |

**Never** access `envelope.payload` (doesn't exist — the field is `event`, deliberately renamed
from a legacy `payload` to make payload-under-payload double-nesting structurally impossible).
**Never** pass a raw `dict` between stages — only `PlatformEvent`/`StageEnvelope` instances (or
their `.to_dict()`/`.from_dict()` JSON serialization at the Valkey boundary, which the runners
already handle — a bundle script never touches JSON directly).

---

## 3. Registering a Bundle (migration convention)

One row in `app_catalog`, keyed by `app_id` (`waddles.<module>.<feature>.<name>`), carries every
stage's `{entrypoint, config, spec}` triple in a single `stages JSONB` column:

```json
{
  "ingest":  {"entrypoint": "builtin_handlers.<mod>:<fn>", "config": {}, "spec": {}},
  "process": {"entrypoint": "builtin_handlers.<mod>:<fn>", "config": {}, "spec": {}},
  "action":  {"entrypoint": "builtin_handlers.<mod>:<fn>", "config": {...defaults...}, "spec": {"required_config": [...]}}
}
```

- `entrypoint`: dotted `module:function`, resolved via `importlib` inside that stage's own
  container image (`builtin_handlers/<file>.py` must already be installed there — never DB-stored code,
  never `exec()`).
- `config`: the bundle's own non-secret shipped defaults only (e.g. `api_base`). Per-activation
  values (`channel_id`, `*_token_ref`) belong in `app_tenant_availability.config_defaults` /
  `app_activations.config` (migration 069's 3-tier precedence: activation > tenant availability >
  bundle default), **never seeded in this migration**.
- `spec.required_config`: list of config keys the bundle needs from an activation before it can
  run — documentation for whoever activates it, not runtime-enforced by the runner itself.

File naming: next sequential number in `config/postgres/migrations/`, e.g. `085_<short_desc>.sql`
(check `ls config/postgres/migrations/ | sort -n | tail -1` for the current head before picking a
number — the sequence is a flat integer prefix, not namespaced per feature).

`ON CONFLICT (app_id) DO NOTHING` for a brand-new `app_id` (idempotent across re-applies).
If a stage is being ADDED to an `app_id` a prior migration already created, use
`ON CONFLICT (app_id) DO UPDATE SET stages = app_catalog.stages || EXCLUDED.stages` (top-level
JSONB merge — only safe when the new stage keys don't overlap the existing ones).

Activate the bundle so `hub_api/services/distribution_service.py::list_bundles_for_stage`
actually serves it to a poller — declaring `stages` alone is not enough:

```sql
-- Tenant-wide (every community under this tenant can use it)
INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'waddles.<module>.<feature>.<name>', TRUE
FROM tenants t WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;

-- OR community-scoped (one specific community only) — use app_activations instead:
-- INSERT INTO app_activations (community_id, tenant_id, app_id, enabled, config)
-- VALUES (<community_id>, <tenant_id>, 'waddles.<module>.<feature>.<name>', TRUE, '{}'::jsonb)
-- ON CONFLICT (community_id, app_id) DO NOTHING;
```

### Filled-in example migration

```sql
-- Migration 085: seed the <feature> action bundle.
--
-- Ported from action/pushing/<platform>_action_module (v2). Follows the
-- `builtin_handlers.<module>:<function>` entrypoint convention established by
-- migration 071 (demo echo) and 082 (Discord send). Config carries only
-- non-secret defaults; per-activation channel/token_ref supplied at
-- activation time (migration 069's 3-tier precedence). No token is ever
-- stored in this table.
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

INSERT INTO app_catalog (
    app_id, manifest_version, module, feature, provider, execution_model,
    is_default, platform_compatibility, status, stages
) VALUES (
    'waddles.bot.<platform>.default',
    '1.0.0',
    'bot',
    'waddles.bot.<platform>',
    'builtin',
    'native',
    FALSE,
    '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
    'active',
    (
        '{"action": {"entrypoint": "builtin_handlers.<platform>_send_action:send_message", ' ||
        '"config": {}, "spec": {"required_config": ["channel_id", "bot_token_ref"]}}}'
    )::jsonb
)
ON CONFLICT (app_id) DO NOTHING;

INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'waddles.bot.<platform>.default', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
```

---

## 4. Copy-Paste Skeletons

### 4a. Minimal ingest handler — `core/svc_ingest/builtin_handlers/<name>_ingest.py`

```python
"""<Platform> ingest bundle -- normalizes a raw fanned-out <platform> event.

Consumes the raw event shape this container's own receiver LPUSHes onto
this bundle's `:ingest` Valkey key and produces a `flask_core.
PlatformEvent`, the frozen stage-to-stage contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flask_core import PlatformEvent


async def normalize(raw: dict[str, Any]) -> PlatformEvent:
    """Normalize one raw <platform> event to a `PlatformEvent`.

    Raises `ValueError` on a malformed raw event -- the ingest runner
    catches this per-event so one bad event never kills the poll loop.
    """
    text = raw.get("text")
    author_id = raw.get("author_id")
    if not isinstance(text, str) or not text:
        raise ValueError("raw <platform> event missing required 'text' string field")
    if not author_id or not isinstance(author_id, str):
        raise ValueError("raw <platform> event missing required 'author_id' string field")

    return PlatformEvent(
        platform="<platform>",
        event_type="message",
        actor=raw.get("author_username") or author_id,
        payload={
            "text": text.strip(),
            "channel_id": raw.get("channel_id"),
            "author_id": author_id,
        },
        occurred_at=raw.get("occurred_at") or datetime.now(UTC).isoformat(),
    )
```

```python
"""Tests for `builtin_handlers.<name>_ingest.normalize`."""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent

from builtin_handlers.<name>_ingest import normalize


class TestNormalize:
    async def test_normalizes_valid_raw_event(self) -> None:
        raw = {"text": "  hello  ", "author_id": "555", "channel_id": "42"}
        result = await normalize(raw)
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "hello"
        assert result.payload["channel_id"] == "42"

    async def test_missing_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text"):
            await normalize({"author_id": "555"})

    async def test_missing_author_id_raises(self) -> None:
        with pytest.raises(ValueError, match="author_id"):
            await normalize({"text": "hi"})
```

### 4b. Process handler (command + no-reply branch) — `core/svc_process/builtin_handlers/<name>_process.py`

```python
"""<Feature> process bundle -- responds to a command, drops ordinary chatter.

`None` is a first-class "no reply" outcome (`core/svc_process/runner.py`)
-- ordinary messages are dropped, not echoed back.
"""

from __future__ import annotations

import dataclasses

from flask_core import PlatformEvent

_COMMAND_PREFIX = "!<command>"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Reply only to `!<command>`; return `None` for everything else.

    Raises `ValueError` on a malformed event -- the process runner
    catches this per-event so one bad event never kills the poll loop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    if not text.strip().startswith(_COMMAND_PREFIX):
        return None  # no reply -- ordinary chatter, not this bundle's concern

    reply_text = "<command response text>"
    return dataclasses.replace(
        event,
        payload={**event.payload, "text": reply_text},
    )
```

```python
"""Tests for `builtin_handlers.<name>_process.transform`."""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent

from builtin_handlers.<name>_process import transform


def _event(text: str) -> PlatformEvent:
    return PlatformEvent(
        platform="<platform>",
        event_type="message",
        actor="penguin",
        payload={"text": text, "channel_id": "chan-1"},
        occurred_at="2026-01-01T00:00:00+00:00",
    )


class TestTransform:
    async def test_command_gets_a_reply(self) -> None:
        result = await transform(_event("!<command>"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == "<command response text>"

    async def test_ordinary_chatter_returns_none(self) -> None:
        assert await transform(_event("just chatting")) is None

    async def test_preserves_channel_id_on_reply(self) -> None:
        result = await transform(_event("!<command>"))
        assert result is not None
        assert result.payload["channel_id"] == "chan-1"

    async def test_missing_text_raises(self) -> None:
        event = PlatformEvent(
            platform="<platform>", event_type="message", actor=None,
            payload={}, occurred_at="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="text"):
            await transform(event)
```

### 4c. Action/send handler — `core/svc_action/builtin_handlers/<name>_send_action.py`

```python
"""<Platform> send-message ACTION bundle -- real <Platform> API call, SSRF-guarded."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from flask_core import StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError, TransportResult
from waddle_transports.signing import SecretResolutionError, resolve_secret
from waddle_transports.url_guard import SSRFError, guarded_request

_DEFAULT_API_BASE = "https://api.<platform>.example/v1"


async def send_message(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Reply in-place: send `envelope.event.payload["text"]` to the resolved channel.

    Raises `NonRetryableTransportError` for a config/auth failure and
    `RetryableTransportError` for a rate-limit/5xx/network error --
    `retry_with_backoff` (runner.py) owns all backoff timing, this bundle
    never sleeps itself.
    """
    event_payload = envelope.event.payload
    payload_channel_id = event_payload.get("channel_id")
    config_channel_id = config.get("channel_id")
    channel_id = (
        payload_channel_id
        if isinstance(payload_channel_id, str) and payload_channel_id
        else (config_channel_id if isinstance(config_channel_id, str) and config_channel_id else None)
    )
    if channel_id is None:
        raise NonRetryableTransportError(
            "<platform> bundle could not resolve a channel_id from either "
            "envelope.event.payload['channel_id'] (reply-in-place) or "
            "config['channel_id'] (fallback)"
        )

    token_ref = config.get("api_token_ref")
    if not isinstance(token_ref, str) or not token_ref:
        raise NonRetryableTransportError("<platform> bundle config missing required 'api_token_ref'")

    text = event_payload.get("text")
    if not isinstance(text, str) or not text:
        raise NonRetryableTransportError("action envelope event.payload missing required 'text' string")

    try:
        token = resolve_secret(token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"<platform> token resolution failed: {exc}") from exc

    api_base = config.get("api_base", _DEFAULT_API_BASE)
    url = f"{api_base}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"text": text}

    try:
        response = await guarded_request(http_client, "POST", url, headers=headers, json=body)
    except SSRFError as exc:
        raise NonRetryableTransportError(f"<platform> API URL rejected by SSRF guard: {exc}") from exc
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        raise RetryableTransportError(f"<platform> API request failed: {exc}") from exc

    if response.status_code == 429:
        raise RetryableTransportError("<platform> API rate limited", http_status=429)
    if response.status_code in (401, 403):
        raise NonRetryableTransportError(
            f"<platform> API rejected auth: HTTP {response.status_code}", http_status=response.status_code,
        )
    if 400 <= response.status_code < 500:
        raise NonRetryableTransportError(
            f"<platform> API returned client error: HTTP {response.status_code}", http_status=response.status_code,
        )
    if response.status_code >= 500:
        raise RetryableTransportError(
            f"<platform> API returned server error: HTTP {response.status_code}", http_status=response.status_code,
        )

    return TransportResult(
        transport="bundle",
        detail=f"<platform> message sent, channel={channel_id}",
        http_status=response.status_code,
    )
```

```python
"""Tests for `builtin_handlers.<name>_send_action.send_message`."""

from __future__ import annotations

import httpx
import pytest
from flask_core import PlatformEvent, StageEnvelope
from waddle_transports import NonRetryableTransportError, RetryableTransportError

from builtin_handlers.<name>_send_action import send_message


def _envelope(payload: dict | None = None) -> StageEnvelope:
    default_payload = {"text": "hello", "channel_id": "123"}
    return StageEnvelope(
        tenant="1", community="42", app_id="waddles.bot.<platform>.default", stage="action",
        event=PlatformEvent(
            platform="<platform>", event_type="message", actor=None,
            payload=payload if payload is not None else default_payload,
            occurred_at="2026-08-31T12:00:00Z",
        ),
        ts="2026-08-31T12:00:00Z",
    )


def _config(**overrides: object) -> dict:
    base = {"api_token_ref": "TEST_<PLATFORM>_TOKEN", "api_base": "https://8.8.8.8/v1"}
    base.update(overrides)
    return base


def _client(handler) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def test_sends_real_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_<PLATFORM>_TOKEN", "s3cr3t")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"id": "1"})

    async with _client(handler) as client:
        result = await send_message(_envelope(), _config(), http_client=client)
    assert captured["auth"] == "Bearer s3cr3t"
    assert result.transport == "bundle"


async def test_missing_channel_id_is_non_retryable() -> None:
    async with _client(lambda r: httpx.Response(200)) as client:
        with pytest.raises(NonRetryableTransportError, match="channel_id"):
            await send_message(_envelope(payload={"text": "hi"}), _config(), http_client=client)


async def test_429_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_<PLATFORM>_TOKEN", "s3cr3t")
    async with _client(lambda r: httpx.Response(429)) as client:
        with pytest.raises(RetryableTransportError):
            await send_message(_envelope(), _config(), http_client=client)
```

---

## 5. Accessing the Database / Shared State

Multiple bundles need DB access (fetch a stored quote, mark a user welcomed, tally poll votes),
but the frozen stage entrypoints (§2) carry no DAL parameter — `process`'s `transform(event)`
doesn't even receive the envelope. `flask_core.bundle_runtime` (`set_bundle_dal`/`get_bundle_dal`,
`bundle_context`/`get_bundle_context`) is the one sanctioned side channel — **never** invent a
second one (a module-level `set_dal()` per bundle, reading `event.payload["community_id"]` as a
tenant/community workaround, etc.). Entrypoint *signatures* stay frozen; a stateful bundle simply
calls these two accessors from inside its own body.

**A bundle never calls `set_bundle_dal()`/`bundle_context()` itself** — that's the stage runner's
job (`core/svc_process/app.py`/`core/svc_action/app.py` call `set_bundle_dal()` once at startup;
`core/svc_process/runner.py`/`core/svc_action/runner.py` wrap every entrypoint call in
`bundle_context()`, once per envelope). A bundle only ever **reads**:

```python
from flask_core import get_bundle_dal, get_bundle_context

dal = get_bundle_dal()          # the process-wide AsyncDAL this stage runner bound at startup
ctx = get_bundle_context()      # ctx.tenant / ctx.community / ctx.app_id -- THIS envelope's scope
```

`get_bundle_context()` is why this matters for `process` specifically: `transform(event)` never
receives the envelope, so `ctx.tenant`/`ctx.community` is the *only* correct way to scope a DB
query — never `event.payload["community_id"]` or any other payload field (payload is
platform-supplied, unscoped data; the envelope's tenant/community come from the Valkey key this
bundle's own runner popped it off, the actual isolation boundary — security.md Tenant Isolation).
Action bundles already receive the full `envelope` as a parameter, so `get_bundle_context()` is
mostly a convenience there, kept for a single accessor shape across both stages.

Both raise `flask_core.BundleRuntimeError` with a clear message if called before the runner has
wired anything (e.g. svc-ingest, which has no DAL at all — it's a pure-transform stage and never
calls `set_bundle_dal()`).

### Worked example: a stateful process bundle

A demo/illustrative bundle only (not a real `app_id`) — tallies a per-user counter and replies
with the caller's running total, reading+writing its own table.

**Migration** (`config/postgres/migrations/0NN_dice_leaderboard_demo.sql` — next sequential
number, §3's convention):

```sql
-- Migration 0NN: dice_leaderboard demo table -- worked example only
-- (docs/APP_BUNDLE_AUTHORING.md 'Accessing the database / shared state'),
-- not a real bundle. Illustrates the shape a genuinely stateful process
-- bundle's own migration takes: its own table, FK'd to tenants/
-- communities, never a PII column (backend-database.md PII Tokenization
-- -- `platform_user_id` is the platform's own opaque ID, not sourced from
-- the `users` identity table).

CREATE TABLE IF NOT EXISTS dice_leaderboard (
    id               SERIAL PRIMARY KEY,
    tenant_id        INTEGER NOT NULL REFERENCES tenants(id),
    community_id     INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    app_id           TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    natural_20_count INTEGER NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, community_id, app_id, platform_user_id)
);
```

**Bundle** (`core/svc_process/builtin_handlers/dice_leaderboard_process.py` shape):

```python
"""Dice leaderboard PROCESS bundle -- demo/example only, not a real bundle.

Illustrates `flask_core.get_bundle_dal()`/`get_bundle_context()` for a
stateful process bundle: tallies a running per-user counter of natural-20
dice rolls (`!roll20`) and replies with the caller's running total. See
docs/APP_BUNDLE_AUTHORING.md, 'Accessing the database / shared state'.
"""

from __future__ import annotations

import dataclasses

from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal

_COMMAND = "!roll20"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Increment and report the caller's natural-20 tally; `None` for anything else.

    Raises `ValueError` on a malformed event -- the process runner catches
    this per-event so one bad event never kills the poll loop.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")
    if text.strip() != _COMMAND:
        return None  # no reply -- not this bundle's command

    platform_user_id = event.payload.get("author_id")
    if not platform_user_id or not isinstance(platform_user_id, str):
        raise ValueError("event payload missing required 'author_id' string field")

    ctx = get_bundle_context()  # tenant/community/app_id of THIS envelope -- never event.payload
    dal = get_bundle_dal()

    rows = await dal.execute(
        "INSERT INTO dice_leaderboard "
        "(tenant_id, community_id, app_id, platform_user_id, natural_20_count) "
        "VALUES ((SELECT id FROM tenants WHERE slug = $1), $2, $3, $4, 1) "
        "ON CONFLICT (tenant_id, community_id, app_id, platform_user_id) "
        "DO UPDATE SET natural_20_count = dice_leaderboard.natural_20_count + 1 "
        "RETURNING natural_20_count",
        [
            ctx.tenant,
            int(ctx.community) if ctx.community else None,
            ctx.app_id,
            platform_user_id,
        ],
    )
    total = rows[0]["natural_20_count"]

    reply = f"\U0001f3b2 nat 20! that's #{total} for you in this community."
    return dataclasses.replace(event, payload={**event.payload, "text": reply})
```

**Test** — injects a fake in-memory DAL via `set_bundle_dal()` and enters `bundle_context()`
manually, exactly mirroring what the real runner does around every entrypoint call:

```python
"""Tests for `builtin_handlers.dice_leaderboard_process.transform` -- demo only."""

from __future__ import annotations

from typing import Any

import pytest
from flask_core import (
    PlatformEvent,
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)

from builtin_handlers.dice_leaderboard_process import transform


class _FakeDal:
    """In-memory stand-in for `AsyncDAL` -- implements only the `.execute()` surface this bundle uses."""

    def __init__(self) -> None:
        self._counts: dict[tuple[Any, ...], int] = {}

    async def execute(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        key = tuple(params[1:])  # (community_id, app_id, platform_user_id)
        self._counts[key] = self._counts.get(key, 0) + 1
        return [{"natural_20_count": self._counts[key]}]


@pytest.fixture(autouse=True)
def _dal() -> Any:
    fake = _FakeDal()
    set_bundle_dal(fake)
    yield fake
    reset_bundle_dal_for_tests()


def _event(text: str) -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="message",
        actor="penguin",
        payload={"text": text, "author_id": "u-1"},
        occurred_at="2026-01-01T00:00:00+00:00",
    )


class TestTransform:
    async def test_first_roll_reports_count_one(self) -> None:
        with bundle_context(tenant="acme-corp", community="42", app_id="waddles.demo.dice.default"):
            result = await transform(_event("!roll20"))
        assert result is not None
        assert "#1" in result.payload["text"]

    async def test_second_roll_increments(self) -> None:
        with bundle_context(tenant="acme-corp", community="42", app_id="waddles.demo.dice.default"):
            await transform(_event("!roll20"))
            result = await transform(_event("!roll20"))
        assert result is not None
        assert "#2" in result.payload["text"]

    async def test_other_text_is_no_reply(self) -> None:
        with bundle_context(tenant="acme-corp", community="42", app_id="a"):
            assert await transform(_event("hello")) is None
```

---

## 6. House Rules (mandatory, every bundle)

| Rule | Detail |
|---|---|
| Typed contract only | `PlatformEvent`/`StageEnvelope` in and out of every stage function — never a raw `dict` crossing a stage boundary. The runner already handles JSON (de)serialization at the Valkey edge |
| No DB-text/eval exec | `entrypoint` is resolved via `importlib` against an installed module in the container image — never `exec()` of a DB-stored string |
| DB access via `bundle_runtime` only | A stateful bundle reaches DB/tenant-scope via `flask_core.get_bundle_dal()`/`get_bundle_context()` (§5) — never a bundle-local `set_dal()`, never `event.payload["community_id"]`/`["tenant_id"]` as a tenant-scope substitute |
| Secrets via `resolve_secret` | Any credential is a `config["*_token_ref"]` env-var-name indirection, resolved with `waddle_transports.signing.resolve_secret()` at dispatch time — never a literal token in `app_catalog`/`app_activations` config, never logged |
| PostHog flag gating | Any bundle behind a genuinely new product feature (not a like-for-like v2 port of an already-shipped feature) is wrapped in `flask_core.feature_flags.feature_enabled(flag_key, tenant=..., community=...)`, default OFF, per `critical-rules.md` Feature Flags — check at the call site invoking the bundle, or inside the bundle's own `transform`/entrypoint before doing real work |
| Reply-in-place | Outbound text always in `event.payload["text"]`; target channel from `event.payload["channel_id"\|"channel_name"]` first, `config` fallback second |
| SSRF guard | Any outbound HTTP(S) call in an action bundle goes through `waddle_transports.url_guard.guarded_request` — never a bare `httpx` call to an externally-influenced URL |
| Typing + lint | `mypy --strict` clean, `ruff check`/`ruff format` clean (ruleset: `E W F I N D UP B ASYNC S`, Google docstring convention) |
| Docstrings | Every module and function: 2–3 line summary + why, PEP 257 |
| Coverage | ≥90% on the bundle module — unit tests only, no live network calls in the gated CI path (an opportunistic `~/.{platform}.token`-gated live check is optional, dev-only, always skips in CI) |
| Never sleep/retry locally | Retry/backoff timing is the runner's job (`retry_with_backoff`) — an action bundle only classifies the failure (`RetryableTransportError` vs `NonRetryableTransportError`), never `asyncio.sleep`s itself |

---

## 7. v2.x → v3 Porting Checklist

1. **Find the v2 source.** Trigger/inbound logic: `trigger/receiver/` (platform gateway/webhook
   receivers). Business logic ("what happens on a command"): `action/interactive/
   <feature>_interaction_module/` (e.g. `shoutout_interaction_module/services/*.py`). Routing:
   `processing/router_module/`. Outbound delivery: `action/pushing/<platform>_action_module/`
   (e.g. `discord_action_module/services/discord_service.py`).
2. **Read the closed GitHub issue** for the v2 feature spec (acceptance criteria, edge cases,
   any bug-fix history) — port behavior, not just code shape; regression-test anything a linked
   issue called out.
3. **Map trigger → ingest.** If the v2 receiver already exists in v3 (e.g. Discord/Twitch
   gateway), no new ingest bundle needed — only the `normalize()` shape may need a new field.
   Genuinely new platform → write a receiver (out of scope for this doc; fans out raw dicts onto
   `:ingest`) plus a `normalize()` ingest bundle (§4a).
4. **Map v2 command/business logic → `transform()`.** Pull the actual decision logic (parse
   command, look up data, build response text) out of the v2 interaction module's `app.py`/
   `services/*.py` into a pure `transform(event) -> PlatformEvent | None` (§4b). Anything
   requiring DB/HTTP calls the v2 module made — keep them (async), but the return contract stays
   pure: no side-effecting sends from `process`, only from `action`.
5. **Map v2 output/delivery logic → an action bundle.** Port the real API-calling code from
   `action/pushing/<platform>_action_module/services/*.py` verbatim where possible (auth header
   shape, endpoint, status-code handling) into the action-stage signature (§4c). Drop anything the
   platform now owns centrally: local retry/sleep loops (→ `retry_with_backoff` in the runner),
   per-module audit tables (→ `action_dispatch_log`, automatic via the runner's `_record`).
6. **Register.** Write the migration (§3) with the next sequential number, one `app_catalog` row
   covering every stage this port touches, `ON CONFLICT DO NOTHING` (new app_id) or
   `DO UPDATE ... stages || EXCLUDED.stages` (adding a stage to an existing app_id — verify no
   key overlap first). Activate via `app_tenant_availability` (tenant-wide) or `app_activations`
   (single community).
7. **Test.** One test file per bundle file (§4 skeletons; §5's worked example for a stateful
   bundle), ≥90% coverage, `mypy --strict` + `ruff check` clean. If porting closes a GitHub issue,
   add `# regression: gh-<N>` on the test covering that issue's specific bug. **Before writing any
   test, set up your venv per the Appendix's canonical recipe** — a missing/stale venv is why
   several bundle agents' tests never actually ran.
8. **Verify end-to-end.** Same `app_id` must appear in every stage this port implements — a
   receiver fanning out under one `app_id` while the migration registers stages under a
   different one is silent breakage (see migration 083's T8 convergence note): the pipeline only
   connects when ingest's `normalize()` output key, process's poll key, and action's poll key all
   resolve to the identical `app_id`.

---

## Appendix: Notes for Fleet Agents

- **`waddle_transports` is a real dependency**, not optional — action bundles doing outbound HTTP
  MUST use `guarded_request`/`resolve_secret`, not hand-rolled `httpx` + `os.environ`.
- **`config` is a `Mapping`, read-only** — never mutate it in a bundle; build new dicts for any
  derived values.
- **One `builtin_handlers/<name>_<stage>.py` file per stage per bundle** — do not combine ingest+process+
  action logic into one file even when they're conceptually "the same feature"; each stage
  lives in its own service's container image and only ever imports its own stage's file.

### Canonical test-run recipe (fixes "my venv can't import flask_core's new exports")

**Root cause, diagnosed directly**: every `core/svc_process`/`core/svc_action`/`core/svc_ingest`
checkout needs its **own** `.venv`, `pip install -e`d against **its own** local `libs/flask_core`
copy — a Python editable install (`pip install -e`/`uv pip install -e`) bakes in an **absolute
path** to the source directory at install time. Two distinct failure modes both present as
"missing StageEnvelope/new exports":

1. **No venv at all.** Confirmed by inspection: every worktree checked under `.worktrees/*/core/
   svc_process`, `.worktrees/*/core/svc_action` had **zero** `.venv` directories — `pytest` was
   never actually runnable, so "0 tests ran" gets misread as "import failed".
2. **A shared/symlinked venv.** If a worktree's `.venv` is copied or symlinked from another
   checkout (explicitly forbidden — `general.md`/`backend-python.md` Virtual Environments: "do
   NOT symlink or share the parent repo's `.venv`"), its `flask_core` editable install still
   points at the *other* checkout's `libs/flask_core` — so any local/uncommitted flask_core change
   in *your own* worktree (e.g. this freeze's `bundle_runtime.py`, or `StageEnvelope` before it
   was merged to `release/v3.0.X`) is invisible until that other checkout is updated. Worktrees
   share committed git history via one `.git` dir, but **never** share uncommitted working-tree
   state — a flask_core change only becomes visible to sibling worktrees once it's committed (and
   merged/rebased into their branch).

**The fix — every service directory gets its own fresh venv, every time:**

```bash
cd core/svc_process   # or core/svc_action, core/svc_ingest, hub_api, libs/<name>

uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python3 --require-hashes -r requirements.txt
uv pip install --python .venv/bin/python3 -e ../../libs/flask_core

# svc-action/svc-ingest bundles using waddle_transports need it too (two
# calls -- hash-pinned reqs first, then the editable local package, same
# reason scripts/ci/install-unit-test-deps.sh keeps them separate: pip's
# --require-hashes rejects mixing hashed and unhashed/editable specs in
# one invocation):
uv pip install --python .venv/bin/python3 --require-hashes -r ../../libs/waddle_transports/requirements.txt
uv pip install --python .venv/bin/python3 -e ../../libs/waddle_transports

.venv/bin/python3 -m pytest            # picks up pytest.ini's testpaths=tests, asyncio_mode=auto
```

Proven end to end, from a from-scratch `.venv` (not a pre-existing one): `core/svc_process`'s
`bot_process` bundle suite — `tests/test_builtin_bot_process.py` — 37 passed; `core/svc_action`'s
`discord_send_action`/`twitch_send_action` suites — 22 passed, 1 skipped (the 1 skip is an
opportunistic live-token check that always skips in CI/sandboxes, by design, per §6 Coverage row).

**This is also the CI recipe** (`scripts/ci/install-unit-test-deps.sh`, driven by `tests/k8s/
alpha/05-unit-tests.sh`) — same install shape, just against one shared system Python instead of
one venv per directory, because CI runs the *whole* widened suite (`hub_api` + every `libs/*` +
every `core/svc_*`) in one process. Either recipe works; **never** skip venv creation to save
time — a missing venv is silent (`pytest` just collects 0 tests or errors on import), not loud.
