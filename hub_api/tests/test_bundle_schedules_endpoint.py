"""`GET /api/v1/internal/bundle-schedules` -- real machine JWTs, real activation path, real DBs.

Real: Ed25519 service JWTs through `machine_authenticated`, `activate_*_with_warnings` (so the
AUTO-BIND `app_source_bindings` row is written by production code, not seeded by the test), the
service's SQL joins. sqlite (`install_dal`) covers scope/filters/exclusions fast; the Postgres
class below re-runs the listing against the real migrated schema (FKs, composite PKs, JSONB).
A SKIP of the Postgres tests means docker is absent -- treat that as a gate FAILURE.
"""

# ruff: noqa: F811  -- imported pytest fixtures (`pg_db`/`pg_dal`) are re-bound as test params

from __future__ import annotations

import itertools
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey, spiffe_id
from quart import Quart
from quart_schema import QuartSchema
from sqlalchemy import delete, select, text

from blueprints.v1 import bundle_schedules
from services.bundle_approval_service import (
    activate_for_community_with_warnings,
    activate_tenant_wide_with_warnings,
)
from services.bundle_manifest_v2 import app_id_slug
from services.tenant_app_availability_service import set_available
from tests.test_bundle_activation_commands import _manifest
from tests.test_bundle_activation_service import _install_and_make_available
from tests.test_seed_core_bundles_pg_integration import (  # noqa: F401  # fixtures
    pg_dal,
    pg_db,
)

SCOPE = bundle_schedules.SCOPE_BUNDLE_SCHEDULES_READ
INGEST = spiffe_id("alpha", "svc-ingest")
ROGUE = spiffe_id("alpha", "svc-rogue")
NO_SCOPE = spiffe_id("alpha", "svc-other")
TENANTED = spiffe_id("alpha", "svc-tenanted")
URL = "/api/v1/internal/bundle-schedules"
FIELDS = {
    "tenant_id",
    "community_id",
    "tenant_slug",
    "community_name",
    "app_id",
    "interval_s",
    "jitter",
}
_SEQ = itertools.count(1)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUNDLE_SCHEDULES_ALLOWED_SERVICES", raising=False)


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    priv = Ed25519PrivateKey.generate()
    ids = {
        INGEST: ServiceIdentity(INGEST, "waddlebot", "svc-ingest", frozenset({SCOPE}), "system"),
        ROGUE: ServiceIdentity(ROGUE, "waddlebot", "svc-rogue", frozenset({SCOPE}), "system"),
        NO_SCOPE: ServiceIdentity(
            NO_SCOPE, "waddlebot", "svc-other", frozenset({"connections:list"}), "system"
        ),
        TENANTED: ServiceIdentity(TENANTED, "waddlebot", "svc-ingest", frozenset({SCOPE}), "acme"),
    }
    return ServiceJwtIssuer(
        keys={"k1": SigningKey("k1", priv, priv.public_key())}, active_kid="k1", identities=ids
    )


def _app(install_dal: Any, issuer: ServiceJwtIssuer) -> Quart:
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(bundle_schedules.bundle_schedules_internal_bp)
    app.config["install_dal"] = install_dal
    app.config["SERVICE_JWT_ISSUER"] = issuer
    app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
    return app


def _hdr(issuer: ServiceJwtIssuer, service: str = INGEST, scope: str = SCOPE) -> dict[str, str]:
    token, _ = issuer.issue_with_claims(service, scope, ttl_seconds=300, extra_claims=None)
    return {"Authorization": f"Bearer {token}"}


def _scheduled(app_id: str, *, interval_s: int = 90, jitter: float | None = 0.25) -> dict[str, Any]:
    """`_manifest` + the hub consume rule + `schedule` block."""
    manifest = _manifest(app_id)
    manifest["stages"]["process"]["consumes"].append(
        {"platform": "hub", "event_types": ["bundle.reconcile"]}
    )
    manifest["schedule"] = {"interval_s": interval_s}
    if jitter is not None:
        manifest["schedule"]["jitter"] = jitter
    return manifest


async def _get(app: Quart, issuer: ServiceJwtIssuer, query: str = "") -> tuple[int, Any]:
    resp = await app.test_client().get(f"{URL}{query}", headers=_hdr(issuer))
    return resp.status_code, await resp.get_json()


# ----------------------------------------------------------------------------- sqlite (fast)


async def _publish_and_activate(
    install_dal: Any, app_id: str, manifest: dict[str, Any], community_id: int | None
) -> None:
    await _install_and_make_available(install_dal, manifest=manifest)
    if community_id is None:
        await activate_tenant_wide_with_warnings(
            install_dal, tenant_id=1, app_id=app_id, activated_by=1, valkey_client=AsyncMock()
        )
    else:
        await activate_for_community_with_warnings(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            app_id=app_id,
            activated_by=1,
            valkey_client=AsyncMock(),
        )


async def test_missing_token_is_401(install_dal: Any, issuer: ServiceJwtIssuer) -> None:
    resp = await _app(install_dal, issuer).test_client().get(URL)
    assert resp.status_code == 401


async def test_wrong_scope_is_refused(install_dal: Any, issuer: ServiceJwtIssuer) -> None:
    """Scope refusal: a valid service token without `bundle-schedules:read` never lists."""
    app = _app(install_dal, issuer)
    resp = await app.test_client().get(URL, headers=_hdr(issuer, NO_SCOPE, "connections:list"))
    assert resp.status_code == 401
    assert "targets" not in (await resp.get_json())


async def test_non_system_tenant_is_403(install_dal: Any, issuer: ServiceJwtIssuer) -> None:
    app = _app(install_dal, issuer)
    resp = await app.test_client().get(URL, headers=_hdr(issuer, TENANTED))
    assert resp.status_code == 403


async def test_non_allowlisted_caller_is_403(install_dal: Any, issuer: ServiceJwtIssuer) -> None:
    app = _app(install_dal, issuer)
    resp = await app.test_client().get(URL, headers=_hdr(issuer, ROGUE))
    assert resp.status_code == 403
    assert await resp.get_json() == {"error": "forbidden"}


async def test_unconfigured_verifier_fails_closed_503(
    install_dal: Any, issuer: ServiceJwtIssuer
) -> None:
    app = _app(install_dal, issuer)
    app.config.pop("SERVICE_JWT_VERIFIER")
    resp = await app.test_client().get(URL, headers=_hdr(issuer))
    assert resp.status_code == 503


async def test_empty_list_when_nothing_is_scheduled(
    install_dal: Any, issuer: ServiceJwtIssuer
) -> None:
    """An active bundle with no `schedule` is not listed; the body is still `{targets: []}`."""
    community = await install_dal.communities.async_insert(tenant_id=1, name="plain-community")
    await _publish_and_activate(
        install_dal,
        "waddles.socials.plain.default",
        _manifest("waddles.socials.plain.default"),
        community,
    )
    status, body = await _get(_app(install_dal, issuer), issuer)
    assert (status, body) == (200, {"targets": []})


async def test_lists_scheduled_scope_with_exact_field_set(
    install_dal: Any, issuer: ServiceJwtIssuer
) -> None:
    """Regression: the row carries exactly the svc-ingest `TargetDto` fields -- no extras leak."""
    community = await install_dal.communities.async_insert(tenant_id=1, name="sched-community")
    app_id = "waddles.socials.sched.default"
    await _publish_and_activate(install_dal, app_id, _scheduled(app_id), community)
    await _publish_and_activate(
        install_dal,
        "waddles.socials.plain2.default",
        _manifest("waddles.socials.plain2.default"),
        community,
    )

    status, body = await _get(_app(install_dal, issuer), issuer)

    assert status == 200
    assert set(body) == {"targets"}
    assert len(body["targets"]) == 1
    row = body["targets"][0]
    assert set(row) == FIELDS
    assert row["tenant_id"] == 1
    assert row["community_id"] == community
    assert row["community_name"] == "sched-community"
    assert row["app_id"] == app_id
    assert (row["interval_s"], row["jitter"]) == (90, 0.25)
    assert isinstance(row["tenant_slug"], str) and row["tenant_slug"]


async def test_activation_binds_hub_stream_on_app_id_slug(
    install_dal: Any, issuer: ServiceJwtIssuer
) -> None:
    """The consumes-binding is written by activation on `app_id_slug(app_id)`, not an ingest src."""
    community = await install_dal.communities.async_insert(tenant_id=1, name="bind-community")
    app_id = "waddles.socials.Bind_Me.default"
    await _publish_and_activate(install_dal, app_id, _scheduled(app_id), community)

    table = install_dal.metadata.tables["app_source_bindings"]
    async with install_dal.engine.connect() as conn:
        rows = (
            await conn.execute(
                select(table.c.platform, table.c.source_id).where(table.c.app_id == app_id)
            )
        ).all()
    assert [(r.platform, r.source_id) for r in rows] == [("hub", app_id_slug(app_id))]
    assert app_id_slug(app_id) == "waddles-socials-bind_me-default"


async def test_tenant_wide_activation_is_community_zero_without_name(
    install_dal: Any, issuer: ServiceJwtIssuer
) -> None:
    app_id = "waddles.socials.wide.default"
    await _publish_and_activate(install_dal, app_id, _scheduled(app_id, jitter=None), None)

    status, body = await _get(_app(install_dal, issuer), issuer, "?app_id=" + app_id)

    assert status == 200
    [row] = body["targets"]
    assert (row["community_id"], row["community_name"]) == (0, None)
    assert row["jitter"] == 0.2  # manifest default


async def test_filters_narrow_and_empty_results(install_dal: Any, issuer: ServiceJwtIssuer) -> None:
    c1 = await install_dal.communities.async_insert(tenant_id=1, name="f-one")
    c2 = await install_dal.communities.async_insert(tenant_id=1, name="f-two")
    a1, a2 = "waddles.socials.fone.default", "waddles.socials.ftwo.default"
    await _publish_and_activate(install_dal, a1, _scheduled(a1), c1)
    await _publish_and_activate(install_dal, a2, _scheduled(a2), c2)
    app = _app(install_dal, issuer)

    _, everything = await _get(app, issuer)
    assert {t["app_id"] for t in everything["targets"]} == {a1, a2}
    _, by_community = await _get(app, issuer, f"?community_id={c2}")
    assert [t["app_id"] for t in by_community["targets"]] == [a2]
    _, by_app = await _get(app, issuer, f"?app_id={a1}")
    assert [t["community_id"] for t in by_app["targets"]] == [c1]
    _, by_tenant = await _get(app, issuer, "?tenant_id=1")
    assert len(by_tenant["targets"]) == 2
    _, other_tenant = await _get(app, issuer, "?tenant_id=999")
    assert other_tenant == {"targets": []}
    _, combined = await _get(app, issuer, f"?community_id={c1}&app_id={a2}")
    assert combined == {"targets": []}


@pytest.mark.parametrize(
    "query",
    ["?tenant_id=0", "?tenant_id=-4", f"?community_id={2**31}", "?app_id=Bad%20Id", "?app_id=-x"],
)
async def test_invalid_filter_is_400(
    install_dal: Any, issuer: ServiceJwtIssuer, query: str
) -> None:
    status, _ = await _get(_app(install_dal, issuer), issuer, query)
    assert status == 400


async def test_missing_binding_is_excluded_loudly(
    install_dal: Any, issuer: ServiceJwtIssuer, caplog: pytest.LogCaptureFixture
) -> None:
    """No `(hub, slug)` binding -> svc-process has no consumer: excluded + ERROR log, not listed."""
    community = await install_dal.communities.async_insert(tenant_id=1, name="nobind-community")
    app_id = "waddles.socials.nobind.default"
    await _publish_and_activate(install_dal, app_id, _scheduled(app_id), community)
    table = install_dal.metadata.tables["app_source_bindings"]
    async with install_dal.engine.begin() as conn:
        await conn.execute(delete(table).where(table.c.app_id == app_id))

    with caplog.at_level(logging.ERROR):
        status, body = await _get(_app(install_dal, issuer), issuer)

    assert (status, body) == (200, {"targets": []})
    assert any(r.getMessage() == "bundle schedule candidate excluded" for r in caplog.records)
    assert [r.reason for r in caplog.records if hasattr(r, "reason")] == ["binding_missing"]


async def test_stored_manifest_without_own_source_rule_is_excluded(
    install_dal: Any, issuer: ServiceJwtIssuer
) -> None:
    """A pre-enforcement manifest whose hub rule names someone else's stream is never scheduled."""
    community = await install_dal.communities.async_insert(tenant_id=1, name="badrule-community")
    app_id = "waddles.socials.badrule.default"
    manifest = _scheduled(app_id)
    await _publish_and_activate(install_dal, app_id, manifest, community)
    uploads = install_dal.metadata.tables["app_version_uploads"]
    manifest["stages"]["process"]["consumes"][-1]["source_id"] = "other-bundle"
    async with install_dal.engine.begin() as conn:
        await conn.execute(
            uploads.update().where(uploads.c.app_id == app_id).values(manifest_json=manifest)
        )

    status, body = await _get(_app(install_dal, issuer), issuer)

    assert (status, body) == (200, {"targets": []})


async def test_activation_of_unscheduled_hub_rule_is_not_bound(install_dal: Any) -> None:
    """A bundle WITHOUT `schedule` gets no hub binding (nothing ticks it)."""
    community = await install_dal.communities.async_insert(tenant_id=1, name="unsched-community")
    app_id = "waddles.socials.unsched.default"
    await _publish_and_activate(install_dal, app_id, _manifest(app_id), community)
    table = install_dal.metadata.tables["app_source_bindings"]
    async with install_dal.engine.connect() as conn:
        rows = (await conn.execute(select(table).where(table.c.app_id == app_id))).all()
    assert [r for r in rows if r.platform == "hub"] == []


# ------------------------------------------------------------------------ real Postgres path


async def _pg_tenant(pg_dal: Any, n: int) -> int:
    async with pg_dal.engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text("INSERT INTO tenants (slug) VALUES (:s) RETURNING id"),
                    {"s": f"sched-{n}"},
                )
            ).scalar_one()
        )


async def _pg_community(pg_dal: Any, tenant_id: int, name: str) -> int:
    async with pg_dal.engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text("INSERT INTO communities (tenant_id, name) VALUES (:t, :n) RETURNING id"),
                    {"t": tenant_id, "n": name},
                )
            ).scalar_one()
        )


async def _pg_publish(pg_dal: Any, tenant_id: int, manifest: dict[str, Any]) -> None:
    async with pg_dal.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO hub_users (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        )
        await conn.execute(
            text("INSERT INTO app_catalog (app_id) VALUES (:a) ON CONFLICT (app_id) DO NOTHING"),
            {"a": manifest["app_id"]},
        )
    await _install_and_make_available(pg_dal, tenant_id=tenant_id, manifest=manifest)


async def test_pg_real_schema_listing_is_tenant_scoped(
    pg_dal: Any, issuer: ServiceJwtIssuer
) -> None:  # noqa: F811
    """Two tenants activate the same scheduled app; each filter returns only its own scope."""
    n = next(_SEQ)
    app_id = f"waddles.socials.pgsched{n}.default"
    t1, t2 = await _pg_tenant(pg_dal, n * 2), await _pg_tenant(pg_dal, n * 2 + 1)
    c1, c2 = (
        await _pg_community(pg_dal, t1, f"pg-a-{n}"),
        await _pg_community(pg_dal, t2, f"pg-b-{n}"),
    )
    manifest = _scheduled(app_id, interval_s=300, jitter=0.1)
    await _pg_publish(pg_dal, t1, manifest)  # global install happens once per (app, version)
    await set_available(pg_dal, tenant_id=t2, app_id=app_id, updated_by=1)
    for tenant, community in ((t1, c1), (t2, c2)):
        await activate_for_community_with_warnings(
            pg_dal,
            tenant_id=tenant,
            community_id=community,
            app_id=app_id,
            activated_by=None,
            valkey_client=AsyncMock(),
        )
    app = _app(pg_dal, issuer)

    status, both = await _get(app, issuer, f"?app_id={app_id}")
    assert status == 200
    assert [(t["tenant_id"], t["community_id"]) for t in both["targets"]] == [(t1, c1), (t2, c2)]
    assert all(set(t) == FIELDS for t in both["targets"])
    assert {(t["interval_s"], t["jitter"]) for t in both["targets"]} == {(300, 0.1)}
    assert [t["tenant_slug"] for t in both["targets"]] == [f"sched-{n * 2}", f"sched-{n * 2 + 1}"]

    _, only_t2 = await _get(app, issuer, f"?app_id={app_id}&tenant_id={t2}")
    assert [(t["tenant_id"], t["community_name"]) for t in only_t2["targets"]] == [
        (t2, f"pg-b-{n}")
    ]
    # cross-tenant community id never resolves another tenant's scope
    _, crossed = await _get(app, issuer, f"?app_id={app_id}&tenant_id={t1}&community_id={c2}")
    assert crossed == {"targets": []}

    bindings = pg_dal.metadata.tables["app_source_bindings"]
    async with pg_dal.engine.connect() as conn:
        bound = (
            await conn.execute(
                select(bindings.c.tenant_id, bindings.c.platform, bindings.c.source_id).where(
                    bindings.c.app_id == app_id
                )
            )
        ).all()
    assert {(r.tenant_id, r.platform, r.source_id) for r in bound} == {
        (t1, "hub", app_id_slug(app_id)),
        (t2, "hub", app_id_slug(app_id)),
    }


async def test_pg_unscheduled_and_unbound_are_not_listed(
    pg_dal: Any, issuer: ServiceJwtIssuer
) -> None:  # noqa: F811
    n = next(_SEQ) + 1000
    tenant = await _pg_tenant(pg_dal, n)
    community = await _pg_community(pg_dal, tenant, f"pg-c-{n}")
    plain, sched = f"waddles.socials.pgplain{n}.default", f"waddles.socials.pgnb{n}.default"
    for manifest in (_manifest(plain), _scheduled(sched)):
        await _pg_publish(pg_dal, tenant, manifest)
        await activate_for_community_with_warnings(
            pg_dal,
            tenant_id=tenant,
            community_id=community,
            app_id=manifest["app_id"],
            activated_by=None,
            valkey_client=AsyncMock(),
        )
    app = _app(pg_dal, issuer)
    _, listed = await _get(app, issuer, f"?tenant_id={tenant}")
    assert [t["app_id"] for t in listed["targets"]] == [sched]

    bindings = pg_dal.metadata.tables["app_source_bindings"]
    async with pg_dal.engine.begin() as conn:
        await conn.execute(delete(bindings).where(bindings.c.app_id == sched))
    _, after = await _get(app, issuer, f"?tenant_id={tenant}")
    assert after == {"targets": []}
