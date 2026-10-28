"""Resolve v2: the data-plane wire contract (static kinds, community-bound guard, list scope).

The golden fixtures under ``tests/fixtures/resolve_v2_*.json`` are the FROZEN wire contract the
Rust ``connection_resolver`` crate parses verbatim; this file is what keeps them honest. Set
``WRITE_GOLDEN=1`` to regenerate them after a deliberate contract change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from flask_core.service_jwt import ServiceJwtIssuer

from services.provider_connections.errors import VaultUnavailable
from tests.provider_connections.conftest import (
    BOT,
    CHANNEL_ID,
    CHANNEL_LOGIN,
    SYSTEM_SERVICE_ID,
)
from tests.provider_connections.support import (
    STATIC_BEARER,
    STATIC_URL,
    create_static,
    grant_to,
    raw_rows,
    set_column,
)
from tests.provider_connections.test_api import INTERNAL, _create, _svc

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
FAKE_EXPIRY = "2026-10-10T16:00:00+00:00"

AWS_DOC = {"access_key_id": "FAKE-ACCESS-KEY-ID", "secret_access_key": "FAKE-SECRET-ACCESS-KEY"}
GCP_DOC = {"client_email": "svc@fake-project.example", "private_key": "FAKE-PRIVATE-KEY"}


STATIC_SCOPE = "connections:resolve:static"


def _bound(
    issuer: ServiceJwtIssuer,
    community_id: int,
    connection_id: int | None = None,
    scope: str = STATIC_SCOPE,
) -> dict[str, str]:
    """Bearer for a community-bound resolve token (default: the static-capable scope)."""
    extra = {"community": community_id}
    if connection_id is not None:
        extra["conn"] = connection_id
    token, _ = issuer.issue_with_claims(SYSTEM_SERVICE_ID, scope, extra_claims=extra)
    return {"Authorization": f"Bearer {token}"}


def _url(community_id: int, connection_id: int) -> str:
    return f"{INTERNAL}/communities/{community_id}/provider-connections/{connection_id}/resolve"


async def _resolve(
    client: Any,
    issuer: ServiceJwtIssuer,
    community_id: int,
    conn: int,
    scope: str = STATIC_SCOPE,
) -> Any:
    return await client.post(
        _url(community_id, conn), headers=_bound(issuer, community_id, scope=scope)
    )


def _normalised(body: dict[str, Any]) -> dict[str, Any]:
    """Replace the run-dependent values (ids, token, expiry) so the body is comparable."""
    out = json.loads(json.dumps(body))
    out["connectionId"], out["communityId"] = 12, 7
    if out["accessToken"] is not None:
        out["accessToken"] = "FAKE-ACCESS-TOKEN"
    if out["expiresAt"] is not None:
        out["expiresAt"] = FAKE_EXPIRY
    return out


def _check_golden(name: str, body: dict[str, Any]) -> None:
    """Compare ``body`` to the committed fixture (``WRITE_GOLDEN=1`` rewrites it)."""
    path = FIXTURES / f"resolve_v2_{name}.json"
    text = json.dumps(_normalised(body), indent=2, sort_keys=False) + "\n"
    if os.environ.get("WRITE_GOLDEN") == "1":
        path.write_text(text)
    assert path.exists(), f"golden fixture {path.name} is missing"
    assert json.loads(path.read_text()) == json.loads(text), f"{path.name} drifted from the wire"


STATIC_CASES = [
    pytest.param(
        "external_http",
        1,
        {"token": STATIC_BEARER},
        {"url": STATIC_URL},
        {"url": STATIC_URL, "region": None, "function": None, "project": None},
        {"scheme": 1, "fields": {"token": STATIC_BEARER}},
        2,
        "http",
        id="http",
    ),
    pytest.param(
        "aws_lambda",
        4,
        AWS_DOC,
        {"region": "us-east-1", "function": "my-function"},
        {"url": None, "region": "us-east-1", "function": "my-function", "project": None},
        {
            "scheme": 4,
            "fields": {
                "accessKeyId": "FAKE-ACCESS-KEY-ID",
                "secretAccessKey": "FAKE-SECRET-ACCESS-KEY",
                "sessionToken": None,
            },
        },
        3,
        "lambda",
        id="lambda",
    ),
    pytest.param(
        "gcp_function",
        5,
        GCP_DOC,
        {"project": "fake-project-1", "region": "us-central1", "function": "my-func"},
        {
            "url": None,
            "region": "us-central1",
            "function": "my-func",
            "project": "fake-project-1",
        },
        {
            "scheme": 5,
            "fields": {"clientEmail": "svc@fake-project.example", "privateKey": "FAKE-PRIVATE-KEY"},
        },
        4,
        "gcp",
        id="gcp",
    ),
]


class TestResolveStaticKinds:
    @pytest.mark.parametrize(
        ("provider", "scheme", "doc", "meta", "endpoint", "secret", "kind", "golden"),
        STATIC_CASES,
    )
    async def test_resolve_static_kind_returns_structured_secret(
        self,
        static_client: Any,
        pc_static_service: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
        provider: str,
        scheme: int,
        doc: dict[str, str],
        meta: dict[str, str],
        endpoint: dict[str, Any],
        secret: dict[str, Any],
        kind: int,
        golden: str,
    ) -> None:
        view = await create_static(
            pc_static_service, pc_db, provider=provider, scheme=scheme, document=doc, metadata=meta
        )
        cid, conn = pc_db.seed.community_id, view.binding.connection.id
        response = await _resolve(static_client, service_issuer, cid, conn)
        assert response.status_code == 200
        body = await response.get_json()
        # exact key set and exact values: nothing extra may travel
        assert set(body) == {
            "success",
            "connectionId",
            "communityId",
            "provider",
            "kind",
            "accessToken",
            "expiresAt",
            "endpoint",
            "secret",
            "metadata",
        }
        assert body == {
            "success": True,
            "connectionId": conn,
            "communityId": cid,
            "provider": provider,
            "kind": kind,
            "accessToken": None,
            "expiresAt": None,
            "endpoint": endpoint,
            "secret": secret,
            "metadata": {},
        }
        assert response.headers["Cache-Control"] == "no-store"
        _check_golden(golden, body)

    async def test_resolve_oauth_kind_contract_unchanged(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        conn = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        response = await _resolve(client, service_issuer, pc_db.seed.community_id, conn)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["kind"] == 1 and body["secret"] is None
        assert body["accessToken"] == bot_tokens[0]
        assert body["endpoint"] == {"url": None, "region": None, "function": None, "project": None}
        assert bot_tokens[1] not in await response.get_data(as_text=True)
        # independent full-body literal: WRITE_GOLDEN can never bless a wrong OAuth fixture
        assert _normalised(body) == {
            "success": True,
            "connectionId": 12,
            "communityId": 7,
            "provider": "twitch",
            "kind": 1,
            "accessToken": "FAKE-ACCESS-TOKEN",
            "expiresAt": FAKE_EXPIRY,
            "endpoint": {"url": None, "region": None, "function": None, "project": None},
            "secret": None,
            "metadata": {
                "channel": CHANNEL_LOGIN,
                "bot_login": BOT.login,
                "channel_login": CHANNEL_LOGIN,
                "channel_id": CHANNEL_ID,
            },
        }
        _check_golden("oauth", body)


class TestGoldenFixturesMatchTheDto:
    @pytest.mark.parametrize("name", ["oauth", "http", "lambda", "gcp"])
    def test_every_fixture_parses_against_the_dto_schema(self, name: str) -> None:
        """A fixture can never drift from the DTO shape the Rust crate deserialises."""
        from dataclasses import fields

        from pydantic import TypeAdapter

        from blueprints.v1.provider_connections import (
            EndpointDTO,
            ResolvedCredentialsDTO,
            SecretDTO,
        )

        raw = json.loads((FIXTURES / f"resolve_v2_{name}.json").read_text())
        assert set(raw) == {f.name for f in fields(ResolvedCredentialsDTO)}
        assert set(raw["endpoint"]) == {f.name for f in fields(EndpointDTO)}
        if raw["secret"] is not None:
            assert set(raw["secret"]) == {f.name for f in fields(SecretDTO)}
        parsed = TypeAdapter(ResolvedCredentialsDTO).validate_python(raw)
        assert json.loads(TypeAdapter(ResolvedCredentialsDTO).dump_json(parsed)) == raw


class TestStaticScopeLeastPrivilege:
    @pytest.mark.parametrize(
        ("provider", "scheme", "doc", "meta", "kind"),
        [(c.values[0], c.values[1], c.values[2], c.values[3], c.values[6]) for c in STATIC_CASES],
    )
    async def test_base_resolve_scope_cannot_release_a_static_kind(
        self,
        static_client: Any,
        pc_static_service: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
        provider: str,
        scheme: int,
        doc: dict[str, str],
        meta: dict[str, str],
        kind: int,
    ) -> None:
        view = await create_static(
            pc_static_service, pc_db, provider=provider, scheme=scheme, document=doc, metadata=meta
        )
        cid, conn = pc_db.seed.community_id, view.binding.connection.id
        base = await _resolve(static_client, service_issuer, cid, conn, scope="connections:resolve")
        assert base.status_code == 403
        assert await base.get_json() == {"error": "forbidden"}  # generic: no secret, no reason
        text = await base.get_data(as_text=True)
        assert not any(v in text for v in doc.values())
        full = await _resolve(static_client, service_issuer, cid, conn, scope=STATIC_SCOPE)
        assert full.status_code == 200
        assert (await full.get_json())["kind"] == kind

    async def test_base_scope_still_resolves_an_oauth_connection(
        self, client: Any, pc_db: Any, bot_tokens: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        conn = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        base = await _resolve(
            client, service_issuer, pc_db.seed.community_id, conn, scope="connections:resolve"
        )
        assert base.status_code == 200 and (await base.get_json())["kind"] == 1

    async def test_the_static_scope_token_is_community_bound_like_the_base(
        self, static_client: Any, pc_static_service: Any, pc_db: Any, service_issuer: Any
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        wrong = await static_client.post(
            _url(pc_db.seed.community_id, view.binding.connection.id),
            headers=_bound(service_issuer, pc_db.seed.second_community_id),
        )
        assert wrong.status_code == 403
        unbound = {
            "Authorization": "Bearer " + service_issuer.issue(SYSTEM_SERVICE_ID, STATIC_SCOPE)
        }
        assert (
            await static_client.post(
                _url(pc_db.seed.community_id, view.binding.connection.id), headers=unbound
            )
        ).status_code == 401


class TestResolveErrors:
    async def test_resolve_requires_grant_for_this_community(
        self,
        static_client: Any,
        pc_static_service: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        """Granted to A only; a token bound to B gets 404 (not 403: no existence oracle)."""
        view = await create_static(pc_static_service, pc_db)
        conn = view.binding.connection.id
        other = pc_db.seed.second_community_id
        response = await _resolve(static_client, service_issuer, other, conn)
        assert response.status_code == 404
        absent = await _resolve(static_client, service_issuer, other, 99999)
        assert absent.status_code == 404
        # identical shape for "exists but not yours" and "does not exist"
        a = (await response.get_json())["error"]
        b = (await absent.get_json())["error"]
        assert (a["code"], a["message"]) == (b["code"], b["message"])

    async def test_a_token_bound_to_another_community_is_refused_by_the_guard(
        self,
        static_client: Any,
        pc_static_service: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        conn = view.binding.connection.id
        response = await static_client.post(
            _url(pc_db.seed.community_id, conn),
            headers=_bound(service_issuer, pc_db.seed.second_community_id),
        )
        assert response.status_code == 403
        unbound = await static_client.post(
            _url(pc_db.seed.community_id, conn), headers=_svc(service_issuer)
        )
        assert unbound.status_code == 401  # a list-scope token cannot resolve

    async def test_resolve_disabled_or_pending_grant_is_409_with_reason(
        self,
        static_client: Any,
        pc_static_service: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        cid = pc_db.seed.community_id
        disabled = await create_static(pc_static_service, pc_db, enabled=False)
        refused = await _resolve(static_client, service_issuer, cid, disabled.binding.connection.id)
        assert refused.status_code == 409
        assert await refused.get_json() == {
            "success": False,
            "error": "not_usable",
            "reason": "disabled",
        }

        pending = await create_static(
            pc_static_service,
            pc_db,
            community_id=pc_db.seed.second_community_id,
        )
        await set_column(
            pc_db, "community_connection_access", pending.binding.grant.id, status="pending"
        )
        refused = await _resolve(
            static_client,
            service_issuer,
            pc_db.seed.second_community_id,
            pending.binding.connection.id,
        )
        assert (refused.status_code, (await refused.get_json())["reason"]) == (
            409,
            "pending_approval",
        )

        revoked = await create_static(pc_static_service, pc_db)
        await set_column(
            pc_db, "community_connection_access", revoked.binding.grant.id, status="revoked"
        )
        refused = await _resolve(static_client, service_issuer, cid, revoked.binding.connection.id)
        assert (await refused.get_json())["reason"] == "revoked_grant"

        expired = await create_static(pc_static_service, pc_db)
        await set_column(
            pc_db, "platform_connections", expired.binding.connection.id, status="expired"
        )
        refused = await _resolve(static_client, service_issuer, cid, expired.binding.connection.id)
        assert refused.status_code == 409
        assert (await refused.get_json())["error"] == "not_usable"

    async def test_a_static_row_past_its_own_expiry_is_connection_expired(
        self,
        static_client: Any,
        pc_static_service: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        await set_column(
            pc_db,
            "platform_connections",
            view.binding.connection.id,
            expires_at="2020-01-01 00:00:00",
        )
        refused = await _resolve(
            static_client, service_issuer, pc_db.seed.community_id, view.binding.connection.id
        )
        assert refused.status_code == 409
        assert (await refused.get_json())["reason"] == "connection_expired"

    async def test_resolve_error_bodies_carry_no_secret(
        self,
        static_client: Any,
        pc_static_service: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        conn, cid = view.binding.connection.id, pc_db.seed.community_id
        (row,) = await raw_rows(pc_db, "platform_connections")
        sealed = row["sealed_secret"]

        # corrupt ciphertext -> generic 500, no ciphertext/plaintext/key text
        await set_column(
            pc_db, "platform_connections", conn, sealed_secret="AAAAAAAAAAAAAAAAAAAAAAAA"
        )
        corrupt = await _resolve(static_client, service_issuer, cid, conn)
        assert corrupt.status_code == 500
        text = await corrupt.get_data(as_text=True)
        assert "AAAAAAAA" not in text and STATIC_BEARER not in text

        # a valid ciphertext of a malformed document is also generic
        await set_column(pc_db, "platform_connections", conn, sealed_secret=sealed)
        monkeypatch.setattr(pc_static_service._vault, "open", lambda *a, **k: "not json")
        malformed = await _resolve(static_client, service_issuer, cid, conn)
        assert malformed.status_code == 500
        monkeypatch.setattr(
            pc_static_service._vault, "open", lambda *a, **k: json.dumps({"x": "y"})
        )
        assert (await _resolve(static_client, service_issuer, cid, conn)).status_code == 500

        # vault unavailable -> 503 with a fixed code
        def unavailable(*_a: Any, **_k: Any) -> str:
            raise VaultUnavailable("PLATFORM_ENCRYPTION_KEY is missing")

        monkeypatch.setattr(pc_static_service._vault, "open", unavailable)
        down = await _resolve(static_client, service_issuer, cid, conn)
        assert down.status_code == 503
        body = await down.get_data(as_text=True)
        assert "PLATFORM_ENCRYPTION_KEY" not in body and STATIC_BEARER not in body
        assert (await down.get_json())["error"]["code"] == "CONNECTIONS_UNAVAILABLE"

    async def test_resolve_response_is_no_store(
        self,
        static_client: Any,
        pc_static_service: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        cid, conn = pc_db.seed.community_id, view.binding.connection.id
        ok = await _resolve(static_client, service_issuer, cid, conn)
        miss = await _resolve(static_client, service_issuer, cid, 424242)
        for response in (ok, miss):
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Pragma"] == "no-cache"

    @pytest.mark.parametrize(
        ("stored", "emitted"),
        [
            ("secret_leak_abc", "other"),  # lowercase-underscore but not in the fixed set
            ("Free text: SECRET-LEAK 123", "other"),
            ("token_expired", "token_expired"),
            ("refresh_rejected", "refresh_rejected"),
        ],
    )
    async def test_a_reason_is_always_a_fixed_code(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        service_issuer: ServiceJwtIssuer,
        stored: str,
        emitted: str,
    ) -> None:
        conn = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        await set_column(
            pc_db, "platform_connections", conn, status="revoked", status_reason=stored
        )
        refused = await _resolve(client, service_issuer, pc_db.seed.community_id, conn)
        assert refused.status_code == 409
        body = await refused.get_json()
        assert body["reason"] == emitted
        if emitted == "other":
            assert stored not in json.dumps(body)


class TestListScopeSplit:
    async def test_list_requires_list_scope_resolve_scope_is_401(
        self, client: Any, pc_db: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        url = f"{INTERNAL}/provider-connections?provider=twitch"
        assert (await client.get(url, headers=_svc(service_issuer))).status_code == 200
        resolve_scoped = {
            "Authorization": "Bearer "
            + service_issuer.issue(SYSTEM_SERVICE_ID, "connections:resolve")
        }
        assert (await client.get(url, headers=resolve_scoped)).status_code == 401
        # and the unbound list token cannot resolve
        assert (
            await client.post(_url(pc_db.seed.community_id, 1), headers=_svc(service_issuer))
        ).status_code == 401

    async def test_list_never_returns_secret_fields(
        self,
        static_client: Any,
        pc_static_service: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        for provider, scheme, doc, meta, *_ in (c.values for c in STATIC_CASES):
            await create_static(
                pc_static_service,
                pc_db,
                provider=provider,
                scheme=scheme,
                document=doc,
                metadata=meta,
            )
            listed = await static_client.get(
                f"{INTERNAL}/provider-connections?provider={provider}", headers=_svc(service_issuer)
            )
            assert listed.status_code == 200
            text = await listed.get_data(as_text=True)
            (item,) = (await listed.get_json())["items"]
            assert (
                item["kind"] == {"external_http": 2, "aws_lambda": 3, "gcp_function": 4}[provider]
            )
            assert item["metadata"] == {}
            assert not any(secret in text for secret in (*doc.values(), "secret", "Secret"))
            assert "secret" not in item and "accessToken" not in item

    async def test_twitch_list_items_carry_kind(
        self, client: Any, pc_db: Any, bot_tokens: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        await _create(client, pc_db, bot_tokens)
        listed = await client.get(
            f"{INTERNAL}/provider-connections?provider=twitch", headers=_svc(service_issuer)
        )
        (item,) = (await listed.get_json())["items"]
        assert item["kind"] == 1


class TestTwitchBindingMetadata:
    async def test_twitch_binding_metadata_has_channel_and_bot_login(
        self, client: Any, pc_db: Any, bot_tokens: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        conn = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        expected = {
            "channel": CHANNEL_LOGIN,
            "bot_login": BOT.login,
            "channel_login": CHANNEL_LOGIN,
            "channel_id": CHANNEL_ID,
        }
        listed = await client.get(
            f"{INTERNAL}/provider-connections?provider=twitch", headers=_svc(service_issuer)
        )
        (item,) = (await listed.get_json())["items"]
        assert item["metadata"] == expected
        resolved = await _resolve(client, service_issuer, pc_db.seed.community_id, conn)
        assert (await resolved.get_json())["metadata"] == expected
        (row,) = await raw_rows(pc_db, "platform_connections")
        stored = json.loads(row["resource_metadata"])
        assert (
            stored["channel_login"] == CHANNEL_LOGIN and "channel" not in stored
        )  # storage unchanged

    @pytest.mark.parametrize("missing", ["channel_login", "bot_login"])
    async def test_twitch_binding_missing_channel_or_bot_login_is_excluded_loudly(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        service_issuer: ServiceJwtIssuer,
        caplog: pytest.LogCaptureFixture,
        missing: str,
    ) -> None:
        conn = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        (row,) = await raw_rows(pc_db, "platform_connections")
        stored = json.loads(row["resource_metadata"])
        del stored[missing]
        await set_column(pc_db, "platform_connections", conn, resource_metadata=json.dumps(stored))
        caplog.set_level("WARNING")
        listed = await client.get(
            f"{INTERNAL}/provider-connections?provider=twitch", headers=_svc(service_issuer)
        )
        assert listed.status_code == 200 and (await listed.get_json())["items"] == []
        resolved = await _resolve(client, service_issuer, pc_db.seed.community_id, conn)
        assert resolved.status_code == 409
        assert (await resolved.get_json())["reason"] == "metadata_incomplete"
        lines = [r.getMessage() for r in caplog.records if "binding_incomplete" in r.getMessage()]
        assert lines and all(f"connection_id={conn}" in line and missing in line for line in lines)
        # never a value
        assert not any(CHANNEL_LOGIN in line or BOT.login in line for line in lines)

    async def test_an_incomplete_binding_is_refused_before_any_refresh(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        service_issuer: ServiceJwtIssuer,
        clock: Any,
        fake_twitch: Any,
    ) -> None:
        conn = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        (row,) = await raw_rows(pc_db, "platform_connections")
        stored = json.loads(row["resource_metadata"])
        del stored["bot_login"]
        await set_column(pc_db, "platform_connections", conn, resource_metadata=json.dumps(stored))
        clock.advance(seconds=14000)  # the token is stale: a resolve WOULD refresh
        resolved = await _resolve(client, service_issuer, pc_db.seed.community_id, conn)
        assert resolved.status_code == 409
        assert fake_twitch.refresh_calls == 0
        (after,) = await raw_rows(pc_db, "platform_connections")
        assert after["access_token"] == row["access_token"]  # nothing was rotated

    async def test_a_numeric_passthrough_value_is_coerced_not_dropped(
        self, pc_service: Any, pc_db: Any
    ) -> None:
        out = pc_service.binding_metadata(
            "twitch",
            {"channel_login": "abc", "bot_login": "bot", "channel_id": 7001},
            connection_id=1,
        )
        assert out == {
            "channel": "abc",
            "bot_login": "bot",
            "channel_login": "abc",
            "channel_id": "7001",
        }

    async def test_metadata_keys_are_allowlisted_per_provider(
        self, client: Any, pc_db: Any, bot_tokens: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        conn = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        (row,) = await raw_rows(pc_db, "platform_connections")
        stored = json.loads(row["resource_metadata"])
        stored["internal_note"] = "must-not-leak"
        await set_column(pc_db, "platform_connections", conn, resource_metadata=json.dumps(stored))
        listed = await client.get(
            f"{INTERNAL}/provider-connections?provider=twitch", headers=_svc(service_issuer)
        )
        resolved = await _resolve(client, service_issuer, pc_db.seed.community_id, conn)
        for response in (listed, resolved):
            text = await response.get_data(as_text=True)
            assert "must-not-leak" not in text and "client_id" not in text
            assert "bot_user_id" not in text and "scopes" not in text

    async def test_a_static_connection_granted_to_a_second_community_resolves_per_community(
        self,
        static_client: Any,
        pc_static_service: Any,
        pc_db: Any,
        service_issuer: ServiceJwtIssuer,
    ) -> None:
        view = await create_static(pc_static_service, pc_db)
        conn = view.binding.connection.id
        await grant_to(pc_db, community_id=pc_db.seed.second_community_id, connection_id=conn)
        second = await _resolve(static_client, service_issuer, pc_db.seed.second_community_id, conn)
        assert second.status_code == 200
        assert (await second.get_json())["communityId"] == pc_db.seed.second_community_id
