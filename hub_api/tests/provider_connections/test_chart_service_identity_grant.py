"""svc-ingest's machine-JWT grant for the data-plane connection routes, from the REAL Helm chart.

The internal routes (``/api/v1/internal/provider-connections`` and ``.../resolve``) demand a machine
JWT with scope ``connections:resolve`` and ``tenant == system``. hub-api only mints a scope the
caller's ``serviceJwt.identities[*].allowedScopes`` lists, rendered by the chart into hub-api's
``SERVICE_JWT_IDENTITIES`` env var. The shared fixtures hand-build an identity, which proves the
route logic but not that the *deployed policy* lets svc-ingest in. These tests render the chart
(``helm template`` against ``values-alpha.yaml``), load the rendered env var through the very
loader hub-api runs at startup (``flask_core.service_jwt.load_identities_from_env``), and mint and
present tokens through the real issuer, verifier and Quart routes.

Zero rendered documents / no hub-api Deployment / no identities is a hard failure, never a silent
pass (critical-rules.md Verification Integrity). ``helm`` missing is a skip locally but a FAILURE
when ``CI`` is set, so the gate cannot go green by not running.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import (
    BootstrapRejected,
    ServiceIdentity,
    ServiceJwtIssuer,
    SigningKey,
    load_identities_from_env,
)

from tests.provider_connections.conftest import SERVICE_AUDIENCE, admin_headers
from tests.provider_connections.test_api import INTERNAL, _create

_CHART_DIR = Path(__file__).resolve().parents[3] / "k8s" / "helm" / "waddlebot"
_SVC_INGEST = "spiffe://penguintech.io/alpha/svc-ingest"
_SCOPE = "connections:resolve"


def _render_identities_env() -> str:
    """Render the alpha chart and return hub-api's raw ``SERVICE_JWT_IDENTITIES`` JSON string."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI to verify the service-identity grant")
        pytest.skip("helm CLI not available in this environment")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [  # noqa: S607 - helm resolved from PATH by design
            "helm",
            "template",
            "waddlebot",
            str(_CHART_DIR),
            "--kube-version",
            "1.30.0",
            "--values",
            str(_CHART_DIR / "values-alpha.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    examined = 0
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            for env in container.get("env", []) or []:
                examined += 1
                if env.get("name") == "SERVICE_JWT_IDENTITIES":
                    return str(env["value"])
    pytest.fail(f"no SERVICE_JWT_IDENTITIES env var rendered (env entries examined: {examined})")


@pytest.fixture(scope="module")
def rendered_identities_env() -> str:
    """The chart's rendered ``SERVICE_JWT_IDENTITIES`` (rendered once per module)."""
    return _render_identities_env()


@pytest.fixture
def chart_identities(
    rendered_identities_env: str, monkeypatch: pytest.MonkeyPatch
) -> dict[str, ServiceIdentity]:
    """The identity allow-list exactly as hub-api's startup loader parses the chart's env var."""
    monkeypatch.setenv("SERVICE_JWT_IDENTITIES", rendered_identities_env)
    identities = load_identities_from_env(env="alpha")
    assert identities, "chart rendered zero service identities -- cannot be a pass"
    return {identity.service_id: identity for identity in identities}


@pytest.fixture
def service_issuer(chart_identities: dict[str, ServiceIdentity]) -> ServiceJwtIssuer:
    """Override the shared hand-built issuer: a real Ed25519 issuer over the CHART's allow-list."""
    private = Ed25519PrivateKey.generate()
    key = SigningKey(kid="k1", private_key=private, public_key=private.public_key())
    return ServiceJwtIssuer(
        keys={"k1": key},
        active_kid="k1",
        identities=chart_identities,
        audience=SERVICE_AUDIENCE,
    )


def test_chart_grants_svc_ingest_exactly_connections_resolve_on_the_system_tenant(
    chart_identities: dict[str, ServiceIdentity],
) -> None:
    identity = chart_identities[_SVC_INGEST]
    assert identity.allowed_scopes == frozenset({_SCOPE})  # minimum: that one scope, nothing else
    assert identity.tenant == "system"
    # Pinned to the dedicated ServiceAccount, never the chart-wide shared one.
    assert (identity.k8s_namespace, identity.k8s_service_account) != ("", "")
    assert identity.k8s_service_account == "svc-ingest"


def test_svc_ingest_identity_can_mint_connections_resolve_with_system_tenant(
    service_issuer: ServiceJwtIssuer,
) -> None:
    token, claims = service_issuer.issue_with_claims(_SVC_INGEST, _SCOPE)
    assert (claims["sub"], claims["scope"], claims["tenant"]) == (_SVC_INGEST, _SCOPE, "system")
    verified = service_issuer.as_verifier().verify(token, required_scope=_SCOPE)
    assert verified["tenant"] == "system"


@pytest.mark.parametrize(
    "scope",
    [
        "identity:ephemeral:mint",
        "identity:handle:resolve",
        "identity:displayname:read",
        "egress:connect",
        "connections:admin",
        "connections:resolve:*",
        "*:admin",
        "",
    ],
)
def test_svc_ingest_identity_cannot_mint_any_scope_beyond_connections_resolve(
    service_issuer: ServiceJwtIssuer, scope: str
) -> None:
    with pytest.raises(BootstrapRejected):
        service_issuer.issue(_SVC_INGEST, scope)


def test_no_other_chart_identity_may_mint_connections_resolve(
    service_issuer: ServiceJwtIssuer, chart_identities: dict[str, ServiceIdentity]
) -> None:
    others = [sid for sid in chart_identities if sid != _SVC_INGEST]
    assert others, "expected other service identities in the chart (svc-process, svc-action, ...)"
    for service_id in others:
        with pytest.raises(BootstrapRejected):
            service_issuer.issue(service_id, _SCOPE)


@pytest.mark.parametrize(
    "service_id",
    [
        "spiffe://penguintech.io/alpha/svc-ingest-evil",
        "spiffe://penguintech.io/beta/svc-ingest",  # same service, another environment
        "svc-ingest",  # bare name is not an allow-listed SPIFFE id
        "spiffe://penguintech.io/alpha/svc-unknown",
    ],
)
def test_an_unlisted_identity_cannot_mint_connections_resolve(
    service_issuer: ServiceJwtIssuer, service_id: str
) -> None:
    with pytest.raises(BootstrapRejected):
        service_issuer.issue(service_id, _SCOPE)


async def test_chart_minted_svc_ingest_token_reaches_the_internal_data_plane_routes(
    client: Any, pc_db: Any, bot_tokens: Any, service_issuer: ServiceJwtIssuer
) -> None:
    created = await _create(client, pc_db, bot_tokens)
    assert created.status_code == 201
    connection_id = (await created.get_json())["connection"]["id"]
    headers = {"Authorization": f"Bearer {service_issuer.issue(_SVC_INGEST, _SCOPE)}"}

    listed = await client.get(
        f"{INTERNAL}/provider-connections?provider=twitch&capability=chat_ingest", headers=headers
    )
    assert listed.status_code == 200
    (item,) = (await listed.get_json())["items"]
    assert item["connectionId"] == connection_id

    resolved = await client.post(
        f"{INTERNAL}/communities/{pc_db.seed.community_id}"
        f"/provider-connections/{connection_id}/resolve",
        headers=headers,
    )
    assert resolved.status_code == 200
    assert (await resolved.get_json())["accessToken"] == bot_tokens[0]


async def test_a_token_from_an_identity_without_the_scope_is_refused_by_the_routes(
    client: Any,
    pc_db: Any,
    service_issuer: ServiceJwtIssuer,
    chart_identities: dict[str, ServiceIdentity],
) -> None:
    process_id = next(sid for sid in chart_identities if sid.endswith("/svc-process"))
    egress_only = {"Authorization": f"Bearer {service_issuer.issue(process_id, 'egress:connect')}"}
    for method, url in (
        ("get", f"{INTERNAL}/provider-connections?provider=twitch"),
        (
            "post",
            f"{INTERNAL}/communities/{pc_db.seed.community_id}/provider-connections/1/resolve",
        ),
    ):
        response = await getattr(client, method)(url, headers=egress_only)
        assert response.status_code == 401, (method, url)


async def test_a_freshly_created_binding_is_listed_with_no_further_approval_or_enable_step(
    client: Any, pc_db: Any, bot_tokens: Any, service_issuer: ServiceJwtIssuer
) -> None:
    """Pins the lifecycle verdict: POST create => approved + enabled + active => data-plane list."""
    created = await _create(client, pc_db, bot_tokens)
    body = (await created.get_json())["connection"]
    assert (body["status"], body["access"], body["enabled"], body["usable"]) == (
        "active",
        "approved",
        True,
        True,
    )
    headers = {"Authorization": f"Bearer {service_issuer.issue(_SVC_INGEST, _SCOPE)}"}
    listed = await client.get(f"{INTERNAL}/provider-connections?provider=twitch", headers=headers)
    assert [i["connectionId"] for i in (await listed.get_json())["items"]] == [body["id"]]


async def test_an_admin_disabled_binding_is_hidden_until_re_enabled(
    client: Any, pc_db: Any, bot_tokens: Any, service_issuer: ServiceJwtIssuer
) -> None:
    """The only "manual" step in the lifecycle is an admin's own enabled toggle."""
    created = await _create(client, pc_db, bot_tokens, enabled=False)
    body = (await created.get_json())["connection"]
    assert (body["access"], body["enabled"], body["usable"]) == ("approved", False, False)
    headers = {"Authorization": f"Bearer {service_issuer.issue(_SVC_INGEST, _SCOPE)}"}
    url = f"{INTERNAL}/provider-connections?provider=twitch"
    assert (await (await client.get(url, headers=headers)).get_json())["items"] == []

    patched = await client.patch(
        f"/api/v1/communities/{pc_db.seed.community_id}/provider-connections/{body['id']}",
        headers=admin_headers(pc_db),
        data=json.dumps({"enabled": True}),
    )
    assert patched.status_code == 200
    items = (await (await client.get(url, headers=headers)).get_json())["items"]
    assert [i["connectionId"] for i in items] == [body["id"]]
