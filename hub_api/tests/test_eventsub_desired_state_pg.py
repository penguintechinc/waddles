"""EventSub desired-state ledger + reconcile contract against a REAL migrated Postgres.

Real migration 0082, real FK/CHECK constraints, real AES-GCM sealing, the real blueprint with the
real machine-JWT verifier. A SKIP means docker is absent -- treat it as a gate FAILURE.
"""

# ruff: noqa: F811  -- imported pytest fixtures (`pg_db`/`pg_dal`) are re-bound as test params

from __future__ import annotations

import itertools
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey
from quart import Quart
from quart_schema import QuartSchema
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from blueprints.v1.eventsub_reconcile import (
    SCOPE_RECONCILE,
    SCOPE_SECRET_READ,
    eventsub_internal_bp,
)
from services import eventsub_desired_state as es
from services.platform_integrations_crypto import decrypt_value
from tests.test_seed_core_bundles_pg_integration import (  # noqa: F401  # fixtures
    pg_dal,
    pg_db,
)

BASE = "https://ingest.example.test"
SEQ = itertools.count(1)
HOST_ID = "spiffe://penguintech.io/alpha/svc-ingest"
OTHER_ID = "spiffe://penguintech.io/alpha/svc-action"


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform credential key (hex 32 bytes) for sealing -- a throwaway test value."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)


def _manifest(app_id: str, patterns: list[str], platform: str = "twitch") -> dict[str, Any]:
    return {
        "app_id": app_id,
        "version": "1.0.0",
        "stages": {"process": {"consumes": [{"platform": platform, "event_types": patterns}]}},
    }


class World:
    """Rows for one test: its own tenant/community/connection."""

    def __init__(self, dal: Any, tenant: int, community: int, conn_id: int) -> None:
        """Bind the ids a test's rows belong to."""
        self.dal, self.tenant, self.community, self.conn_id = dal, tenant, community, conn_id

    async def activate(self, patterns: list[str], *, community: int | None = None) -> None:
        n = next(SEQ)
        app_id = f"es.app.{n}"
        manifest = _manifest(app_id, patterns)
        async with self.dal.engine.begin() as c:
            await c.execute(text("INSERT INTO app_catalog (app_id) VALUES (:a)"), {"a": app_id})
            vid = (
                await c.execute(
                    text(
                        "INSERT INTO app_versions (app_id, version, artifact_digest, language, "
                        "artifact_kind) VALUES (:a, '1.0.0', :d, 'python', 'source') RETURNING id"
                    ),
                    {"a": app_id, "d": f"sha256:{n:064x}"},
                )
            ).scalar_one()
            await c.execute(
                text(
                    "INSERT INTO app_version_uploads (app_id, version, tenant_id, artifact_kind, "
                    "language, status, manifest_json, app_version_id) VALUES "
                    "(:a, '1.0.0', :t, 'source', 'python', 'PUBLISHED', CAST(:m AS jsonb), :v)"
                ),
                {"a": app_id, "t": self.tenant, "m": json.dumps(manifest), "v": vid},
            )
            await c.execute(
                text(
                    "INSERT INTO app_active_versions (app_id, tenant_id, community_id, version_id)"
                    " VALUES (:a, :t, :c, :v)"
                ),
                {
                    "a": app_id,
                    "t": self.tenant,
                    "c": self.community if community is None else community,
                    "v": vid,
                },
            )

    async def rows(self) -> list[Any]:
        async with self.dal.engine.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT * FROM twitch_eventsub_subscriptions WHERE community_id = :c "
                            "ORDER BY sub_type"
                        ),
                        {"c": self.community},
                    )
                ).all()
            )

    async def sql(self, stmt: str, **params: Any) -> None:
        async with self.dal.engine.begin() as c:
            await c.execute(text(stmt), params)


@pytest.fixture
async def world(pg_dal: Any) -> World:  # noqa: F811
    """A fresh tenant + community + approved/enabled active Twitch channel connection."""
    n = next(SEQ)
    async with pg_dal.engine.begin() as c:
        tenant = (
            await c.execute(
                text("INSERT INTO tenants (slug) VALUES (:s) RETURNING id"), {"s": f"es-{n}"}
            )
        ).scalar_one()
        community = (
            await c.execute(
                text("INSERT INTO communities (tenant_id, name) VALUES (:t, :n) RETURNING id"),
                {"t": tenant, "n": f"es-c-{n}"},
            )
        ).scalar_one()
        conn_id = (
            await c.execute(
                text(
                    "INSERT INTO platform_connections (tenant_id, platform, resource_type, "
                    "resource_id, access_token) VALUES (:t, 'twitch', 'twitch_channel', :r, 'x') "
                    "RETURNING id"
                ),
                {"t": tenant, "r": f"bc-{n}"},
            )
        ).scalar_one()
        await c.execute(
            text(
                "INSERT INTO community_connection_access (community_id, connection_id, status) "
                "VALUES (:c, :n, 'approved')"
            ),
            {"c": community, "n": conn_id},
        )
    return World(pg_dal, tenant, community, conn_id)


async def _reconcile(w: World, **kw: Any) -> tuple[es.SyncSummary, list[es.WorkItem], list[str]]:
    return await es.reconcile(w.dal, base_url=BASE, **kw)


def _mine(work: list[es.WorkItem], w: World) -> list[es.WorkItem]:
    return [x for x in work if x.community_id == w.community]


class TestPatternMatching:
    @pytest.mark.parametrize(
        ("patterns", "expected"),
        [
            (["chat.message"], set()),
            (["channel.raid"], {es.EventSubType.CHANNEL_RAID}),
            (["stream.*"], {es.EventSubType.STREAM_ONLINE, es.EventSubType.STREAM_OFFLINE}),
            (["channel.**"], {t for t in es.EventSubType if es.EVENTSUB_TYPES[t][0][0] == "c"}),
            (["**"], set(es.EventSubType)),
            (["channel"], set()),
            (["channel.subscription.gift.extra"], set()),
            (
                ["channel.follow", "stream.online"],
                {es.EventSubType.CHANNEL_FOLLOW, es.EventSubType.STREAM_ONLINE},
            ),
        ],
    )
    def test_event_patterns(self, patterns: list[str], expected: set[es.EventSubType]) -> None:
        assert es.types_for_event_patterns(patterns) == expected

    def test_seven_types_with_follow_v2(self) -> None:
        assert len(es.EVENTSUB_TYPES) == 7
        assert es.EVENTSUB_TYPES[es.EventSubType.CHANNEL_FOLLOW] == ("channel.follow", 2)

    def test_callback_url_requires_config(self) -> None:
        with pytest.raises(es.NotConfiguredError):
            es.callback_url("", "k")
        assert es.callback_url(BASE + "/", "k") == f"{BASE}/eventsub/twitch/webhook/k"


class TestDerivation:
    async def test_no_bundle_no_rows(self, world: World) -> None:
        summary, work, _ = await _reconcile(world)
        assert summary.communities >= 1
        assert await world.rows() == []
        assert _mine(work, world) == []

    async def test_bundle_creates_pending_rows_with_sealed_secret(self, world: World) -> None:
        await world.activate(["channel.raid", "stream.*"])
        summary, work, _ = await _reconcile(world)
        rows = await world.rows()
        assert {r.sub_type for r in rows} == {5, 6, 7}
        assert summary.created >= 3
        mine = _mine(work, world)
        assert len(mine) == 3
        for item in mine:
            assert item.action is es.Action.CREATE
            assert item.connection_id == world.conn_id
            assert item.callback_url.startswith(f"{BASE}/eventsub/twitch/webhook/")
            assert item.secret and len(item.secret) >= 40
        raw = {str(r.public_id): r for r in rows}
        for item in mine:
            row = raw[item.public_id]
            assert item.secret not in row.secret_enc  # sealed, not plaintext
            assert (
                decrypt_value(row.secret_enc, aad=f"eventsub:{row.public_id}".encode())
                == item.secret
            )
        # secrets are per-subscription
        assert len({i.secret for i in mine}) == 3

    async def test_tenant_wide_bundle_applies(self, world: World) -> None:
        await world.activate(["channel.follow"], community=0)
        await _reconcile(world)
        assert [r.sub_type for r in await world.rows()] == [1]

    async def test_other_tenants_bundle_does_not_apply(self, world: World, pg_dal: Any) -> None:
        other = World(pg_dal, world.tenant + 100000, world.community, world.conn_id)
        async with pg_dal.engine.begin() as c:
            await c.execute(
                text("INSERT INTO tenants (id, slug) VALUES (:i, :s)"),
                {"i": other.tenant, "s": f"es-other-{next(SEQ)}"},
            )
        await other.activate(["**"])
        await _reconcile(world)
        assert await world.rows() == []

    async def test_non_twitch_consumer_ignored(self, world: World) -> None:
        n = next(SEQ)
        app_id = f"es.discord.{n}"
        async with world.dal.engine.begin() as c:
            await c.execute(text("INSERT INTO app_catalog (app_id) VALUES (:a)"), {"a": app_id})
            vid = (
                await c.execute(
                    text(
                        "INSERT INTO app_versions (app_id, version, artifact_digest, language, "
                        "artifact_kind) VALUES (:a, '1.0.0', :d, 'python', 'source') RETURNING id"
                    ),
                    {"a": app_id, "d": f"sha256:{n + 5000:064x}"},
                )
            ).scalar_one()
            await c.execute(
                text(
                    "INSERT INTO app_version_uploads (app_id, version, tenant_id, artifact_kind, "
                    "language, status, manifest_json, app_version_id) VALUES "
                    "(:a, '1.0.0', :t, 'source', 'python', 'PUBLISHED', CAST(:m AS jsonb), :v)"
                ),
                {
                    "a": app_id,
                    "t": world.tenant,
                    "m": json.dumps(_manifest(app_id, ["**"], platform="discord")),
                    "v": vid,
                },
            )
            await c.execute(
                text(
                    "INSERT INTO app_active_versions (app_id, tenant_id, community_id, version_id)"
                    " VALUES (:a, :t, :c, :v)"
                ),
                {"a": app_id, "t": world.tenant, "c": world.community, "v": vid},
            )
        await _reconcile(world)
        assert await world.rows() == []

    async def test_missing_manifest_is_skipped_not_fatal(self, world: World) -> None:
        n = next(SEQ)
        app_id = f"es.nomanifest.{n}"
        async with world.dal.engine.begin() as c:
            await c.execute(text("INSERT INTO app_catalog (app_id) VALUES (:a)"), {"a": app_id})
            vid = (
                await c.execute(
                    text(
                        "INSERT INTO app_versions (app_id, version, artifact_digest, language, "
                        "artifact_kind) VALUES (:a, '1.0.0', :d, 'python', 'source') RETURNING id"
                    ),
                    {"a": app_id, "d": f"sha256:{n + 9000:064x}"},
                )
            ).scalar_one()
            await c.execute(
                text(
                    "INSERT INTO app_active_versions (app_id, tenant_id, community_id, version_id)"
                    " VALUES (:a, :t, :c, :v)"
                ),
                {"a": app_id, "t": world.tenant, "c": world.community, "v": vid},
            )
        await _reconcile(world)
        assert await world.rows() == []

    @pytest.mark.parametrize(
        "ddl",
        [
            "UPDATE community_connection_access SET status = 'pending' WHERE community_id = :c",
            "UPDATE community_connection_access SET enabled = FALSE WHERE community_id = :c",
            "UPDATE platform_connections SET status = 'revoked' WHERE id = "
            "(SELECT connection_id FROM community_connection_access WHERE community_id = :c)",
        ],
    )
    async def test_ineligible_connection_yields_nothing(self, world: World, ddl: str) -> None:
        await world.activate(["channel.raid"])
        await world.sql(ddl, c=world.community)
        await _reconcile(world)
        assert await world.rows() == []


class TestLifecycle:
    async def _create_all(self, w: World, patterns: list[str]) -> list[es.WorkItem]:
        await w.activate(patterns)
        _, work, _ = await _reconcile(w)
        mine = _mine(work, w)
        await es.report_results(
            w.dal,
            [
                es.Report(
                    x.public_id,
                    es.Outcome.CREATED,
                    twitch_subscription_id=f"tw-{i}-{x.public_id[:8]}",
                )
                for i, x in enumerate(mine)
            ],
        )
        return mine

    async def test_created_report_enables_and_stops_work(self, world: World) -> None:
        mine = await self._create_all(world, ["channel.raid"])
        rows = await world.rows()
        assert [r.status for r in rows] == [int(es.SubStatus.ENABLED)]
        _, work, enabled = await _reconcile(world)
        assert _mine(work, world) == []
        assert rows[0].twitch_subscription_id in enabled
        assert mine[0].public_id == str(rows[0].public_id)

    async def test_idempotent_second_sweep_changes_nothing(self, world: World) -> None:
        await self._create_all(world, ["channel.raid"])
        before = [(r.id, r.status, r.secret_enc) for r in await world.rows()]
        summary, _, _ = await _reconcile(world)
        assert (summary.created, summary.marked_delete, summary.marked_stale) == (0, 0, 0)
        assert [(r.id, r.status, r.secret_enc) for r in await world.rows()] == before

    async def test_bundle_deactivated_deletes_then_removes(self, world: World) -> None:
        await self._create_all(world, ["channel.raid"])
        await world.sql("DELETE FROM app_active_versions WHERE tenant_id = :t", t=world.tenant)
        summary, work, _ = await _reconcile(world)
        assert summary.marked_delete >= 1
        mine = _mine(work, world)
        assert [(x.action, x.secret) for x in mine] == [(es.Action.DELETE, None)]
        assert mine[0].twitch_subscription_id
        await es.report_results(world.dal, [es.Report(mine[0].public_id, es.Outcome.DELETED)])
        assert await world.rows() == []

    async def test_never_created_row_is_dropped_when_no_longer_desired(self, world: World) -> None:
        await world.activate(["channel.raid"])
        await _reconcile(world)
        await world.sql("DELETE FROM app_active_versions WHERE tenant_id = :t", t=world.tenant)
        summary, work, _ = await _reconcile(world)
        assert summary.removed >= 1
        assert await world.rows() == []
        assert _mine(work, world) == []

    async def test_redesired_before_delete_ran_becomes_stale_then_recreated(
        self, world: World
    ) -> None:
        await self._create_all(world, ["channel.raid"])
        await world.sql(
            "UPDATE app_active_versions SET community_id = 0 WHERE tenant_id = :t", t=world.tenant
        )
        await world.sql("DELETE FROM app_active_versions WHERE tenant_id = :t", t=world.tenant)
        await _reconcile(world)  # DELETE_PENDING
        await world.activate(["channel.raid"])
        summary, work, _ = await _reconcile(world)
        assert summary.marked_stale >= 1
        mine = _mine(work, world)
        assert [x.action for x in mine] == [es.Action.DELETE]
        old_secret = (await world.rows())[0].secret_enc
        await es.report_results(world.dal, [es.Report(mine[0].public_id, es.Outcome.DELETED)])
        row = (await world.rows())[0]
        assert row.status == int(es.SubStatus.PENDING)
        assert row.twitch_subscription_id is None
        assert row.secret_enc != old_secret  # rotated on recreate
        _, work, _ = await _reconcile(world)
        assert [x.action for x in _mine(work, world)] == [es.Action.CREATE]

    async def test_callback_base_change_marks_stale(self, world: World) -> None:
        await self._create_all(world, ["channel.raid"])
        summary, work, _ = await es.reconcile(world.dal, base_url="https://new.example.test")
        assert summary.marked_stale >= 1
        mine = _mine(work, world)
        assert mine[0].action is es.Action.DELETE
        assert mine[0].callback_url.startswith("https://new.example.test/")

    async def test_pending_callback_drift_updates_in_place(self, world: World) -> None:
        await world.activate(["channel.raid"])
        await _reconcile(world)
        await es.reconcile(world.dal, base_url="https://moved.example.test")
        assert (await world.rows())[0].callback_url.startswith("https://moved.example.test/")

    async def test_revoked_is_recreated_with_rotated_secret(self, world: World) -> None:
        mine = await self._create_all(world, ["channel.raid"])
        old = (await world.rows())[0].secret_enc
        await es.report_results(world.dal, [es.Report(mine[0].public_id, es.Outcome.REVOKED)])
        row = (await world.rows())[0]
        assert (row.status, row.failure_reason) == (4, int(es.FailureReason.TWITCH_REVOKED))
        _, work, _ = await _reconcile(world)
        again = _mine(work, world)
        assert [x.action for x in again] == [es.Action.CREATE]
        assert (await world.rows())[0].secret_enc != old

    async def test_retryable_failure_backs_off_then_retries(self, world: World) -> None:
        await world.activate(["channel.raid"])
        _, work, _ = await _reconcile(world)
        pid = _mine(work, world)[0].public_id
        t0 = datetime.now(UTC)
        await es.report_results(
            world.dal,
            [es.Report(pid, es.Outcome.FAILED, failure_reason=es.FailureReason.RATE_LIMITED)],
            now=t0,
        )
        row = (await world.rows())[0]
        assert (row.status, row.failure_count) == (3, 1)
        assert row.next_attempt_at is not None
        _, work, _ = await es.reconcile(world.dal, base_url=BASE, now=t0)
        assert _mine(work, world) == []  # still backing off
        _, work, _ = await es.reconcile(world.dal, base_url=BASE, now=t0 + timedelta(hours=2))
        assert [x.action for x in _mine(work, world)] == [es.Action.CREATE]

    async def test_backoff_grows_and_is_capped(self) -> None:
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        assert es._backoff(1, t0) == t0 + timedelta(seconds=30)
        assert es._backoff(3, t0) == t0 + timedelta(seconds=120)
        assert es._backoff(50, t0) == t0 + timedelta(seconds=3600)

    async def test_permanent_failure_waits_for_connection_change(self, world: World) -> None:
        await world.activate(["channel.raid"])
        _, work, _ = await _reconcile(world)
        pid = _mine(work, world)[0].public_id
        await es.report_results(
            world.dal,
            [es.Report(pid, es.Outcome.FAILED, failure_reason=es.FailureReason.MISSING_SCOPE)],
        )
        row = (await world.rows())[0]
        assert row.next_attempt_at is None
        _, work, _ = await _reconcile(world)
        assert _mine(work, world) == []
        await world.sql(
            "UPDATE platform_connections SET updated_at = NOW() + interval '1 minute' "
            "WHERE id = :i",
            i=world.conn_id,
        )
        _, work, _ = await _reconcile(world)
        assert (await world.rows())[0].status == int(es.SubStatus.PENDING)
        assert [x.action for x in _mine(work, world)] == [es.Action.CREATE]

    async def test_failed_delete_keeps_delete_pending(self, world: World) -> None:
        await self._create_all(world, ["channel.raid"])
        await world.sql("DELETE FROM app_active_versions WHERE tenant_id = :t", t=world.tenant)
        _, work, _ = await _reconcile(world)
        pid = _mine(work, world)[0].public_id
        await es.report_results(
            world.dal,
            [es.Report(pid, es.Outcome.FAILED, failure_reason=es.FailureReason.TRANSPORT_ERROR)],
        )
        assert (await world.rows())[0].status == int(es.SubStatus.DELETE_PENDING)

    async def test_connection_loss_deletes_via_cascade_free_path(self, world: World) -> None:
        await self._create_all(world, ["channel.raid"])
        await world.sql(
            "UPDATE platform_connections SET status = 'revoked' WHERE id = :i", i=world.conn_id
        )
        summary, _, _ = await _reconcile(world)
        assert summary.marked_delete >= 1

    async def test_connection_row_delete_cascades_ledger(self, world: World) -> None:
        await world.activate(["channel.raid"])
        await _reconcile(world)
        await world.sql(
            "DELETE FROM community_connection_access WHERE connection_id = :i", i=world.conn_id
        )
        await world.sql("DELETE FROM platform_connections WHERE id = :i", i=world.conn_id)
        assert await world.rows() == []


class TestReportValidation:
    async def test_unknown_subscription(self, pg_dal: Any) -> None:  # noqa: F811
        import uuid

        with pytest.raises(es.UnknownSubscriptionError):
            await es.report_results(pg_dal, [es.Report(str(uuid.uuid4()), es.Outcome.REVOKED)])

    async def test_bad_uuid(self, pg_dal: Any) -> None:  # noqa: F811
        with pytest.raises(es.InvalidReportError):
            await es.report_results(pg_dal, [es.Report("nope", es.Outcome.REVOKED)])

    async def test_created_needs_valid_twitch_id(self, world: World) -> None:
        await world.activate(["channel.raid"])
        _, work, _ = await _reconcile(world)
        pid = _mine(work, world)[0].public_id
        for bad in (None, "", "has space", "x" * 65):
            with pytest.raises(es.InvalidReportError):
                await es.report_results(
                    world.dal, [es.Report(pid, es.Outcome.CREATED, twitch_subscription_id=bad)]
                )

    async def test_failed_needs_reason_and_deleted_needs_pending_delete(self, world: World) -> None:
        await world.activate(["channel.raid"])
        _, work, _ = await _reconcile(world)
        pid = _mine(work, world)[0].public_id
        with pytest.raises(es.InvalidReportError):
            await es.report_results(world.dal, [es.Report(pid, es.Outcome.FAILED)])
        with pytest.raises(es.InvalidReportError):
            await es.report_results(world.dal, [es.Report(pid, es.Outcome.DELETED)])

    async def test_batch_is_atomic(self, world: World) -> None:
        await world.activate(["channel.raid"])
        _, work, _ = await _reconcile(world)
        pid = _mine(work, world)[0].public_id
        with pytest.raises(es.InvalidReportError):
            await es.report_results(
                world.dal,
                [
                    es.Report(pid, es.Outcome.CREATED, twitch_subscription_id="tw-ok"),
                    es.Report(pid, es.Outcome.FAILED),
                ],
            )
        assert (await world.rows())[0].status == int(es.SubStatus.PENDING)


class TestSchemaConstraints:
    async def test_check_constraints_and_uniqueness(self, world: World) -> None:
        await world.activate(["channel.raid"])
        await _reconcile(world)
        row = (await world.rows())[0]
        for stmt in (
            "UPDATE twitch_eventsub_subscriptions SET sub_type = 8 WHERE id = :i",
            "UPDATE twitch_eventsub_subscriptions SET status = 0 WHERE id = :i",
            "UPDATE twitch_eventsub_subscriptions SET failure_reason = 9 WHERE id = :i",
        ):
            with pytest.raises(IntegrityError):
                await world.sql(stmt, i=row.id)
        with pytest.raises(IntegrityError):
            await world.sql(
                "INSERT INTO twitch_eventsub_subscriptions (public_id, tenant_id, community_id, "
                "connection_id, sub_type, sub_version, callback_key, callback_url, secret_enc) "
                "VALUES (gen_random_uuid(), :t, :c, :n, 5, 1, 'k-dup-0123456789abcdef', 'u', 's')",
                t=world.tenant,
                c=world.community,
                n=world.conn_id,
            )

    async def test_pagination_cursor_and_limits(self, world: World) -> None:
        summary, _, _ = await es.reconcile(world.dal, base_url=BASE, after_community_id=2**30)
        assert summary.communities == 0
        with pytest.raises(es.NotConfiguredError):
            await es.reconcile(world.dal, base_url="")


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    """Real issuer: host (reconcile+secret), other service (neither), non-system tenant."""
    private = Ed25519PrivateKey.generate()
    key = SigningKey(kid="k1", private_key=private, public_key=private.public_key())

    def ident(sid: str, scopes: set[str], tenant: str = "system") -> ServiceIdentity:
        return ServiceIdentity(
            service_id=sid,
            k8s_namespace="waddlebot",
            k8s_service_account=sid.rsplit("/", 1)[-1],
            allowed_scopes=frozenset(scopes),
            tenant=tenant,
        )

    return ServiceJwtIssuer(
        keys={"k1": key},
        active_kid="k1",
        identities={
            HOST_ID: ident(HOST_ID, {SCOPE_RECONCILE, SCOPE_SECRET_READ}),
            OTHER_ID: ident(OTHER_ID, {"connections:resolve"}),
            "spiffe://penguintech.io/alpha/svc-tenant": ident(
                "spiffe://penguintech.io/alpha/svc-tenant", {SCOPE_RECONCILE}, "acme"
            ),
        },
        audience="waddlebot-internal",
    )


def _app(dal: Any, issuer: ServiceJwtIssuer, base: str = BASE) -> Quart:
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(eventsub_internal_bp)
    app.config["install_dal"] = dal
    app.config["HUB_API_CONFIG"] = SimpleNamespace(eventsub_callback_base_url=base)
    app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
    return app


def _h(
    issuer: ServiceJwtIssuer, sid: str = HOST_ID, scope: str = SCOPE_RECONCILE
) -> dict[str, str]:
    return {"Authorization": f"Bearer {issuer.issue(sid, scope)}"}


class TestContractHttp:
    async def test_full_round_trip(self, world: World, issuer: ServiceJwtIssuer) -> None:
        await world.activate(["channel.raid", "stream.online"])
        client = _app(world.dal, issuer).test_client()
        resp = await client.post("/api/v1/internal/eventsub/reconcile", json={}, headers=_h(issuer))
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "no-store"
        body = await resp.get_json()
        mine = [w for w in body["work"] if w["communityId"] == world.community]
        assert {w["twitchType"] for w in mine} == {"channel.raid", "stream.online"}
        assert all(w["action"] == 1 and w["secret"] and w["tenantId"] == world.tenant for w in mine)
        assert body["sweep"]["communities"] >= 1
        # no PII / raw broadcaster id leaks into the contract
        assert "bc-" not in json.dumps(mine)

        results = [
            {"subscriptionId": w["subscriptionId"], "outcome": 1, "twitchSubscriptionId": f"tw-{i}"}
            for i, w in enumerate(mine)
        ]
        resp = await client.post(
            "/api/v1/internal/eventsub/results", json={"results": results}, headers=_h(issuer)
        )
        assert resp.status_code == 200
        assert (await resp.get_json())["applied"] == 2
        resp = await client.post("/api/v1/internal/eventsub/reconcile", json={}, headers=_h(issuer))
        body = await resp.get_json()
        assert not [w for w in body["work"] if w["communityId"] == world.community]
        assert {"tw-0", "tw-1"} <= set(body["enabledTwitchSubscriptionIds"])

    async def test_secret_route_matches_work_item(
        self, world: World, issuer: ServiceJwtIssuer
    ) -> None:
        await world.activate(["channel.raid"])
        client = _app(world.dal, issuer).test_client()
        resp = await client.post("/api/v1/internal/eventsub/reconcile", json={}, headers=_h(issuer))
        item = next(
            w for w in (await resp.get_json())["work"] if w["communityId"] == world.community
        )
        key = item["callbackUrl"].rsplit("/", 1)[-1]
        hdr = _h(issuer, scope=SCOPE_SECRET_READ)
        resp = await client.get(f"/api/v1/internal/eventsub/secret/{key}", headers=hdr)
        assert resp.status_code == 200
        assert (await resp.get_json())["secret"] == item["secret"]
        assert (
            await client.get("/api/v1/internal/eventsub/secret/unknown-key-0123456789", headers=hdr)
        ).status_code == 404
        assert (
            await client.get("/api/v1/internal/eventsub/secret/bad!", headers=hdr)
        ).status_code == 404

    async def test_auth_matrix(self, world: World, issuer: ServiceJwtIssuer) -> None:
        client = _app(world.dal, issuer).test_client()
        url = "/api/v1/internal/eventsub/reconcile"
        assert (await client.post(url, json={})).status_code == 401
        other = _h(issuer, OTHER_ID, "connections:resolve")
        assert (await client.post(url, json={}, headers=other)).status_code == 401
        # reconcile scope does not unlock the secret route, nor the reverse
        r = await client.get(
            "/api/v1/internal/eventsub/secret/abcdefghijklmnopqrst", headers=_h(issuer)
        )
        assert r.status_code == 401
        r = await client.post(url, json={}, headers=_h(issuer, scope=SCOPE_SECRET_READ))
        assert r.status_code == 401
        tenant = _h(issuer, "spiffe://penguintech.io/alpha/svc-tenant")
        assert (await client.post(url, json={}, headers=tenant)).status_code == 403

    async def test_unconfigured_callback_is_503(
        self, world: World, issuer: ServiceJwtIssuer
    ) -> None:
        client = _app(world.dal, issuer, base="").test_client()
        resp = await client.post("/api/v1/internal/eventsub/reconcile", json={}, headers=_h(issuer))
        assert resp.status_code == 503

    async def test_request_validation(self, world: World, issuer: ServiceJwtIssuer) -> None:
        client = _app(world.dal, issuer).test_client()
        rec = "/api/v1/internal/eventsub/reconcile"
        res = "/api/v1/internal/eventsub/results"
        h = _h(issuer)
        assert (await client.post(rec, json={"limit": 0}, headers=h)).status_code == 400
        assert (await client.post(rec, json={"afterCommunityId": -1}, headers=h)).status_code == 400
        assert (await client.post(res, json={"results": []}, headers=h)).status_code == 400
        bad = {
            "results": [{"subscriptionId": "00000000-0000-0000-0000-000000000000", "outcome": 99}]
        }
        assert (await client.post(res, json=bad, headers=h)).status_code == 400
        missing = {
            "results": [{"subscriptionId": "00000000-0000-0000-0000-000000000000", "outcome": 4}]
        }
        assert (await client.post(res, json=missing, headers=h)).status_code == 404
        invalid = {"results": [{"subscriptionId": "zzz", "outcome": 4}]}
        assert (await client.post(res, json=invalid, headers=h)).status_code == 400

    async def test_secret_never_in_logs(
        self, world: World, issuer: ServiceJwtIssuer, caplog: pytest.LogCaptureFixture
    ) -> None:
        await world.activate(["channel.raid"])
        caplog.set_level("DEBUG")
        client = _app(world.dal, issuer).test_client()
        resp = await client.post("/api/v1/internal/eventsub/reconcile", json={}, headers=_h(issuer))
        item = next(
            w for w in (await resp.get_json())["work"] if w["communityId"] == world.community
        )
        assert item["secret"] not in caplog.text
        assert os.environ["CREDENTIAL_ENCRYPTION_KEY"] not in caplog.text
