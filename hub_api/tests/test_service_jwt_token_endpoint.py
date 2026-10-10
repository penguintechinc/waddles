"""`POST /internal/service-token` end to end (only the k8s TokenReview call is faked).

Covers the audit trail the endpoint writes for every issuance: `jti`/`exp`/`tenant` come from
the claims the issuer just signed (`ServiceJwtIssuer.issue_with_claims`), and the endpoint never
re-decodes its own token with signature verification disabled (H-2 Phase 0 removed that).
"""

from __future__ import annotations

from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import (
    BootstrapRejected,
    ServiceIdentity,
    ServiceJwtIssuer,
    SigningKey,
    spiffe_id,
)
from quart import Quart

from blueprints import service_jwt_bp as bp_module

SCOPE = "identity:ephemeral:mint"
SERVICE_ID = spiffe_id("alpha", "svc-process")


class _AuditLog:
    """Captures `logger.audit(...)` calls the way hub-api's AAA logger receives them."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def audit(self, **fields: Any) -> None:
        self.events.append(fields)


def _issuer(tenant: str = "acme") -> ServiceJwtIssuer:
    priv = Ed25519PrivateKey.generate()
    identity = ServiceIdentity(
        service_id=SERVICE_ID,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-process",
        allowed_scopes=frozenset({SCOPE}),
        tenant=tenant,
    )
    return ServiceJwtIssuer(
        keys={"k1": SigningKey(kid="k1", private_key=priv, public_key=priv.public_key())},
        active_kid="k1",
        identities={SERVICE_ID: identity},
        audience="waddlebot-internal",
    )


@pytest.fixture
def audit() -> _AuditLog:
    return _AuditLog()


@pytest.fixture
def app(audit: _AuditLog, monkeypatch: pytest.MonkeyPatch) -> Quart:
    quart_app = Quart(__name__)
    quart_app.register_blueprint(bp_module.service_jwt_bp)
    quart_app.config["SERVICE_JWT_ISSUER"] = _issuer()
    quart_app.config["logger"] = audit
    monkeypatch.setattr(
        bp_module, "verify_service_account_token", lambda **_kw: ("waddlebot", "svc-process")
    )
    return quart_app


async def _post(app: Quart, *, scope: str | None = SCOPE, bearer: str | None = "sa-token") -> Any:
    headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}
    body = {"scope": scope} if scope is not None else {}
    return await app.test_client().post("/internal/service-token", headers=headers, json=body)


async def test_token_issued_and_verifies_with_the_issuers_own_verifier(app: Quart) -> None:
    response = await _post(app)
    assert response.status_code == 200
    body = await response.get_json()
    claims = (
        app.config["SERVICE_JWT_ISSUER"].as_verifier().verify(body["token"], required_scope=SCOPE)
    )
    assert claims["sub"] == SERVICE_ID
    assert set(body) == {"token", "expires_in"}  # exact response shape, no claims leaked


async def test_audit_event_carries_the_signed_claims_and_never_the_token(
    app: Quart, audit: _AuditLog
) -> None:
    response = await _post(app)
    body = await response.get_json()
    signed = jwt.decode(body["token"], options={"verify_signature": False})
    assert len(audit.events) == 1
    event = audit.events[0]
    assert (event["action"], event["user"], event["result"]) == (
        "service_jwt.issue",
        SERVICE_ID,
        "SUCCESS",
    )
    assert (event["jti"], event["exp"], event["tenant"], event["scope"]) == (
        signed["jti"],
        signed["exp"],
        "acme",
        SCOPE,
    )
    assert body["token"] not in repr(event)


async def test_endpoint_never_decodes_its_own_token_unverified(
    app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("jwt.decode must not be called while issuing")

    monkeypatch.setattr(jwt, "decode", forbidden)
    assert (await _post(app)).status_code == 200


async def test_unconfigured_issuer_fails_closed(app: Quart) -> None:
    app.config["SERVICE_JWT_ISSUER"] = None
    assert (await _post(app)).status_code == 503


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [({"bearer": None}, 401), ({"scope": None}, 400)],
    ids=["no-bearer", "no-scope"],
)
async def test_request_validation(app: Quart, kwargs: dict[str, Any], status: int) -> None:
    assert (await _post(app, **kwargs)).status_code == status


async def test_rejected_service_account_token_is_401(
    app: Quart, monkeypatch: pytest.MonkeyPatch, audit: _AuditLog
) -> None:
    def reject(**_kw: Any) -> tuple[str, str]:
        raise BootstrapRejected("nope")

    monkeypatch.setattr(bp_module, "verify_service_account_token", reject)
    app.logger.disabled = True
    assert (await _post(app)).status_code == 401
    assert audit.events == []


async def test_unknown_service_account_is_401(
    app: Quart, monkeypatch: pytest.MonkeyPatch, audit: _AuditLog
) -> None:
    monkeypatch.setattr(
        bp_module, "verify_service_account_token", lambda **_kw: ("other", "intruder")
    )
    assert (await _post(app)).status_code == 401
    assert audit.events == []


async def test_scope_not_allow_listed_is_401_and_not_audited_as_success(
    app: Quart, audit: _AuditLog
) -> None:
    assert (await _post(app, scope="identity:other:scope")).status_code == 401
    assert audit.events == []


async def test_missing_audit_logger_does_not_break_issuance(app: Quart) -> None:
    app.config["logger"] = None
    assert (await _post(app)).status_code == 200
