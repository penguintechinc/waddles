#!/usr/bin/env python3
"""Mints the committed `hub_api_service_jwt.json` fixture with hub-api's REAL issuer.

`tests/rooms_grpc_auth.rs` verifies these tokens with the Rust `ServiceJwtVerifier`, proving the
Rust verifier accepts exactly what `flask_core.service_jwt.ServiceJwtIssuer` produces (header
shape, raw r||s ES256 signature, claim set). Only the PUBLIC key is committed; the throwaway
private key never leaves this process.

Regenerate (from the repo root, with `cryptography` and `PyJWT` installed):

    PYTHONPATH=libs/flask_core python3 core/svc_streaming/tests/rooms_fixtures/mint_hub_api_service_jwt.py \
        > core/svc_streaming/tests/rooms_fixtures/hub_api_service_jwt.json

The tokens expire an hour after `iat`; the Rust test pins its clock to `iat + 10` so the fixture
never rots.
"""

import json
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask_core.service_jwt import (
    ServiceIdentity,
    ServiceJwtIssuer,
    SigningKey,
    spiffe_id,
)

KID = "fixture-1"
SERVICE_ID = spiffe_id("test", "hub-api")
SCOPES = ("streaming.rooms:read", "streaming.rooms:write", "streaming.rooms:moderate")

private_key = ec.generate_private_key(ec.SECP256R1())
issuer = ServiceJwtIssuer(
    keys={KID: SigningKey(kid=KID, private_key=private_key, public_key=private_key.public_key())},
    active_kid=KID,
    identities={
        SERVICE_ID: ServiceIdentity(
            service_id=SERVICE_ID,
            k8s_namespace="waddlebot",
            k8s_service_account="hub-api",
            allowed_scopes=frozenset(SCOPES),
            tenant="system",
        )
    },
    audience="waddlebot-internal",
)

tokens = {}
claims = {}
for scope in SCOPES:
    token, payload = issuer.issue_with_claims(SERVICE_ID, scope, ttl_seconds=3600)
    tokens[scope] = token
    claims[scope] = payload

public_pem = (
    private_key.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)

json.dump(
    {
        "kid": KID,
        "sub": SERVICE_ID,
        "issuer": "hub-api",
        "audience": "waddlebot-internal",
        "public_key_pem": public_pem,
        "tokens": tokens,
        "claims": claims,
    },
    sys.stdout,
    indent=2,
    sort_keys=True,
)
sys.stdout.write("\n")
