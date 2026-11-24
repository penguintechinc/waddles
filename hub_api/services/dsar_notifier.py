"""Outbound notification seam for the DSAR inbox (verification emails).

This module defines the contract and resolves the notifier: an explicit
`app.config["DSAR_NOTIFIER"]` wins, else the SMTP sender (`services/dsar_email_sender.py`) when
`DSAR_SMTP_HOST` is configured, else a default that FAILS LOUD (`NotifierUnavailable`) instead
of pretending delivery happened.
The intake route still stores the request (verification_pending) and records a
`verification_send_failed` event, so a failure is visible and the public response stays uniform.
"""

from __future__ import annotations

from typing import Protocol

from quart import Quart, current_app

CONFIG_KEY = "DSAR_NOTIFIER"


class NotifierUnavailable(RuntimeError):
    """No outbound email sender is configured (or the send failed)."""


class DsarNotifier(Protocol):
    """Sends the verification link to a requester. Implementations MUST NOT log `to_email`/URL."""

    async def send_verification(self, *, to_email: str, verify_url: str) -> None:
        """Deliver `verify_url` to `to_email`; raise `NotifierUnavailable` on failure."""
        ...


class UnconfiguredNotifier:
    """Default when SMTP is not configured: refuses to send."""

    async def send_verification(self, *, to_email: str, verify_url: str) -> None:
        """Always raises `NotifierUnavailable` -- delivery is never faked."""
        raise NotifierUnavailable("no email sender is configured for hub-api")


def get_notifier(app: Quart | None = None) -> DsarNotifier:
    """Return the app's notifier: explicit config, else env-configured SMTP, else fail-loud."""
    target = app if app is not None else current_app
    notifier: DsarNotifier | None = target.config.get(CONFIG_KEY)
    if notifier is not None:
        return notifier
    from services.dsar_email_sender import notifier_from_env  # lazy: it imports this module

    return notifier_from_env() or UnconfiguredNotifier()
