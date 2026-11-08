"""Least-privilege machine scopes for the `gameserver-net` host capability.

A credential holding only ``connections:{list,resolve}:gameserver`` reaches `gameserver` rows and
nothing else: other providers are refused (list 403) or not found (resolve 404, checked before any
refresh), and the scopes can never mint the generic connection scopes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import (
    BootstrapRejected,
    ServiceIdentity,
    ServiceJwtIssuer,
    SigningKey,
)
from quart import Quart

from blueprints.v1.gameserver_connections import gameserver_connections_bp
from blueprints.v1.provider_connections import SERVICE_CONFIG_KEY
from services.provider_connections import PlatformKeyVault, ProviderConnectionService
from services.provider_connections.gameserver import GameServerAdapter
from tests.provider_connections.conftest import (
    SERVICE_AUDIENCE,
    FakeClock,
    admin_headers,
    make_registry,
)
from tests.provider_connections.test_api import INTERNAL, _create

GS_LIST = "connections:list:gameserver"
GS_RESOLVE = "connections:resolve:gameserver"
SID = "spiffe://penguintech.io/alpha/svc-process"
HOST = "93.184.216.34"
PASSWORD = "Sup3r|Secret-pw"


@pytest.fixture
def service_issuer() -> ServiceJwtIssuer:
    """An issuer for an identity holding ONLY the two gameserver-scoped machine scopes."""
    private = Ed25519PrivateKey.generate()
    key = SigningKey(kid="k1", private_key=private, public_key=private.public_key())
    identity = ServiceIdentity(
        service_id=SID,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-process",
        allowed_scopes=frozenset({GS_LIST, GS_RESOLVE}),
        tenant="system",
    )
    return ServiceJwtIssuer(
        keys={"k1": key}, active_kid="k1", identities={SID: identity}, audience=SERVICE_AUDIENCE
    )


@pytest.fixture
def mixed_app(pc_app: Quart, pc_db: Any, clock: FakeClock, fake_twitch: Any) -> Quart:
    pc_app.register_blueprint(gameserver_connections_bp)
    registry = make_registry(fake_twitch, clock)
    registry.register(GameServerAdapter())
    pc_app.config[SERVICE_CONFIG_KEY] = ProviderConnectionService(
        pc_db.install_dal, registry, PlatformKeyVault(), clock=clock
    )
    return pc_app


async def test_gameserver_scoped_credential_reaches_gameserver_rows_only(
    mixed_app: Quart, pc_db: Any, service_issuer: ServiceJwtIssuer, bot_tokens: Any
) -> None:
    client = mixed_app.test_client()
    community = pc_db.seed.community_id
    base = f"/api/v1/communities/{community}/gameserver-connections"
    body = {"host": HOST, "port": 27015, "rconPassword": PASSWORD, "game": "source"}
    created = await client.post(base, headers=admin_headers(pc_db), data=json.dumps(body))
    assert created.status_code == 201
    gs = (await created.get_json())["server"]
    tw = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]

    def bearer(scope: str, **extra: Any) -> dict[str, str]:
        token, _ = service_issuer.issue_with_claims(SID, scope, extra_claims=extra)
        return {"Authorization": f"Bearer {token}"}

    listed = await client.get(
        f"{INTERNAL}/provider-connections?provider=gameserver", headers=bearer(GS_LIST)
    )
    assert listed.status_code == 200
    (item,) = (await listed.get_json())["items"]
    assert item["metadata"]["server_id"] == gs["serverId"]
    assert PASSWORD not in await listed.get_data(as_text=True)
    refused = await client.get(
        f"{INTERNAL}/provider-connections?provider=twitch", headers=bearer(GS_LIST)
    )
    assert refused.status_code == 403

    url = f"{INTERNAL}/communities/{community}/provider-connections/{gs['connectionId']}/resolve"
    ok = await client.post(url, headers=bearer(GS_RESOLVE, community=community))
    assert ok.status_code == 200
    assert (await ok.get_json())["accessToken"] == f"{HOST}|27015|{PASSWORD}"

    twitch_url = f"{INTERNAL}/communities/{community}/provider-connections/{tw}/resolve"
    miss = await client.post(twitch_url, headers=bearer(GS_RESOLVE, community=community))
    assert miss.status_code == 404
    assert bot_tokens[0] not in await miss.get_data(as_text=True)


@pytest.mark.parametrize(
    "scope", ["connections:resolve", "connections:list", "connections:resolve:static"]
)
def test_gameserver_identity_cannot_mint_the_generic_connection_scopes(
    service_issuer: ServiceJwtIssuer, scope: str
) -> None:
    with pytest.raises(BootstrapRejected):
        service_issuer.issue(SID, scope)
