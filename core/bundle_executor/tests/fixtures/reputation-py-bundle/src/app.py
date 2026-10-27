"""Test-only Python `stage-next` bundle that CALLS the `reputation` host import (issue #726).

Not a shipped bundle (no `bundles/core-bundles.yaml` entry, never seeded): it
exists so `core/bundle_executor/tests/stage_next_python_reputation_e2e.rs`
can prove the whole Python path against a REAL componentize-py build -- the
SDK's eager wizening of `wit_world.imports.reputation`, the
`waddle_sdk.reputation` facade, and its fail-loud error mapping -- exactly the
seam `componentize-py-eager-wizen` documents as invisible to mocked tests.

`event.event_type` selects the call (`rep-get` / `rep-adjust`); the call's
outcome is echoed back as `payload["result"]` (`ok:<balance>` or a stable
error tag) so the test can assert on it.
"""

from __future__ import annotations

from dataclasses import replace

from waddle_sdk import reputation
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Call `reputation.get`/`adjust` per `event_type` and echo the outcome."""
    user = event.payload.get("user", "")
    try:
        if event.event_type == "rep-get":
            result = f"ok:{await reputation.get(user)}"
        elif event.event_type == "rep-adjust":
            balance = await reputation.adjust(
                user, int(event.payload["delta"]), str(event.payload["reason"])
            )
            result = f"ok:{balance}"
        else:
            return None
    except reputation.DeniedError as exc:
        result = f"denied:{exc.code}"
    except reputation.NotAMemberError:
        result = "not-a-member"
    except reputation.DailyCapExceededError:
        result = "daily-cap-exceeded"
    except reputation.UnavailableError:
        result = "unavailable"
    except reputation.InvalidReputationArgError:
        result = "invalid"
    except reputation.ReputationError:
        result = "backend"
    return replace(event, payload={"result": result})


async def dispatch(envelope: StageEnvelope, config: dict[str, object], **_: object) -> None:
    """Unsupported: this bundle has no action stage."""
    raise NotImplementedError("reputation test bundle has no action stage")
