"""Migration safety: guest badges and raw actors never grant money privileges."""

# F811: pytest fixture is deliberately re-exported from test_app.
# ruff: noqa: F811
from __future__ import annotations

import asyncio

import pytest
from test_app import MENTION, Host, event, host  # noqa: F401
from waddle_sdk import tool

import app


def test_badges_are_not_forwarded_as_authorization(host: Host) -> None:
    """Only the host's separate scope framework authorizes the admin service tool."""
    request = event(f"!points add 3 {MENTION}")
    request.payload.update({"is_mod": True, "is_broadcaster": "true", "__ctx": {"actor": "spoof"}})
    asyncio.run(app.transform(request))
    assert set(host.calls[0][2]) == {"user", "delta"}


def test_tool_backend_errors_fail_loud(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    """Storage/service outages propagate instead of producing fabricated balances."""

    async def fail(*args: object, **kwargs: object) -> object:
        """Raise the service's typed unavailable error at the SDK boundary."""
        raise tool.ToolError("target_unavailable")

    monkeypatch.setattr(tool, "invoke", fail)
    with pytest.raises(tool.ToolError, match="target_unavailable"):
        asyncio.run(app.transform(event("!points")))


def test_denied_admin_tool_produces_explicit_refusal(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admin denial stays a denial even when the event asserts moderator badges."""

    async def fail(*args: object, **kwargs: object) -> object:
        """Raise the host gate's scope denial for the requested write."""
        raise tool.ToolError("forbidden")

    monkeypatch.setattr(tool, "invoke", fail)
    request = event(f"!points set 100 {MENTION}")
    request.payload["is_mod"] = True
    result = asyncio.run(app.transform(request))
    assert result is not None
    assert result.payload["reply"] == "Points request refused (forbidden)."
