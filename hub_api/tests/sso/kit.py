"""Scenario helpers for the SSO integration tests (drive the real Quart app end to end)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
from flask_core.auth import verify_jwt_token
from quart import Quart

from blueprints.v1.sso import TRANSPORT_CONFIG_KEY
from services.sso_types import SCOPE_SSO_ADMIN
from tests.conftest import SECRET_KEY, TENANT_SLUG, make_user_token
from tests.sso.idp_fakes import FakeOidcIdp, FakeSamlIdp

FRONTEND = "https://app.example.com"
ADMIN_USER_ID = 7


@dataclass(slots=True)
class Kit:
    """Bundles the app, client and fake IdPs so tests read as scenarios."""

    app: Quart
    client: Any
    db: Any
    oidc: FakeOidcIdp
    saml: FakeSamlIdp

    def use_oidc_idp(self) -> None:
        """Point hub-api's IdP socket at the OIDC fake."""
        self.app.config[TRANSPORT_CONFIG_KEY] = self.oidc.transport

    # -- admin API -----------------------------------------------------------

    def headers(
        self,
        *,
        scope: str = SCOPE_SSO_ADMIN,
        tenant: str = TENANT_SLUG,
        user_id: int = ADMIN_USER_ID,
    ) -> dict[str, str]:
        """Admin bearer header for `tenant` carrying `scope`."""
        return {
            "Authorization": f"Bearer {make_user_token(user_id=user_id, scope=scope, tenant=tenant)}"
        }

    async def post_connection(self, body: dict[str, Any], **hdr: Any) -> Any:
        """`POST /connections`."""
        return await self.client.post(
            "/api/v1/tenant/sso/connections",
            headers={**self.headers(**hdr), "Content-Type": "application/json"},
            data=json.dumps(body),
        )

    async def patch_connection(self, public_id: str, body: dict[str, Any], **hdr: Any) -> Any:
        """`PATCH /connections/<id>`."""
        return await self.client.patch(
            f"/api/v1/tenant/sso/connections/{public_id}",
            headers={**self.headers(**hdr), "Content-Type": "application/json"},
            data=json.dumps(body),
        )

    async def get_connection(self, public_id: str, **hdr: Any) -> Any:
        """`GET /connections/<id>`."""
        return await self.client.get(
            f"/api/v1/tenant/sso/connections/{public_id}", headers=self.headers(**hdr)
        )

    async def delete_connection(self, public_id: str, **hdr: Any) -> Any:
        """`DELETE /connections/<id>`."""
        return await self.client.delete(
            f"/api/v1/tenant/sso/connections/{public_id}", headers=self.headers(**hdr)
        )

    def oidc_body(self, **overrides: Any) -> dict[str, Any]:
        """A complete, enabled generic-OIDC create body for the fake IdP."""
        body: dict[str, Any] = {
            "protocol": "oidc",
            "displayName": "Acme OIDC",
            "enabled": True,
            "allowedDomains": ["acme.test"],
            "issuer": self.oidc.issuer,
            "clientId": self.oidc.client_id,
            "clientSecret": self.oidc.client_secret,
        }
        body.update(overrides)
        return body

    def saml_body(self, **overrides: Any) -> dict[str, Any]:
        """A complete, enabled SAML create body (IdP metadata pasted in)."""
        body: dict[str, Any] = {
            "protocol": "saml",
            "displayName": "Acme SAML",
            "enabled": True,
            "allowedDomains": ["acme.test"],
            "idpMetadataXml": self.saml.metadata_xml(),
        }
        body.update(overrides)
        return body

    def google_body(self, **overrides: Any) -> dict[str, Any]:
        """A complete, enabled Google create body with the tenant's own client."""
        body: dict[str, Any] = {
            "protocol": "google",
            "displayName": "Acme Google",
            "enabled": True,
            "allowedDomains": ["acme.test"],
            "clientId": self.oidc.client_id,
            "clientSecret": self.oidc.client_secret,
        }
        body.update(overrides)
        return body

    async def create(self, body: dict[str, Any]) -> str:
        """Create a connection and return its public id (asserts 201)."""
        response = await self.post_connection(body)
        assert response.status_code == 201, await response.get_data(as_text=True)
        return str((await response.get_json())["connection"]["id"])

    # -- login flows ---------------------------------------------------------

    async def start(self, public_id: str) -> Any:
        """`GET /<id>/start` (stores the binder cookie in the client jar)."""
        return await self.client.get(f"/api/v1/auth/sso/{public_id}/start")

    async def oidc_login(self, public_id: str, **claims: Any) -> Any:
        """Drive a full OIDC/Google login; returns the callback response."""
        self.use_oidc_idp()
        started = await self.start(public_id)
        assert started.status_code == 200, await started.get_data(as_text=True)
        redirect_url = (await started.get_json())["redirectUrl"]
        code, state = self.oidc.authorize(redirect_url, **claims)
        return await self.client.get(
            f"/api/v1/auth/sso/{public_id}/callback", query_string={"code": code, "state": state}
        )

    async def saml_start(self, public_id: str) -> tuple[Any, str]:
        """Start a SAML login; returns `(parsed AuthnRequest, relay_state)`."""
        started = await self.start(public_id)
        assert started.status_code == 200, await started.get_data(as_text=True)
        authn = self.saml.parse_authn_request((await started.get_json())["redirectUrl"])
        return authn, authn.relay_state

    async def post_acs(self, public_id: str, raw: bytes | str, relay_state: str) -> Any:
        """POST a SAMLResponse to the ACS."""
        b64 = raw if isinstance(raw, str) else self.saml.encode(raw)
        return await self.client.post(
            f"/api/v1/auth/sso/{public_id}/acs",
            form={"SAMLResponse": b64, "RelayState": relay_state},
        )

    async def saml_login(self, public_id: str, **build_kwargs: Any) -> Any:
        """Drive a full SAML login; returns the ACS response."""
        authn, relay = await self.saml_start(public_id)
        return await self.post_acs(
            public_id, self.saml.build_response(authn, **build_kwargs), relay
        )

    # -- assertions ----------------------------------------------------------

    @staticmethod
    def location(response: Any) -> str:
        """The redirect target of `response`."""
        return str(response.headers["Location"])

    def error_reason(self, response: Any) -> str | None:
        """The `?error=` reason of a redirect to the SPA login page, else None."""
        parsed = urlparse(self.location(response))
        if f"{parsed.scheme}://{parsed.netloc}{parsed.path}" != f"{FRONTEND}/login":
            return None
        return parse_qs(parsed.query).get("error", [None])[0]

    async def redeem(self, response: Any) -> dict[str, Any]:
        """Redeem the exchange code in a successful login redirect; return the JWT payload."""
        parsed = urlparse(self.location(response))
        assert (
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{FRONTEND}/auth/callback"
        ), self.location(response)
        code = parse_qs(parsed.query)["code"][0]
        exchanged = await self.client.post(
            "/api/v1/auth/exchange",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"code": code}),
        )
        assert exchanged.status_code == 200, await exchanged.get_data(as_text=True)
        token = (await exchanged.get_json())["token"]
        payload = verify_jwt_token(token, SECRET_KEY)
        assert payload is not None
        payload["_token"] = token
        return payload

    async def users_by_email(self, email: str) -> list[Any]:
        """`hub_users` rows with `email`."""
        rows = await self.db.select_async(self.db.dal(self.db.dal.hub_users.email == email))
        return list(rows)

    async def count_identities(self) -> int:
        """Number of `sso_identities` rows."""
        rows = await self.db.install_dal(self.db.install_dal.sso_identities.id > 0).select()
        return len(list(rows))

    async def connection_row(self, public_id: str) -> Any:
        """Raw `sso_connections` row (for storage-level assertions)."""
        table = self.db.install_dal.sso_connections
        return list(await self.db.install_dal(table.public_id == public_id).select())[0]


def decode_unverified(token: str) -> dict[str, Any]:
    """Decode a JWT without verification (test inspection only)."""
    return jwt.decode(token, options={"verify_signature": False})  # type: ignore[no-any-return]
