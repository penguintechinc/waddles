"""Refusal logging in the EventSub blueprint never carries secret/key/payload material."""

from __future__ import annotations

import logging

import pytest
from quart import Quart

from blueprints.v1 import eventsub_reconcile as bp
from services import eventsub_desired_state as es

SECRET = "whsec-SUPER-SECRET-VALUE-123"  # noqa: S105 -- synthetic canary, not a credential


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (es.UnknownSubscriptionError("unknown callback key"), "NOT_FOUND"),
        (es.NotConfiguredError("eventsub callback base url is not configured"), "NOT_CONFIGURED"),
        (es.InvalidReportError("invalid"), "BAD_REQUEST"),
    ],
)
@pytest.mark.asyncio
async def test_refusal_logs_no_traceback_or_secret(
    caplog: pytest.LogCaptureFixture, exc: es.EventSubError, code: str
) -> None:
    """A refusal whose cause chain holds a secret logs only op, class and code."""
    app = Quart(__name__)
    try:
        try:
            raise ValueError(SECRET)
        except ValueError as inner:
            raise exc from inner
    except es.EventSubError as caught:
        async with app.test_request_context("/"):
            with caplog.at_level(logging.DEBUG):
                body, _status = bp._refusal(caught, "secret")
    text = caplog.text
    assert SECRET not in text
    assert "Traceback" not in text
    assert all(r.exc_info is None for r in caplog.records)
    assert f"code={code}" in text
    assert "op=secret" in text
    assert type(exc).__name__ in text
    assert SECRET not in str(body)
