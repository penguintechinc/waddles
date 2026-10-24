"""Live hub-api harness for ``core/connection_resolver/tests/e2e_live_hub.rs``.

Starts a REAL Postgres 17 container migrated to Alembic head, boots the REAL hub-api blueprints
(provider-connections admin + internal data-plane routes + the machine-token endpoint) over it with
the real vault, the real Ed25519 issuer/verifier and the real ``build_default_registry`` adapters,
serves them on a loopback port, writes a JSON manifest the Rust test reads, and blocks until
SIGTERM/SIGINT (then tears the container down).

Only three seams are substituted, none of them the system under test:

* the Kubernetes TokenReview call (``verify_service_account_token``) -- there is no cluster in CI;
  a fixed table maps two throwaway SA token strings to the chart's real ServiceAccounts;
* the PostHog/licence backends behind the entitlement client (real client, fake gates, as in the
  unit suite) so the static-connections flag is on;
* nothing else: identities come from ``helm template`` of the REAL chart (alpha values), so the
  live run also proves the chart's scope grants.

Usage: ``python3 -m tests.live.live_hub <manifest-path>`` from ``hub_api/`` with
``PYTHONPATH=../libs/flask_core``.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "alembic" / "tests"))

# Strong, per-run, in-memory secrets (never committed, never a real credential).
os.environ["SECRET_KEY"] = secrets.token_hex(32)
os.environ["CREDENTIAL_ENCRYPTION_KEY"] = secrets.token_hex(32)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from flask_core import entitlement  # noqa: E402
from flask_core.auth import create_jwt_token  # noqa: E402
from flask_core.database import AsyncDAL  # noqa: E402
from flask_core.service_jwt import (  # noqa: E402
    BootstrapRejected,
    ServiceJwtIssuer,
    SigningKey,
    load_identities_from_env,
)
from penguin_dal import AsyncDB  # noqa: E402
from pg_docker import migrated_postgres  # type: ignore[import-not-found]  # noqa: E402
from pydal import Field  # noqa: E402
from quart import Quart  # noqa: E402
from quart_schema import QuartSchema  # noqa: E402

import blueprints.service_jwt_bp as service_jwt_bp_module  # noqa: E402
from blueprints.service_jwt_bp import service_jwt_bp  # noqa: E402
from blueprints.v1.provider_connections import (  # noqa: E402
    provider_connections_bp,
    provider_connections_internal_bp,
)
from config import HubAPIConfig  # noqa: E402
from tests.provider_connections.conftest import hub_config  # noqa: E402
from tests.sso.conftest import Entitlements  # noqa: E402

TENANT_SLUG = "live-acme"
#: Throwaway ServiceAccount token strings the stubbed TokenReview recognises.
SA_TOKENS = {"live-sa-svc-action": "svc-action", "live-sa-svc-ingest": "svc-ingest"}


def _chart_identities_env() -> str:
    """Render the real alpha chart and return hub-api's ``SERVICE_JWT_IDENTITIES`` JSON."""
    chart = _REPO / "k8s" / "helm" / "waddlebot"
    rendered = subprocess.run(  # noqa: S603 - fixed argv
        [
            shutil.which("helm") or "helm", "template", "waddlebot", str(chart),
            "--kube-version", "1.30.0",
            "--values", str(chart / "values-alpha.yaml"),
            "--set", "pipeline.rustDataPlane.enabled=true",
            "--set", "pipeline.rustDataPlane.svcIngest.enabled=true",
        ],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    examined = 0
    for doc in yaml.safe_load_all(rendered.stdout):
        if not doc or doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            for env in container.get("env", []) or []:
                examined += 1
                if env.get("name") == "SERVICE_JWT_IDENTITIES":
                    return str(env["value"])
    raise SystemExit(f"SERVICE_JWT_IDENTITIES not rendered (env entries examined: {examined})")


def _fake_token_review(
    *, sa_token: str, audience: str, k8s_api_server: str, ca_cert_path: str
) -> tuple[str, str]:
    """TokenReview stand-in: known throwaway tokens -> the chart's namespace + ServiceAccount."""
    account = SA_TOKENS.get(sa_token)
    if account is None:
        raise BootstrapRejected("ServiceAccount token not authenticated")
    return os.environ["LIVE_NAMESPACE"], account


def _sql(container: str, statement: str) -> str:
    docker = shutil.which("docker") or "docker"
    argv = [docker, "exec", "-i", container, "psql", "-U", "waddlebot", "-d", "waddlebot"]
    result = subprocess.run(  # noqa: S603 - fixed argv
        [*argv, "-tAq", "-c", statement], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _seed(container: str) -> dict[str, int]:
    """One tenant with two communities (A, B) and a second tenant's community (C)."""
    _sql(container, "ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER")
    ids: dict[str, int] = {}
    for key, slug in (("tenant", TENANT_SLUG), ("other_tenant", "live-other")):
        ids[key] = int(
            _sql(container, f"INSERT INTO tenants (slug) VALUES ('{slug}') RETURNING id")  # noqa: S608
        )
    ids["admin_user"] = int(
        _sql(container, "INSERT INTO hub_users (username) VALUES ('live-admin') RETURNING id")
    )
    for key, name, tenant in (
        ("a", "live-a", "tenant"),
        ("b", "live-b", "tenant"),
        ("c", "live-c", "other_tenant"),
    ):
        ids[key] = int(
            _sql(
                container,
                f"INSERT INTO communities (name, tenant_id) VALUES ('{name}', {ids[tenant]})"  # noqa: S608
                " RETURNING id",
            )
        )
    return ids


async def _serve(manifest_path: Path) -> None:
    with migrated_postgres("connection-resolver-live") as pg:
        ids = _seed("waddles-migtest-connection-resolver-live")
        identities_env = _chart_identities_env()
        os.environ["SERVICE_JWT_IDENTITIES"] = identities_env
        identities = load_identities_from_env(env="alpha")
        os.environ["LIVE_NAMESPACE"] = next(iter(identities)).k8s_namespace
        private = Ed25519PrivateKey.generate()
        issuer = ServiceJwtIssuer(
            keys={"k1": SigningKey(kid="k1", private_key=private, public_key=private.public_key())},
            active_kid="k1",
            identities={i.service_id: i for i in identities},
            audience="waddlebot-internal",
        )
        service_jwt_bp_module.verify_service_account_token = _fake_token_review  # type: ignore[assignment]
        previous_client = entitlement._default_client
        Entitlements().set_tier("enterprise")

        pydal_uri = pg.dsn.replace("postgresql://", "postgres://", 1)
        async_dal = AsyncDAL(pydal_uri, pool_size=2, migrate=False)
        async_dal.dal.define_table(
            "tenants", Field("slug"), Field("is_active", "boolean"), migrate=False, rname="tenants"
        )
        install_dal = AsyncDB(pg.dsn, pool_size=4, echo=False)
        await install_dal.reflect()

        app = Quart(__name__)
        QuartSchema(app)
        app.register_blueprint(provider_connections_bp)
        app.register_blueprint(provider_connections_internal_bp)
        app.register_blueprint(service_jwt_bp)
        cfg: HubAPIConfig = hub_config()
        app.config.update(
            dal=async_dal.dal,
            async_dal=async_dal,
            install_dal=install_dal,
            HUB_API_CONFIG=cfg,
            SERVICE_JWT_ISSUER=issuer,
            SERVICE_JWT_VERIFIER=issuer.as_verifier(),
        )

        port = _free_port()
        admin_token = create_jwt_token(
            user_id=str(ids["admin_user"]), username="live-admin", email="admin@example.invalid",
            roles=["super_admin"], secret_key=os.environ["SECRET_KEY"],
            tenant=TENANT_SLUG, scope="connections:admin", expiration_seconds=3600,
        )  # fmt: skip
        manifest: dict[str, Any] = {
            "hub_url": f"http://127.0.0.1:{port}",
            "admin_token": admin_token,
            "tenant_id": ids["tenant"],
            "community_a": ids["a"],
            "community_b": ids["b"],
            "community_other_tenant": ids["c"],
            "pg_container": "waddles-migtest-connection-resolver-live",
            "sa_tokens": SA_TOKENS,
        }
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        server = asyncio.create_task(
            app.run_task(host="127.0.0.1", port=port, shutdown_trigger=stop.wait)
        )
        await _wait_listening(port)
        tmp = manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest))
        os.chmod(tmp, 0o600)
        tmp.rename(manifest_path)  # atomic: the script polls for this file
        await server
        await install_dal.close()
        entitlement._default_client = previous_client


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_listening(port: int, timeout_s: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        try:
            _r, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return
        except OSError:
            await asyncio.sleep(0.2)
    raise SystemExit("live hub-api never started listening")


def main() -> None:
    """Entry point: ``live_hub.py <manifest-path>``."""
    if len(sys.argv) != 2 or not shutil.which("helm") or not shutil.which("docker"):
        raise SystemExit("usage: live_hub.py <manifest-path> (needs helm + docker on PATH)")
    asyncio.run(_serve(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
