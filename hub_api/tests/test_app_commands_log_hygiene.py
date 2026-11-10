"""Slash refusal logging carries no user/source/community value and no traceback."""

from __future__ import annotations

import logging

import pytest
from quart import Quart

from blueprints.v1 import app_commands_internal as bp
from services import app_command_registry as registry
from services.dataplane_scope import (
    AmbiguousScopeError,
    InvalidSourceError,
    ScopeNotFoundError,
)

CANARY = "user-11111111-canary-SECRET-guild-987654321"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (InvalidSourceError("bad"), "BAD_REQUEST"),
        (AmbiguousScopeError("ambiguous"), "SCOPE_AMBIGUOUS"),
        (
            registry.ContextNotFoundError("community is not paired to this source"),
            "CONTEXT_NOT_FOUND",
        ),
        (ScopeNotFoundError("none"), "SCOPE_NOT_FOUND"),
    ],
)
@pytest.mark.parametrize("op", ["get_context", "set_context", "clear_context"])
async def test_refusal_logs_no_pii(
    caplog: pytest.LogCaptureFixture, exc: Exception, code: str, op: str
) -> None:
    """A refusal chained to a canary-bearing cause logs only op, class and code."""
    app = Quart(__name__)
    try:
        try:
            raise ValueError(CANARY)
        except ValueError as inner:
            raise exc from inner
    except Exception as caught:  # noqa: BLE001 -- test harness
        async with app.test_request_context("/"):
            with caplog.at_level(logging.DEBUG):
                body, _status = bp._refusal(caught, op)
    assert CANARY not in caplog.text
    assert CANARY not in str(body)
    assert "Traceback" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)
    assert f"op={op}" in caplog.text
    assert f"code={code}" in caplog.text
    assert type(exc).__name__ in caplog.text
