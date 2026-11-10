"""RSI Spectrum provider (#486): adapter + HTTP admin API, real DB, only the RSI socket faked.

Real path: Quart blueprints, auth chain, DB-backed community-admin check, service, AES-GCM vault and
store over sqlite are real; only the RSI socket is an ``httpx.MockTransport`` (DNS answers publicly
so the real SSRF guard still runs). Proves the exact response field set, that the session token is
never returned to an admin / never in a log / never plaintext at rest, that a tenantId in the body
is ignored (tenant comes from the token), and cross-community / cross-tenant refusal.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any

import httpx
import pytest
from flask_core.service_jwt import ServiceJwtIssuer

from blueprints.v1.provider_connections import SERVICE_CONFIG_KEY
from services.provider_connections import PlatformKeyVault, ProviderConnectionService
from services.provider_connections.adapter import AdapterRegistry, Credentials, validate_metadata
from services.provider_connections.errors import (
    CredentialsRejected,
    InvalidConnectionInput,
    ProviderUnavailable,
    RefreshNotConfigured,
)
from services.provider_connections.registry import build_default_registry
from services.provider_connections.spectrum import SPECTRUM_SPEC, SpectrumAdapter
from tests.conftest import make_user_token
from tests.provider_connections.conftest import SYSTEM_SERVICE_ID, FakeClock, admin_headers
from tests.provider_connections.grpc_internal_client import GrpcRoutingClient
from tests.provider_connections.support import open_row, raw_rows

TOKEN = "Zk3jQ9x-rsi_session.token+value/abc123=="  # noqa: S105 - fake
MEMBER = "4242"
COMMUNITY = "100200"
INTERNAL = "/api/v1/internal"
CONNECTION_FIELDS = {
    "access",
    "capabilities",
    "createdAt",
    "enabled",
    "expiresAt",
    "hasRefreshToken",
    "hasApiCredential",
    "id",
    "kind",
    "metadata",
    "provider",
    "publicId",
    "resourceType",
    "shared",
    "status",
    "statusReason",
    "updatedAt",
    "usable",
}


class FakeRsi:
    """A tiny fake RSI identify endpoint that knows one valid session token."""

    def __init__(self) -> None:
        """Start serving a signed-in member for :data:`TOKEN`."""
        self.requests: list[httpx.Request] = []
        self.status_override: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer ``POST /api/spectrum/auth/identify``."""
        self.requests.append(request)
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={"success": 0})
        assert request.method == "POST"
        if request.headers.get("x-rsi-token") != TOKEN:
            return httpx.Response(200, json={"success": 0, "code": "ErrNotLoggedIn"})
        return httpx.Response(200, json={"success": 1, "data": {"member": {"id": MEMBER}}})

    @property
    def transport(self) -> httpx.MockTransport:
        """The socket replacement."""
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _rsi_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.getaddrinfo

    def fake(host: str, *args: Any, **kwargs: Any) -> Any:
        if host == "robertsspaceindustries.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.0.10", 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr("services.url_guard.socket.getaddrinfo", fake)


@pytest.fixture
def fake_rsi() -> FakeRsi:
    """The fake RSI socket."""
    return FakeRsi()


@pytest.fixture
def sclient(pc_app: Any, pc_db: Any, fake_rsi: FakeRsi, clock: FakeClock) -> Any:
    """Test client whose service serves Spectrum on the fake socket."""
    pc_app.config[SERVICE_CONFIG_KEY] = ProviderConnectionService(
        pc_db.install_dal,
        AdapterRegistry([SpectrumAdapter(transport=fake_rsi.transport, clock=clock)]),
        PlatformKeyVault(),
        clock=clock,
    )
    return GrpcRoutingClient(pc_app.test_client())


def _base(db: Any, community_id: int | None = None) -> str:
    return f"/api/v1/communities/{community_id or db.seed.community_id}/provider-connections"


def _body(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "provider": "spectrum",
        "credentials": {"access_token": TOKEN},
        "metadata": {"community_id": COMMUNITY},
    }
    body.update(overrides)
    return json.dumps(body)


async def _create(client: Any, db: Any, **overrides: Any) -> Any:
    return await client.post(_base(db), headers=admin_headers(db), data=_body(**overrides))


class TestSpec:
    def test_inbound_only_non_refreshable_credential_provider(self) -> None:
        assert SPECTRUM_SPEC.capabilities == frozenset({"chat_ingest"})
        assert not SPECTRUM_SPEC.refreshable
        assert SPECTRUM_SPEC.resource_type == "spectrum_community"
        assert [f.name for f in SPECTRUM_SPEC.credential_fields] == ["access_token"]

    def test_default_registry_serves_spectrum(self) -> None:
        assert build_default_registry().get("spectrum").spec is SPECTRUM_SPEC

    def test_community_id_is_required_and_numeric(self) -> None:
        assert validate_metadata(SPECTRUM_SPEC, {"community_id": COMMUNITY}) == {
            "community_id": COMMUNITY
        }
        for bad in ({}, {"community_id": ""}, {"community_id": "12a"}, {"community_id": "1" * 19}):
            with pytest.raises(InvalidConnectionInput):
                validate_metadata(SPECTRUM_SPEC, bad)
        with pytest.raises(InvalidConnectionInput, match="unknown"):
            validate_metadata(SPECTRUM_SPEC, {"community_id": COMMUNITY, "forum": "1"})


class TestAdapter:
    @pytest.fixture
    def adapter(self, fake_rsi: FakeRsi) -> SpectrumAdapter:
        return SpectrumAdapter(transport=fake_rsi.transport)

    async def test_validate_and_resolve_store_no_handle(self, adapter: SpectrumAdapter) -> None:
        creds = Credentials(TOKEN)
        account = await adapter.validate_credentials(creds)
        assert (account.account_id, account.account_label, account.scopes) == (MEMBER, "", ())
        resource = await adapter.resolve_resource(creds, account, {"community_id": COMMUNITY})
        assert resource.resource_id == COMMUNITY
        assert resource.facts == {"community_id": COMMUNITY, "member_id": MEMBER}

    async def test_token_only_travels_in_headers_never_url_or_body(
        self, adapter: SpectrumAdapter, fake_rsi: FakeRsi
    ) -> None:
        await adapter.validate_credentials(Credentials(TOKEN))
        (request,) = fake_rsi.requests
        assert TOKEN not in str(request.url) and TOKEN not in request.content.decode()
        assert request.headers["cookie"] == f"Rsi-Token={TOKEN}"

    async def test_anonymous_session_is_rejected(self, adapter: SpectrumAdapter) -> None:
        with pytest.raises(CredentialsRejected):
            await adapter.validate_credentials(Credentials("some-other-token-1234"))

    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_statuses_are_rejected(
        self, adapter: SpectrumAdapter, fake_rsi: FakeRsi, status: int
    ) -> None:
        fake_rsi.status_override = status
        with pytest.raises(CredentialsRejected):
            await adapter.validate_credentials(Credentials(TOKEN))

    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_upstream_failures_are_unavailable(
        self, adapter: SpectrumAdapter, fake_rsi: FakeRsi, status: int
    ) -> None:
        fake_rsi.status_override = status
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials(TOKEN))

    @pytest.mark.parametrize(
        "body",
        [b"nope", b"[1]", b'{"success":1,"data":{"member":{"id":"not-numeric"}}}'],
    )
    async def test_malformed_answers_are_unavailable(self, body: bytes) -> None:
        adapter = SpectrumAdapter(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
        )
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials(TOKEN))

    async def test_oversized_answer_and_transport_failure(self) -> None:
        big = SpectrumAdapter(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 300_000))
        )
        with pytest.raises(ProviderUnavailable, match="large"):
            await big.validate_credentials(Credentials(TOKEN))

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        down = SpectrumAdapter(transport=httpx.MockTransport(boom))
        with pytest.raises(ProviderUnavailable):
            await down.validate_credentials(Credentials(TOKEN))

    async def test_blocked_by_the_ssrf_guard(
        self, adapter: SpectrumAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "services.url_guard.socket.getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))],
        )
        with pytest.raises(ProviderUnavailable, match="network guard"):
            await adapter.validate_credentials(Credentials(TOKEN))

    def test_cookie_prefix_stripped_and_header_unsafe_tokens_refused(
        self, adapter: SpectrumAdapter
    ) -> None:
        assert (
            adapter.normalize_credentials(Credentials(f"Rsi-Token={TOKEN}")).access_token == TOKEN
        )
        for bad in ("a; Other=1;b", "tok,en12345", 'tok"en12345', "short", "x" * 3000):
            with pytest.raises(InvalidConnectionInput) as err:
                adapter.normalize_credentials(Credentials(bad))
            assert bad not in str(err.value)

    async def test_refresh_is_refused_loudly(self, adapter: SpectrumAdapter) -> None:
        with pytest.raises(RefreshNotConfigured):
            await adapter.refresh("r", {})


class TestHttpAdmin:
    async def test_create_returns_exactly_the_documented_fields_and_no_secret(
        self, sclient: Any, pc_db: Any
    ) -> None:
        response = await _create(sclient, pc_db)
        assert response.status_code == 201
        text = await response.get_data(as_text=True)
        conn = json.loads(text)["connection"]
        assert set(conn) == CONNECTION_FIELDS
        assert set(conn["metadata"]) == {"community_id", "capabilities", "scopes"}
        assert conn["metadata"]["community_id"] == COMMUNITY
        assert (conn["provider"], conn["resourceType"], conn["capabilities"]) == (
            "spectrum",
            "spectrum_community",
            ["chat_ingest"],
        )
        assert TOKEN not in text and MEMBER not in text

    async def test_secret_is_never_returned_by_get_list_or_patch(
        self, sclient: Any, pc_db: Any
    ) -> None:
        cid = (await (await _create(sclient, pc_db)).get_json())["connection"]["id"]
        url = f"{_base(pc_db)}/{cid}"
        patched = await sclient.patch(
            url, headers=admin_headers(pc_db), data=json.dumps({"enabled": False})
        )
        got = await sclient.get(url, headers=admin_headers(pc_db))
        listed = await sclient.get(_base(pc_db), headers=admin_headers(pc_db))
        for response in (patched, got, listed):
            assert response.status_code == 200
            text = await response.get_data(as_text=True)
            assert TOKEN not in text
            assert "accessToken" not in text and "sealed" not in text.lower()

    async def test_token_is_ciphertext_at_rest_and_never_logged(
        self, sclient: Any, pc_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        assert (await _create(sclient, pc_db)).status_code == 201
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert row["access_token"] != TOKEN and TOKEN not in row["access_token"]
        assert row["refresh_token"] is None
        assert open_row(row) == TOKEN
        assert TOKEN.encode() not in pc_db.db_path.read_bytes()
        assert TOKEN not in caplog.text

    async def test_tenant_id_in_the_body_is_ignored_tenant_comes_from_the_token(
        self, sclient: Any, pc_db: Any
    ) -> None:
        response = await _create(
            sclient,
            pc_db,
            tenantId=pc_db.seed.other_tenant_id,
            tenant_id=pc_db.seed.other_tenant_id,
        )
        assert response.status_code in (201, 400)
        rows = await raw_rows(pc_db, "platform_connections")
        assert all(r["tenant_id"] == pc_db.seed.tenant_id for r in rows)

    async def test_bad_token_is_a_401_family_error_and_stores_nothing(
        self, sclient: Any, pc_db: Any
    ) -> None:
        response = await _create(
            sclient, pc_db, credentials={"access_token": "another-session-token-0000"}
        )
        assert response.status_code in (400, 401, 422)
        assert "another-session-token-0000" not in await response.get_data(as_text=True)
        assert await raw_rows(pc_db, "platform_connections") == []

    @pytest.mark.parametrize(
        "metadata", [{}, {"community_id": "x"}, {"community_id": COMMUNITY, "a": "b"}]
    )
    async def test_invalid_metadata_is_a_400_and_stores_nothing(
        self, sclient: Any, pc_db: Any, metadata: dict[str, Any]
    ) -> None:
        response = await _create(sclient, pc_db, metadata=metadata)
        assert response.status_code == 400
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_cross_community_and_cross_tenant_are_refused(
        self, sclient: Any, pc_db: Any
    ) -> None:
        cid = (await (await _create(sclient, pc_db)).get_json())["connection"]["id"]
        # A different community of the SAME tenant has no access grant to this connection.
        second = pc_db.seed.second_community_id
        got = await sclient.get(f"{_base(pc_db, second)}/{cid}", headers=admin_headers(pc_db))
        assert got.status_code == 404
        listed = await sclient.get(_base(pc_db, second), headers=admin_headers(pc_db))
        assert (await listed.get_json())["connections"] == []
        # An admin of ANOTHER tenant cannot reach this community's connections at all.
        other = {
            "Authorization": "Bearer "
            + make_user_token(
                user_id=pc_db.seed.admin_user_id, scope="connections:admin", tenant="other-corp"
            ),
            "Content-Type": "application/json",
        }
        denied = await sclient.get(f"{_base(pc_db)}/{cid}", headers=other)
        assert denied.status_code in (403, 404)
        assert TOKEN not in await denied.get_data(as_text=True)
        # ...and cannot delete it.
        gone = await sclient.delete(f"{_base(pc_db)}/{cid}", headers=other)
        assert gone.status_code in (403, 404)
        still = await sclient.get(f"{_base(pc_db)}/{cid}", headers=admin_headers(pc_db))
        assert still.status_code == 200

    async def test_data_plane_resolve_is_bound_to_its_community(
        self, sclient: Any, pc_db: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        cid = (await (await _create(sclient, pc_db)).get_json())["connection"]["id"]

        def bound(community_id: int) -> dict[str, str]:
            token, _ = service_issuer.issue_with_claims(
                SYSTEM_SERVICE_ID, "connections:resolve", extra_claims={"community": community_id}
            )
            return {"Authorization": f"Bearer {token}"}

        url = "{}/communities/{}/provider-connections/{}/resolve"
        ok = await sclient.post(
            url.format(INTERNAL, pc_db.seed.community_id, cid),
            headers=bound(pc_db.seed.community_id),
        )
        assert ok.status_code == 200
        out = await ok.get_json()
        assert out["accessToken"] == TOKEN  # the data plane (only) receives the session token
        assert out["metadata"]["community_id"] == COMMUNITY
        # A token bound to ANOTHER community cannot resolve it, nor can the URL be re-pointed.
        second = pc_db.seed.second_community_id
        wrong_bound = await sclient.post(
            url.format(INTERNAL, pc_db.seed.community_id, cid), headers=bound(second)
        )
        wrong_url = await sclient.post(url.format(INTERNAL, second, cid), headers=bound(second))
        for refused in (wrong_bound, wrong_url):
            assert refused.status_code in (403, 404)
            assert TOKEN not in await refused.get_data(as_text=True)

    async def test_admin_only(self, sclient: Any, pc_db: Any) -> None:
        no_scope = admin_headers(pc_db, scope="*:read")
        assert (await sclient.post(_base(pc_db), headers=no_scope, data=_body())).status_code == 403
        assert (await sclient.post(_base(pc_db), data=_body())).status_code == 401
        assert await raw_rows(pc_db, "platform_connections") == []


def test_kind_metadata_is_optional_org_only_and_reaches_the_data_plane() -> None:
    """The data plane derives roster (default) or events from the optional ``kind``; never lobby."""
    import re

    fields = {f.name: f for f in SPECTRUM_SPEC.metadata_fields}
    assert fields["community_id"].required is True
    assert fields["kind"].required is False
    assert fields["kind"].pattern is not None
    for ok in ("roster", "events"):
        assert re.fullmatch(fields["kind"].pattern, ok)
    for bad in ("lobby", "forum", "roster;x", ""):
        assert not re.fullmatch(fields["kind"].pattern, bad)
    assert "kind" in SPECTRUM_SPEC.runtime_metadata_keys
    wire = {(b.wire, b.required) for b in SPECTRUM_SPEC.binding_metadata}
    assert wire == {("community_id", True), ("kind", False)}
