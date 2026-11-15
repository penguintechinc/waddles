"""Recent-MFA (step-up) check on a verified platform JWT, for the most sensitive admin actions.

OIDC conveys "how and when did this user authenticate" with two claims: `amr` (authentication
methods references, RFC 8176) and `auth_time` (epoch seconds of the authentication event). A
sensitive action -- rotating or retiring a platform signing key -- requires that the token's
authentication event used a second factor AND is recent, so a stolen long-lived session cookie
alone can never trigger it (security review H7).

Everything fails closed: a token without `amr`, without a second-factor method, without a numeric
`auth_time`, or with an `auth_time` in the future (beyond a small skew) or older than
:data:`MFA_MAX_AGE_SECONDS` is NOT a recent MFA. Nothing here reads a role name.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Final

from flask_core.auth import verify_jwt_token
from flask_core.secrets import require_secret_key
from quart import Request

#: How fresh the authentication event must be (5 minutes, security review H7).
MFA_MAX_AGE_SECONDS: Final = 300
#: Clock skew tolerated for an `auth_time` slightly in the future.
MFA_SKEW_SECONDS: Final = 60
#: `amr` values (RFC 8176) that ASSERT a second factor: `mfa` (minted only for a user-verified
#: passkey, `services/auth_amr.py`) or `otp`. A bare `hwk` / `swk` (a passkey without user
#: verification) and `pwd` are deliberately NOT enough.
MFA_METHODS: Final = frozenset({"mfa", "otp"})

#: Result words of :func:`step_up_failure` (None = satisfied).
NO_MFA: Final = "no_mfa"
STALE: Final = "stale"
NO_AUTH_TIME: Final = "no_auth_time"


def step_up_failure(claims: Mapping[str, Any], *, now: float | None = None) -> str | None:
    """Why `claims` is not a recent MFA (`no_mfa` / `no_auth_time` / `stale`), or None if it is."""
    amr = claims.get("amr")
    methods = {m for m in amr if isinstance(m, str)} if isinstance(amr, list) else set()
    if not methods & MFA_METHODS:
        return NO_MFA
    auth_time = claims.get("auth_time")
    if isinstance(auth_time, bool) or not isinstance(auth_time, int | float):
        return NO_AUTH_TIME
    moment = time.time() if now is None else now
    age = moment - float(auth_time)
    if age < -MFA_SKEW_SECONDS or age > MFA_MAX_AGE_SECONDS:
        return STALE
    return None


def request_claims(request: Request) -> dict[str, Any] | None:
    """The verified claims of the request's bearer token (a second, independent decode), or None."""
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        return None
    payload = verify_jwt_token(header[7:], require_secret_key())
    return dict(payload) if payload is not None else None
