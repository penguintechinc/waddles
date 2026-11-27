"""Tenant SSO signups under the per-tenant signup-domain restriction (real OIDC login flow).

Restricted tenant: every SSO signup is created unverified, mailed a link (real aiosmtpd STARTTLS
sink) and gets no session until verified; a wrong domain is refused; an unrestricted tenant's SSO
signup needs no email; an existing unverified user is held once the tenant adds a restriction.
"""

# ruff: noqa: F811 - reused fixtures re-declared as args

from __future__ import annotations

from typing import Any

from services import email_verification as ev
from tests.conftest import TENANT_SLUG
from tests.sso.kit import Kit
from tests.test_dsar_email_sender import make_sink, pki  # noqa: F401 - fixtures
from tests.test_email_verification import (  # noqa: F401 - fixtures + helpers
    _token_from,
    kill_switch_off,
    no_smtp_env,
    smtp,
)
from tests.test_tenant_signup_domains import restrict


class TestSsoSignup:
    async def test_restricted_tenant_holds_sso_signup_until_verified(
        self, kit: Kit, smtp: Any
    ) -> None:
        sink = smtp()
        await restrict(kit.db, TENANT_SLUG, ["acme.test"])
        public_id = await kit.create(kit.oidc_body())

        pending = await kit.oidc_login(public_id)
        assert kit.error_reason(pending) == ev.ERR_REQUIRED
        users = await kit.users_by_email("alice@acme.test")
        assert len(users) == 1 and users[0].email_verified is False
        assert len(sink.handler.messages) == 1

        again = await kit.oidc_login(public_id)  # still pending: no session, no mail flood
        assert kit.error_reason(again) == ev.ERR_REQUIRED
        assert len(sink.handler.messages) == 1

        verified = await kit.client.post(
            "/api/v1/auth/verify-email", json={"token": _token_from(sink)}
        )
        assert verified.status_code == 200
        payload = await kit.redeem(await kit.oidc_login(public_id))
        assert payload["tenant"] == TENANT_SLUG

    async def test_restricted_tenant_refuses_wrong_domain(self, kit: Kit, smtp: Any) -> None:
        sink = smtp()
        await restrict(kit.db, TENANT_SLUG, ["corp.test"])  # IdP connection allows acme.test
        public_id = await kit.create(kit.oidc_body())
        response = await kit.oidc_login(public_id)
        assert kit.error_reason(response) == ev.ERR_DOMAIN
        assert await kit.users_by_email("alice@acme.test") == []
        assert sink.handler.messages == []

    async def test_unrestricted_tenant_sso_needs_no_email(self, kit: Kit, smtp: Any) -> None:
        sink = smtp()
        public_id = await kit.create(kit.oidc_body())
        payload = await kit.redeem(await kit.oidc_login(public_id))
        assert payload["tenant"] == TENANT_SLUG and sink.handler.messages == []
        assert (await kit.users_by_email("alice@acme.test"))[0].email_verified is True

    async def test_restriction_added_later_holds_an_unverified_existing_user(
        self, kit: Kit, smtp: Any
    ) -> None:
        sink = smtp()
        public_id = await kit.create(kit.oidc_body())
        await kit.redeem(await kit.oidc_login(public_id))
        dal = kit.db.dal
        dal(dal.hub_users.email == "alice@acme.test").update(email_verified=False)
        dal.commit()
        assert (await kit.redeem(await kit.oidc_login(public_id)))["tenant"]  # no restriction

        await restrict(kit.db, TENANT_SLUG, ["acme.test"])
        held = await kit.oidc_login(public_id)
        assert kit.error_reason(held) == ev.ERR_REQUIRED and len(sink.handler.messages) == 1
