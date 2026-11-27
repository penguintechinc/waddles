"""Account MFA after a tenant SSO (SAML / OIDC) sign-in, end to end through the real blueprints.

A user with an ACTIVE local factor gets no session and no exchange code from the SSO callback:
the same `_require_mfa_if_enrolled` gate as the password path issues a pending token, handed to
the webui MFA step in the URL fragment. The optional tenant opt-in `ssoTrustIdpMfa` lets an OIDC
IdP's MFA `amr` stand in for the local factor; it is off by default. Completing the challenge
through `/api/v1/auth/mfa/login/verify` is covered in `tests/test_mfa_federated.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from services import auth_service, mfa_service, oauth_service, totp
from services.session_cookie import SESSION_COOKIE_NAME
from services.sso_service import TRUST_IDP_MFA_KEY
from tests.conftest import TENANT_SLUG
from tests.sso.kit import FRONTEND, Kit, decode_unverified

EMAIL = "alice@acme.test"


async def _enroll(kit: Kit) -> int:
    """Give the (already provisioned) SSO user an ACTIVE authenticator-app factor."""
    users = await kit.users_by_email(EMAIL)
    assert len(users) == 1
    uid = int(users[0].id)
    kit.db.dal.hub_user_mfa.insert(
        user_id=uid,
        totp_secret_enc="sealed-elsewhere",  # noqa: S106 -- the gate only reads the status
        totp_status=mfa_service.STATUS_ACTIVE,
        totp_confirmed_at=datetime.now(UTC),
        email_status=0,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    kit.db.dal.commit()
    return uid


def _set_trust(kit: Kit, on: bool) -> None:
    t = kit.db.dal.tenants
    kit.db.dal(t.slug == TENANT_SLUG).update(config={TRUST_IDP_MFA_KEY: on})
    kit.db.dal.commit()


def _assert_challenged(kit: Kit, response: Any, *, auth_method: str) -> str:
    """The callback redirected to the MFA step with a fragment-only pending token; no session."""
    assert response.status_code == 303, response.headers
    loc = urlparse(kit.location(response))
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{FRONTEND}{auth_service.MFA_STEP_PATH}"
    assert loc.query == ""  # never in the query string
    assert response.headers["Referrer-Policy"] == "no-referrer"
    frag = {k: v[0] for k, v in parse_qs(loc.fragment).items()}
    token = frag["pending"]
    assert frag["methods"].split(",")[0] == "totp"
    cookies = response.headers.getlist("Set-Cookie")
    assert not any(c.startswith(f"{SESSION_COOKIE_NAME}=") for c in cookies)
    assert not any(c.startswith(f"{oauth_service.EXCHANGE_BINDING_COOKIE}=") for c in cookies)
    t = kit.db.dal.hub_mfa_challenges
    row = kit.db.dal(t.token_hash == totp.hash_token(token)).select().first()
    assert row.auth_method == auth_method
    assert row.tenant_slug == TENANT_SLUG  # the post-MFA session keeps the SSO tenant
    return token


class TestSsoMfaChallenge:
    async def test_oidc_enrolled_user_is_challenged(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.redeem(await kit.oidc_login(public_id))  # provision (not enrolled: unchanged)
        await _enroll(kit)
        _assert_challenged(kit, await kit.oidc_login(public_id), auth_method="sso_oidc")

    async def test_saml_enrolled_user_is_challenged(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        await kit.redeem(await kit.saml_login(public_id))
        await _enroll(kit)
        _assert_challenged(kit, await kit.saml_login(public_id), auth_method="sso_saml")

    async def test_non_enrolled_user_gets_the_session_unchanged(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        payload = await kit.redeem(await kit.oidc_login(public_id))
        assert payload["tenant"] == TENANT_SLUG
        assert kit.db.dal(kit.db.dal.hub_mfa_challenges).count() == 0


class TestTrustIdpMfa:
    async def test_trust_off_challenges_even_with_idp_mfa_amr(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.redeem(await kit.oidc_login(public_id))
        await _enroll(kit)
        _set_trust(kit, False)
        response = await kit.oidc_login(public_id, amr=["pwd", "mfa"])
        _assert_challenged(kit, response, auth_method="sso_oidc")

    async def test_trust_on_with_idp_mfa_amr_skips_the_local_factor(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.redeem(await kit.oidc_login(public_id))
        uid = await _enroll(kit)
        _set_trust(kit, True)
        payload = await kit.redeem(await kit.oidc_login(public_id, amr=["pwd", "hwk"]))
        assert payload["sub"] == str(uid) and payload["tenant"] == TENANT_SLUG
        assert set(decode_unverified(payload["_token"])["amr"]) == {"hwk", "mfa"}
        assert kit.db.dal(kit.db.dal.hub_mfa_challenges).count() == 0

    @pytest.mark.parametrize("amr", [None, ["pwd"], "mfa", [123]])
    async def test_trust_on_without_an_mfa_amr_still_challenges(self, kit: Kit, amr: Any) -> None:
        public_id = await kit.create(kit.oidc_body())
        await kit.redeem(await kit.oidc_login(public_id))
        await _enroll(kit)
        _set_trust(kit, True)
        response = await kit.oidc_login(public_id, amr=amr)
        _assert_challenged(kit, response, auth_method="sso_oidc")

    async def test_trust_on_never_applies_to_saml(self, kit: Kit) -> None:
        public_id = await kit.create(kit.saml_body())
        await kit.redeem(await kit.saml_login(public_id))
        await _enroll(kit)
        _set_trust(kit, True)
        _assert_challenged(kit, await kit.saml_login(public_id), auth_method="sso_saml")
