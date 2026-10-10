"""Internal machine-JWT token endpoint.

Bootstrap endpoint for the per-service EdDSA machine JWT mechanism
(`flask_core.service_jwt`). A calling service (e.g. `svc-process`,
`svc-action`) authenticates with its own projected Kubernetes
ServiceAccount token instead of a shared secret; hub-api validates that
token via the TokenReview API, maps the resulting ServiceAccount identity
to an allow-listed `ServiceIdentity`, and mints a short-lived Ed25519 JWT
scoped to the requested `scope`.

Not mounted on the public API surface -- register only on hub-api's
internal/cluster-local listener (see chart README, `client.md`
"never call third-party APIs directly from client" applies symmetrically
here: only hub-api ever holds the signing key).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from flask_core.service_jwt import (
    DEFAULT_TOKEN_TTL_SECONDS,
    BootstrapRejected,
    ServiceJwtIssuer,
    verify_service_account_token,
)
from quart import Blueprint, Response, current_app, jsonify, request

service_jwt_bp = Blueprint("service_jwt", __name__, url_prefix="/internal")

#: Audience the bootstrap ServiceAccount token projection must carry
#: (Helm: `serviceJwt.bootstrapAudience` on each caller's pod spec).
BOOTSTRAP_AUDIENCE = os.getenv("SERVICE_JWT_BOOTSTRAP_AUDIENCE", "hub-api")
K8S_API_SERVER = os.getenv("KUBERNETES_SERVICE_HOST_URL", "https://kubernetes.default.svc")
K8S_CA_CERT_PATH = os.getenv(
    "KUBERNETES_CA_CERT_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
)


@service_jwt_bp.route("/service-token", methods=["POST"])
async def issue_service_token() -> tuple[Response, int]:
    """Exchange a caller's projected ServiceAccount token for a machine JWT.

    Request: `Authorization: Bearer <projected SA token>`, JSON body
    `{"scope": "..."}`. Response: `{"token": "...", "expires_in": <secs>}`.
    Returns 401 for any bootstrap failure -- wrong/expired/unauthenticated
    SA token, wrong audience, or a ServiceAccount not allow-listed for the
    requested scope -- without distinguishing which check failed.
    """
    issuer: ServiceJwtIssuer | None = current_app.config.get("SERVICE_JWT_ISSUER")
    if issuer is None:
        # Fails closed rather than raising a KeyError -- a deployment that
        # hasn't wired SERVICE_JWT_* config (see hub_api/app.py::create_app)
        # must reject every bootstrap attempt, not crash the whole request.
        return jsonify({"error": "service jwt issuance not configured"}), 503
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "missing bearer token"}), 401
    sa_token = auth_header[len("Bearer ") :]
    body = await request.get_json(silent=True) or {}
    scope = body.get("scope")
    if not scope:
        return jsonify({"error": "scope is required"}), 400

    try:
        # `verify_service_account_token` makes a blocking `requests.post`
        # call (the k8s TokenReview API) -- offload to a thread so this
        # coroutine never blocks the event loop for every other in-flight
        # request (penguin-python-dev: never sync I/O directly in an async
        # handler).
        namespace, service_account = await asyncio.to_thread(
            verify_service_account_token,
            sa_token=sa_token,
            audience=BOOTSTRAP_AUDIENCE,
            k8s_api_server=K8S_API_SERVER,
            ca_cert_path=K8S_CA_CERT_PATH,
        )
    except BootstrapRejected:
        current_app.logger.warning("service_jwt.bootstrap_rejected")
        return jsonify({"error": "unauthorized"}), 401

    matched = next(
        (
            identity
            for identity in issuer.identities.values()
            if identity.matches_service_account(namespace, service_account)
        ),
        None,
    )
    if matched is None:
        current_app.logger.warning(
            "service_jwt.no_matching_identity",
            extra={"namespace": namespace, "service_account": service_account},
        )
        return jsonify({"error": "unauthorized"}), 401

    try:
        token = issuer.issue(matched.service_id, scope, ttl_seconds=DEFAULT_TOKEN_TTL_SECONDS)
    except BootstrapRejected:
        return jsonify({"error": "unauthorized"}), 401

    # security.md audit logging -- record the issuance (who, what scope,
    # which token by `jti`, when it expires) without ever logging the
    # token itself. `jwt.decode` re-parse is unnecessary here: `issue()`
    # doesn't hand back the claims it minted, but every field logged is
    # already known to this call site except `jti`/`exp`, which the issuer
    # doesn't currently return either -- log what's known rather than
    # re-decoding the token just to extract `jti`.
    claims = _unverified_claims(token)
    logger = current_app.config.get("logger")
    if logger is not None:
        logger.audit(
            action="service_jwt.issue",
            user=matched.service_id,
            community="internal",
            result="SUCCESS",
            scope=scope,
            tenant=claims.get("tenant"),
            jti=claims.get("jti"),
            exp=claims.get("exp"),
        )

    return jsonify({"token": token, "expires_in": DEFAULT_TOKEN_TTL_SECONDS}), 200


def _unverified_claims(token: str) -> dict[str, Any]:
    """Pull `jti`/`exp` for the audit log without re-verifying the token.

    Safe here specifically because `token` was *just minted* by `issuer.
    issue()` on this same request -- there is no untrusted input to
    validate, only this process's own freshly-signed payload to read back.
    """
    import jwt as pyjwt

    return pyjwt.decode(token, options={"verify_signature": False})


@service_jwt_bp.route("/service-jwks.json", methods=["GET"])
async def service_jwks() -> tuple[Response, int]:
    """Public-key set for verifiers -- no auth required (public keys only)."""
    issuer: ServiceJwtIssuer | None = current_app.config.get("SERVICE_JWT_ISSUER")
    if issuer is None:
        return jsonify({"keys": []}), 200
    return jsonify(issuer.jwks()), 200
