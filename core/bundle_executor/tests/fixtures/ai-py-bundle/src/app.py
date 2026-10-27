"""Test-only Python `stage-next` bundle that CALLS the `ai` host import (`ai.generate`).

Not a shipped bundle (no `bundles/core-bundles.yaml` entry, never seeded): it
exists so `core/bundle_executor/tests/stage_next_python_ai_e2e.rs` can prove the
whole Python path against a REAL componentize-py build -- the SDK's eager
wizening of `wit_world.imports.ai`, the `waddle_sdk.ai` facade, and its
fail-loud error mapping -- exactly the seam `componentize-py-eager-wizen`
documents as invisible to mocked tests.

`event.event_type` `ai-generate` calls `ai.generate` with the payload's
`prompt` and optional `max_tokens` / `temperature_x100` / `json`; the outcome is
echoed back as `payload["result"]` (`ok:<text>` or a stable error tag) so the
test can assert on it. The completion is only ever echoed as inert text, never
acted on -- the model output is untrusted.
"""

from __future__ import annotations

from dataclasses import replace

from waddle_sdk import ai
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Call `ai.generate` for an `ai-generate` event and echo the outcome."""
    if event.event_type != "ai-generate":
        return None
    payload = event.payload
    temperature_x100 = payload.get("temperature_x100")
    try:
        text = await ai.generate(
            str(payload.get("prompt", "")),
            max_tokens=payload.get("max_tokens"),
            temperature=None if temperature_x100 is None else int(temperature_x100) / 100,
            json=bool(payload.get("json", False)),
        )
        result = f"ok:{text}"
    except ai.DeniedError as exc:
        result = f"denied:{exc.code}"
    except ai.TooManyTokensError as exc:
        result = f"too-many-tokens:{exc.bound}"
    except ai.ContentFilteredError:
        result = "content-filtered"
    except ai.UnavailableError:
        result = "unavailable"
    except ai.InvalidAiArgError:
        result = "invalid"
    except ai.AiError:
        result = "backend"
    return replace(event, payload={"result": result})


async def dispatch(envelope: StageEnvelope, config: dict[str, object], **_: object) -> None:
    """Unsupported: this bundle has no action stage."""
    raise NotImplementedError("ai test bundle has no action stage")
