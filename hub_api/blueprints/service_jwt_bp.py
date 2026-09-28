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

import os

from flask import Blueprint, current_app, jsonify, request
from flask_core.service_jwt import (
    DEFAULT_TOKEN_TTL_SECONDS,
    BootstrapRejected,
    ServiceJwtIssuer,
    verify_service_account_token,
)

service_jwt_bp = Blueprint("service_jwt", __name__, url_prefix="/internal")

#: Audience the bootstrap ServiceAccount token projection must carry
#: (Helm: `serviceAccountToken.audience` on each caller's pod spec).
BOOTSTRAP_AUDIENCE = os.getenv("SERVICE_JWT_BOOTSTRAP_AUDIENCE", "waddlebot-internal-bootstrap")
K8S_API_SERVER = os.getenv("KUBERNETES_SERVICE_HOST_URL", "https://kubernetes.default.svc")
K8S_CA_CERT_PATH = os.getenv(
    "KUBERNETES_CA_CERT_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
)


@service_jwt_bp.route("/service-token", methods=["POST"])
def issue_service_token() -> tuple[dict, int]:
    """Exchange a caller's projected ServiceAccount token for a machine JWT.

    Request: `Authorization: Bearer <projected SA token>`, JSON body
    `{"scope": "..."}`. Response: `{"token": "...", "expires_in": <secs>}`.
    Returns 401 for any bootstrap failure -- wrong/expired/unauthenticated
    SA token, wrong audience, or a ServiceAccount not allow-listed for the
    requested scope -- without distinguishing which check failed.
    """
    issuer: ServiceJwtIssuer = current_app.config["SERVICE_JWT_ISSUER"]
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "missing bearer token"}), 401
    sa_token = auth_header[len("Bearer ") :]
    body = request.get_json(silent=True) or {}
    scope = body.get("scope")
    if not scope:
        return jsonify({"error": "scope is required"}), 400

    try:
        namespace, service_account = verify_service_account_token(
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

    return jsonify({"token": token, "expires_in": DEFAULT_TOKEN_TTL_SECONDS}), 200


@service_jwt_bp.route("/service-jwks.json", methods=["GET"])
def service_jwks() -> tuple[dict, int]:
    """Public-key set for verifiers -- no auth required (public keys only)."""
    issuer: ServiceJwtIssuer = current_app.config["SERVICE_JWT_ISSUER"]
    return jsonify(issuer.jwks()), 200
