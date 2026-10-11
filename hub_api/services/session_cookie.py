"""Browser-SPA session cookie -- security.md C4 fix (OWASP A07).

The session JWT used to be handed to the frontend only in the response
body, which `admin/hub_module/frontend` persisted to `localStorage` (see
`contexts/AuthContext.jsx`) so it could attach `Authorization: Bearer
<token>` on every subsequent request. `localStorage` is readable by any
script running on the page -- a single XSS payload anywhere in the SPA
(or a compromised dependency) can exfiltrate the full session, no
further exploitation needed. Every auth-issuing route in
`blueprints/v1/auth.py` (login/register/admin/temp-password/refresh/
OAuth-exchange/passkey) now ALSO sets this HttpOnly cookie; `app.py`'s
`before_request` hook bridges it back into `Authorization: Bearer` for
every one of hub-api's existing token-extraction call sites
(`flask_core.tenancy.tenant_middleware`, `flask_core.authz.
require_scope`, `services.current_user`, `services.community_authz`,
etc.) to consume completely unchanged -- see that hook's own docstring.

CSRF: `SameSite=Lax` is the chosen mitigation (security.md's "double-submit
token or SameSite" -- either is acceptable). A cross-site page cannot
trigger an authenticated state-changing request: the browser omits a
`SameSite=Lax` cookie on cross-site subrequests (fetch/XHR/form-POST),
so a forged request arrives with neither a bearer header nor the cookie
and is rejected by the normal 401/403 path. Lax (not Strict) is required
so the cookie the OAuth exchange lands isn't itself dropped on the
provider's redirect back to our own origin.
"""

from __future__ import annotations

from typing import Any, cast

from flask_core.auth import ACCESS_TOKEN_TTL_SECONDS
from quart import Response, after_this_request
from werkzeug.wrappers import Response as WerkzeugResponse

#: `after_this_request`'s callback signature is typed against Quart's own
#: `Response | werkzeug.wrappers.Response` union (Quart falls back to a
#: bare Werkzeug response in a couple of internal code paths, never
#: actually exercised by the routes in this module -- every response
#: reaching `_attach`/`_detach` below is a real `quart.Response`) --
#: match the union exactly rather than fighting mypy with a narrower
#: `Response`-only hint.
_AnyResponse = Response | WerkzeugResponse

#: HttpOnly cookie carrying the session JWT for the browser SPA. Never
#: readable via `document.cookie` -- that is the entire point: an XSS
#: payload can no longer read the session out of client-side storage.
SESSION_COOKIE_NAME = "wb_session"

#: The access-token cookie lives exactly as long as the JWT inside it (15 minutes by default):
#: the browser drops it the moment it expires instead of replaying a dead token, and the next
#: request's 401 triggers the silent refresh. The JWT's own `exp` is what the server enforces;
#: this only keeps the cookie from outliving it.
SESSION_COOKIE_MAX_AGE = ACCESS_TOKEN_TTL_SECONDS

#: HttpOnly cookie carrying the rotating REFRESH token. Scoped to `/api/v1/auth` so the browser
#: sends it only to the login/refresh/logout endpoints -- never on an ordinary API call, which
#: keeps the long-lived credential off the hot path (and out of every proxy/access log line that
#: records a non-auth request). `SameSite=Lax` for the same OAuth-redirect reason as
#: `SESSION_COOKIE_NAME`; a cross-site POST to `/refresh` carries no Lax cookie.
REFRESH_COOKIE_NAME = "wb_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


def set_session_cookie(response: Response, token: str) -> Response:
    """Set the HttpOnly session cookie on `response`, carrying `token`.

    Use directly on routes that already hold a real `Response` object
    (e.g. `services.dto_response.jsonify_dto`'s return value).
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return response


def clear_session_cookie(response: Response) -> Response:
    """Clear the session cookie (and the refresh cookie) on `response` -- logout."""
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)
    return response


def set_refresh_cookie(response: Response, token: str, *, max_age: int) -> Response:
    """Set the HttpOnly refresh cookie on `response`, valid for `max_age` seconds."""
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        token,
        max_age=max_age,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return response


def issue_refresh_cookie(token: str, *, max_age: int) -> None:
    """Register an `after_this_request` hook that sets the refresh cookie.

    Called from the single place every login path mints a session
    (`auth_service.create_session_token`), so password / OAuth / passkey / SSO / registration
    all deliver the refresh token without each route having to remember to.
    """

    @after_this_request
    def _attach_refresh(response: _AnyResponse) -> _AnyResponse:
        return set_refresh_cookie(cast(Response, response), token, max_age=max_age)


def issue_session_cookie(token: str) -> None:
    """Register an `after_this_request` hook that sets the session cookie.

    Routes decorated with `@validate_response` return a bare dataclass --
    quart-schema builds the real `Response` afterward, so there is nothing
    to call `.set_cookie()` on yet at the point the route function
    returns. `after_this_request` fires once Quart has the final
    `Response` regardless of which path built it (quart-schema's
    `TypeAdapter` conversion or `jsonify_dto`'s manual one), so this is
    safe to call from either kind of route.
    """

    @after_this_request
    def _attach(response: _AnyResponse) -> _AnyResponse:
        return set_session_cookie(cast(Response, response), token)


def clear_session_cookie_after_request() -> None:
    """Register an `after_this_request` hook that clears the session cookie."""

    @after_this_request
    def _detach(response: _AnyResponse) -> _AnyResponse:
        return clear_session_cookie(cast(Response, response))


def refresh_token_from_cookie(request: Any) -> str | None:
    """Return the refresh cookie's raw token, or `None` if absent."""
    return request.cookies.get(REFRESH_COOKIE_NAME) or None


def bearer_token_from_cookie(request: Any) -> str | None:
    """Return the session cookie's raw JWT, or `None` if absent.

    Used only by `app.py`'s `before_request` bridge -- every other
    call site keeps reading `Authorization` unchanged (see module
    docstring).
    """
    return request.cookies.get(SESSION_COOKIE_NAME) or None
