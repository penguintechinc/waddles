"""Browser binding for single-use OAuth install/connect ``state`` tokens (login-CSRF defence).

A ``state`` token alone only proves *some* flow was started; the public callback carries no bearer
token, so without a browser binding an attacker can start an install in their own session and feed
the resulting callback URL to a victim's browser (install injection). Each flow therefore also mints
a CSPRNG nonce, set as an HttpOnly/Secure/SameSite=Lax host-only cookie on the INITIATING browser's
authorize response; the server keeps only its SHA-256 inside the state record. The callback must
present the cookie, it is compared in constant time BEFORE the state is consumed (so a foreign
browser cannot burn a victim's pending install), and the state is then single-use (atomic
``GETDEL``).
Cookie lifetime equals the state TTL. Failures raise :class:`InstallStateError` with a fixed typed
``code`` -- never a silent fallback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Final

from quart import Response

#: Raised/returned when the state is absent, expired, replayed, or malformed.
ERR_STATE: Final = "invalid_or_expired_state"
#: Raised when the browser cannot present the nonce minted for this state (missing/foreign cookie).
ERR_BINDING: Final = "state_binding_mismatch"

_MAX_LEN: Final = 128
_COOKIE_NAME_RE: Final = re.compile(r"__Host-[a-z0-9_]{1,40}")


class InstallStateError(Exception):
    """A state could not be consumed; ``code`` is a fixed ``ERR_*`` string safe to render."""

    def __init__(self, code: str) -> None:
        """Record the typed ``code``."""
        super().__init__(code)
        self.code = code


def new_nonce() -> str:
    """Fresh CSPRNG browser-binding nonce (256 bits, URL-safe)."""
    return secrets.token_urlsafe(32)


def nonce_hash(nonce: str) -> str:
    """SHA-256 hex of ``nonce`` -- all the server ever stores of it."""
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def cookie_name(prefix: str, suffix: str = "") -> str:
    """Validated ``__Host-`` cookie name (``__Host-<prefix>[_<suffix>]``), else ``ValueError``."""
    name = f"__Host-{prefix}" + (f"_{suffix}" if suffix else "")
    if not _COOKIE_NAME_RE.fullmatch(name):
        raise ValueError("invalid binding cookie name")
    return name


def set_binding_cookie(response: Response, name: str, nonce: str, max_age: int) -> None:
    """Set the binding cookie (HttpOnly, Secure, SameSite=Lax, Path=/, host-only)."""
    response.set_cookie(
        name, nonce, max_age=max_age, path="/", secure=True, httponly=True, samesite="Lax"
    )


def clear_binding_cookie(response: Response, name: str) -> None:
    """Expire the binding cookie on ``response``."""
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="Lax")


async def consume_bound(client: Any, key: str, nonce: str | None) -> str:
    """Verify the browser binding, THEN atomically fetch-and-delete the state record at ``key``.

    Returns the raw JSON of the record. Raises :class:`InstallStateError`: ``ERR_STATE`` when the
    record is absent/expired/replayed/unparseable; ``ERR_BINDING`` when ``nonce`` is missing or
    does not match the record's stored hash (the record is left untouched in that case).
    """
    if not nonce or len(nonce) > _MAX_LEN:
        # Still distinguish an absent state from a missing cookie only after a state lookup.
        if await client.get(key) is None:
            raise InstallStateError(ERR_STATE)
        raise InstallStateError(ERR_BINDING)
    peeked = await client.get(key)
    if peeked is None:
        raise InstallStateError(ERR_STATE)
    try:
        stored = json.loads(peeked).get("binding")
    except (ValueError, AttributeError) as exc:
        raise InstallStateError(ERR_STATE) from exc
    if not isinstance(stored, str) or not hmac.compare_digest(stored, nonce_hash(nonce)):
        raise InstallStateError(ERR_BINDING)
    raw = await client.getdel(key)
    if raw is None:
        raise InstallStateError(ERR_STATE)
    return str(raw)
