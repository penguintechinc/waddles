"""Account MFA after a federated (OAuth provider) sign-in: same pending token, same verify step.

Real paths: the auth + mfa blueprints, `oauth_service.oauth_callback` (state + browser binding,
identity-link resolution), the shared `_require_mfa_if_enrolled` gate, the challenge table and the
session mint. The faked seams are the provider network calls only (`_exchange_code`, provider
credentials). SSO (SAML / OIDC) federated MFA lives in `tests/sso/test_sso_mfa.py`.
"""

# ruff: noqa: F811 -- pytest fixtures imported from tests.test_mfa are shadowed by test params

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

import blueprints.v1.auth as auth_module
from services import auth_service, mfa_service, oauth_service, totp
from services.session_cookie import SESSION_COOKIE_NAME
from tests.conftest import TENANT_SLUG
from tests.test_mfa import (  # noqa: F401 -- fixtures re-used by name
    _enroll_totp,
    _fresh_limits,
    _now_code,
    _verify,
    app,
    client,
    no_transport,
    user,
)

PLATFORM = "discord"
PROVIDER_USER_ID = "discord-alice-1"


@pytest.fixture(autouse=True)
def _provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ONLY the provider network boundary (token exchange + credential lookup)."""
    monkeypatch.setattr(
        oauth_service,
        "_get_platform_credentials",
        AsyncMock(return_value={"client_id": "cid", "client_secret": "sec"}),  # noqa: S106
    )
    monkeypatch.setattr(
        oauth_service,
        "_exchange_code",
        AsyncMock(return_value={"id": PROVIDER_USER_ID, "username": "alice"}),
    )


def _link(auth_db: Any, user_id: int) -> None:
    """Link the provider identity to the seeded account (an existing, already-linked user)."""
    auth_db.dal.hub_user_identities.insert(
        hub_user_id=user_id,
        platform=PLATFORM,
        platform_user_id=PROVIDER_USER_ID,
        platform_username="alice",
        linked_at=datetime.now(UTC),
    )
    auth_db.dal.commit()


async def _oauth_callback(client: Any) -> Any:
    """Run start -> provider -> callback in one browser; return the callback response."""
    started = await client.get(f"/api/v1/auth/oauth/{PLATFORM}")
    assert started.status_code == 200, await started.get_data()
    state = (await started.get_json())["state"]
    cookie = next(
        h.split(";", 1)[0]
        for h in started.headers.getlist("Set-Cookie")
        if h.startswith(f"{auth_module.LOGIN_BINDING_COOKIE}=")
    )
    return await client.get(
        f"/api/v1/auth/oauth/{PLATFORM}/callback",
        query_string={"code": "provider-code", "state": state},
        headers={"Cookie": cookie},
    )


def _fragment(resp: Any) -> dict[str, str]:
    """Assert the redirect is the webui MFA step and return its fragment parameters."""
    assert resp.status_code == 302, resp.headers
    loc = urlparse(resp.headers["Location"])
    assert loc.path == auth_service.MFA_STEP_PATH
    assert loc.query == ""  # the pending token is NEVER in the query string
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    return {k: v[0] for k, v in parse_qs(loc.fragment).items()}


def _session_cookies(resp: Any) -> list[str]:
    return [
        h
        for h in resp.headers.getlist("Set-Cookie")
        if h.startswith((f"{SESSION_COOKIE_NAME}=", f"{oauth_service.EXCHANGE_BINDING_COOKIE}="))
    ]


class TestOAuthMfa:
    async def test_enrolled_user_gets_pending_token_then_verify_mints_session(
        self, client: Any, auth_db: Any, user: Any
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        _link(auth_db, user.id)
        codes_before = len(auth_db.dal(auth_db.dal.hub_oauth_exchange_codes).select())

        resp = await _oauth_callback(client)
        frag = _fragment(resp)
        token = frag["pending"]
        assert token not in resp.headers["Location"].split("#", 1)[0]
        assert frag["methods"].split(",") == ["totp", "recovery"]
        assert int(frag["expiresIn"]) == mfa_service.CHALLENGE_TTL_SECONDS
        # No session, no refresh cookie, no exchange code / binding: nothing a session could ride.
        assert _session_cookies(resp) == []
        assert len(auth_db.dal(auth_db.dal.hub_oauth_exchange_codes).select()) == codes_before
        row = (
            auth_db.dal(auth_db.dal.hub_mfa_challenges.token_hash == totp.hash_token(token))
            .select()
            .first()
        )
        assert row.auth_method == f"oauth_{PLATFORM}"  # carried for audit / the session

        done = await _verify(client, token, "totp", _now_code(secret))
        assert done.status_code == 200, await done.get_data()
        claims = jwt.decode((await done.get_json())["token"], options={"verify_signature": False})
        assert claims["sub"] == str(user.id)
        assert {"otp", "mfa"} <= set(claims["amr"]) and "pwd" not in claims["amr"]
        assert any(
            c.startswith(f"{SESSION_COOKIE_NAME}=") for c in done.headers.getlist("Set-Cookie")
        )

    async def test_non_enrolled_user_flow_unchanged(
        self, client: Any, auth_db: Any, user: Any
    ) -> None:
        _link(auth_db, user.id)
        resp = await _oauth_callback(client)
        assert resp.status_code == 302
        loc = urlparse(resp.headers["Location"])
        assert loc.path == "/auth/callback" and parse_qs(loc.query)["code"]
        assert auth_db.dal(auth_db.dal.hub_mfa_challenges).count() == 0

    async def test_replayed_pending_token_is_refused(
        self, client: Any, auth_db: Any, user: Any
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        _link(auth_db, user.id)
        token = _fragment(await _oauth_callback(client))["pending"]
        assert (await _verify(client, token, "totp", _now_code(secret))).status_code == 200
        again = await _verify(client, token, "totp", _now_code(secret, 1))
        assert again.status_code == 401
        assert (await again.get_json())["error"]["code"] == "MFA_CHALLENGE_INVALID"

    async def test_expired_pending_token_is_refused(
        self, client: Any, auth_db: Any, user: Any
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        _link(auth_db, user.id)
        token = _fragment(await _oauth_callback(client))["pending"]
        t = auth_db.dal.hub_mfa_challenges
        auth_db.dal(t.token_hash == totp.hash_token(token)).update(
            expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        auth_db.dal.commit()
        resp = await _verify(client, token, "totp", _now_code(secret))
        assert resp.status_code == 401
        assert (await resp.get_json())["error"]["code"] == "MFA_CHALLENGE_INVALID"
        assert _session_cookies(resp) == []

    async def test_kill_switch_still_challenges_enrolled_oauth_user(
        self, client: Any, auth_db: Any, user: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _enroll_totp(client, user.headers)
        _link(auth_db, user.id)
        monkeypatch.setenv("FLAG_WADDLES_DISABLE_MFA", "true")
        assert _fragment(await _oauth_callback(client))["pending"]


class TestSsoChallengeCompletes:
    """An SSO-issued challenge completes through the SAME verify route into a tenant session."""

    async def test_sso_challenge_verifies_into_the_tenant_session(
        self, client: Any, auth_db: Any, user: Any
    ) -> None:
        secret, _ = await _enroll_totp(client, user.headers)
        tenant = auth_db.dal(auth_db.dal.tenants.slug == TENANT_SLUG).select().first()
        with pytest.raises(auth_service.MfaRequiredError) as raised:
            await auth_service._require_mfa_if_enrolled(
                auth_db,
                auth_db.dal,
                user_id=user.id,
                tenant_id=int(tenant.id),
                tenant_slug=TENANT_SLUG,
                auth_method="sso_oidc",
            )
        resp = await _verify(client, raised.value.challenge.token, "totp", _now_code(secret))
        assert resp.status_code == 200, await resp.get_data()
        claims = jwt.decode((await resp.get_json())["token"], options={"verify_signature": False})
        assert claims["tenant"] == TENANT_SLUG
        assert "pwd" not in claims["amr"] and "mfa" in claims["amr"]
