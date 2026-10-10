"""Test-only Python `stage-next` bundle that CALLS the `economy` host import (issue #714).

Not a shipped bundle (no `bundles/core-bundles.yaml` entry, never seeded): it
exists so `core/bundle_executor/tests/stage_next_python_economy_e2e.rs` can
prove the whole Python path against a REAL componentize-py build -- the SDK's
eager wizening of `wit_world.imports.economy`, the `waddle_sdk.economy`
facade, and its fail-loud error mapping -- exactly the seam
`componentize-py-eager-wizen` documents as invisible to mocked tests.

It is also the reference shape of a casino-style bundle: it decides the game
outcome itself (`eco-wager` takes the `payout` from the event) and lets the
host bound it.

`event.event_type` selects the call (`eco-balance` / `eco-wager` /
`eco-transfer` / `eco-max-bet` / `eco-board`); the call's outcome is echoed
back as `payload["result"]` (`ok:<value>` or a stable error tag) so the test
can assert on it.
"""

from __future__ import annotations

from dataclasses import replace

from waddle_sdk import economy
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Call the `economy.*` op named by `event_type` and echo the outcome."""
    payload = event.payload
    user = payload.get("user", "")
    try:
        if event.event_type == "eco-balance":
            result = f"ok:{await economy.balance(user)}"
        elif event.event_type == "eco-wager":
            new_balance = await economy.wager(user, int(payload["stake"]), int(payload["payout"]))
            result = f"ok:{new_balance}"
        elif event.event_type == "eco-transfer":
            await economy.transfer(str(payload["from"]), str(payload["to"]), int(payload["amount"]))
            result = "ok"
        elif event.event_type == "eco-max-bet":
            result = f"ok:{await economy.max_bet(user)}"
        elif event.event_type == "eco-board":
            rows = await economy.leaderboard(int(payload["limit"]))
            result = "ok:" + ",".join(f"{r.user}={r.balance}" for r in rows)
        else:
            return None
    except economy.DeniedError as exc:
        result = f"denied:{exc.code}"
    except economy.InsufficientFundsError as exc:
        result = f"insufficient-funds:{exc.balance}"
    except economy.OverCapError as exc:
        result = f"over-cap:{exc.cap}"
    except economy.NotAMemberError:
        result = "not-a-member"
    except economy.UnavailableError:
        result = "unavailable"
    except economy.InvalidEconomyArgError:
        result = "invalid"
    except economy.EconomyError:
        result = "backend"
    return replace(event, payload={"result": result})


async def dispatch(envelope: StageEnvelope, config: dict[str, object], **_: object) -> None:
    """Unsupported: this bundle has no action stage."""
    raise NotImplementedError("economy test bundle has no action stage")
