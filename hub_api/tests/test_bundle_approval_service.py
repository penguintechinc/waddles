"""Tests for get_permission_summary()/classify_diff()/approve_version()/deny_version().

Includes the `routes_to` cross-tenant refusal wired into `approve_version()`
(spec Sec5.9, D30) -- a version declaring `routes_to` an app in a
different tenant is refused at approval time; the stage independently
drops it at runtime (out of hub-api's scope).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.bundle_approval_service import (
    KV_PERMISSION_ID,
    _derive_capabilities,
    approve_version,
    classify_diff,
    deny_version,
    get_permission_summary,
)
from services.bundle_install_dal import raw_sql_rows
from services.bundle_manifest_v2 import parse_bundle_manifest_v2
from services.errors import ApiError
from tests.conftest import TENANT_SLUG

_MANIFEST = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music Station",
    "version": "3.0.1",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "x:y",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        },
    },
    "egress": [{"host": "api.spotify.com"}],
    "data": {"tables": ["music_queue"]},
}


async def _seed_published(install_dal: Any, *, manifest: dict = _MANIFEST) -> None:
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id=manifest["app_id"],
        version=manifest["version"],
        artifact_digest="sha256:" + "a" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=manifest["app_id"],
        version=manifest["version"],
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=manifest,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )


async def test_get_permission_summary_is_deterministic(install_dal: Any) -> None:
    await _seed_published(install_dal)
    summary1, hash1 = await get_permission_summary(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1"
    )
    summary2, hash2 = await get_permission_summary(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1"
    )
    assert summary1 == summary2
    assert hash1 == hash2


async def test_get_permission_summary_unknown_version_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await get_permission_summary(install_dal, app_id="waddles.x.y.default", version="1.0.0")
    assert exc.value.status_code == 404


async def test_approve_version_records_a_row(install_dal: Any) -> None:
    await _seed_published(install_dal)
    row = await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=None,
        approved_by=1,
    )
    assert row.approved_by == 1
    assert row.permission_hash.startswith("sha256:")


async def test_approve_version_not_published_is_refused(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="0.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="COMPILING",
        manifest_json=_MANIFEST,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await approve_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="0.0.1",
            tenant_id=1,
            community_id=None,
            approved_by=1,
        )
    assert exc.value.code == "version_not_published"


async def test_approve_version_headless_hash_mismatch_fails_closed(install_dal: Any) -> None:
    await _seed_published(install_dal)
    with pytest.raises(ApiError) as exc:
        await approve_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.1",
            tenant_id=1,
            community_id=None,
            approved_by=1,
            expected_permission_hash="sha256:" + "0" * 64,
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "permission_hash_mismatch"


async def test_approve_version_supersedes_the_previous_current_approval(install_dal: Any) -> None:
    await _seed_published(install_dal)
    first = await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=None,
        approved_by=1,
    )
    newer_manifest = {**_MANIFEST, "version": "3.0.2"}
    await _seed_published(install_dal, manifest=newer_manifest)
    second = await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.2",
        tenant_id=1,
        community_id=None,
        approved_by=1,
    )
    refreshed_first = (
        await install_dal(install_dal.app_install_approvals.id == first.id).select()
    ).first()
    assert refreshed_first.superseded_by == second.id


async def _active_rows(install_dal: Any, *, app_id: str, tenant_id: int) -> Any:
    return await install_dal(
        (install_dal.app_active_versions.app_id == app_id)
        & (install_dal.app_active_versions.tenant_id == tenant_id)
    ).select()


async def test_version_is_inactive_until_approved(install_dal: Any) -> None:
    """A PUBLISHED, un-approved version has no `app_active_versions` row (Justin's ruling)."""
    await _seed_published(install_dal)
    before = await _active_rows(install_dal, app_id="waddles.socials.music.default", tenant_id=1)
    assert not before

    await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=None,
        approved_by=1,
    )
    after = await _active_rows(install_dal, app_id="waddles.socials.music.default", tenant_id=1)
    assert len(after) == 1


async def test_approve_version_activates_it_tenant_wide(install_dal: Any) -> None:
    """`community_id=None` activates tenant-wide, using the `0` sentinel (migration 0022)."""
    await _seed_published(install_dal)
    row = await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=None,
        approved_by=7,
    )
    upload = (
        await install_dal(install_dal.app_version_uploads.version == "3.0.1").select()
    ).first()
    active = (
        await install_dal(
            (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
            & (install_dal.app_active_versions.tenant_id == 1)
            & (install_dal.app_active_versions.community_id == 0)
        ).select()
    ).first()
    assert active is not None
    assert active.version_id == upload.app_version_id
    assert active.activated_by == 7
    assert row.approved_by == 7


async def test_approve_version_activates_for_a_specific_community(install_dal: Any) -> None:
    await _seed_published(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=community_id,
        approved_by=1,
    )
    active = (
        await install_dal(
            (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
            & (install_dal.app_active_versions.tenant_id == 1)
            & (install_dal.app_active_versions.community_id == community_id)
        ).select()
    ).first()
    assert active is not None
    # tenant-wide sentinel row is untouched by a community-scoped approval
    tenant_wide = (
        await install_dal(
            (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
            & (install_dal.app_active_versions.tenant_id == 1)
            & (install_dal.app_active_versions.community_id == 0)
        ).select()
    ).first()
    assert tenant_wide is None


async def test_approve_version_reactivation_upserts_the_pointer(install_dal: Any) -> None:
    """Re-approving a newer version updates the same `(app_id, tenant_id, community_id)` row."""
    await _seed_published(install_dal)
    await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=None,
        approved_by=1,
    )
    newer_manifest = {**_MANIFEST, "version": "3.0.2"}
    await _seed_published(install_dal, manifest=newer_manifest)
    await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.2",
        tenant_id=1,
        community_id=None,
        approved_by=1,
    )
    active_rows = await install_dal(
        (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
        & (install_dal.app_active_versions.tenant_id == 1)
        & (install_dal.app_active_versions.community_id == 0)
    ).select()
    assert len(active_rows) == 1
    upload_v2 = (
        await install_dal(install_dal.app_version_uploads.version == "3.0.2").select()
    ).first()
    assert active_rows.first().version_id == upload_v2.app_version_id


async def test_approve_version_rolls_back_the_approval_if_activation_fails(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security-review regression: the approval write and activation must be one transaction.

    Simulates a failure in the `app_active_versions` INSERT half of
    `_write_approval_and_activate()`'s single `engine.begin()` block --
    the whole transaction must roll back, leaving no orphan
    `app_install_approvals` row (previously: the approval row committed
    in its own auto-committing session before activation ever ran).
    """
    from sqlalchemy import Table

    await _seed_published(install_dal)

    original_insert = Table.insert

    def _failing_insert(self: Table, *args: Any, **kwargs: Any) -> Any:
        if self.name == "app_active_versions":
            raise RuntimeError("simulated activation failure")
        return original_insert(self, *args, **kwargs)

    monkeypatch.setattr(Table, "insert", _failing_insert)

    with pytest.raises(RuntimeError, match="simulated activation failure"):
        await approve_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.1",
            tenant_id=1,
            community_id=None,
            approved_by=1,
        )

    monkeypatch.undo()

    approvals = await install_dal(
        install_dal.app_install_approvals.app_id == "waddles.socials.music.default"
    ).select()
    assert not approvals, "the approval row must roll back together with the failed activation"

    active = await _active_rows(install_dal, app_id="waddles.socials.music.default", tenant_id=1)
    assert not active


async def test_approve_version_missing_app_version_id_is_500(install_dal: Any) -> None:
    """A PUBLISHED row with no digest pointer is a data-integrity bug, refused loudly."""
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="4.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json={**_MANIFEST, "version": "4.0.0"},
        app_version_id=None,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await approve_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="4.0.0",
            tenant_id=1,
            community_id=None,
            approved_by=1,
        )
    assert exc.value.status_code == 500
    assert exc.value.code == "missing_app_version"


async def test_deny_version_does_not_activate(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="9.9.9",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="UPLOADED",
        manifest_json={**_MANIFEST, "version": "9.9.9"},
        created_at=now,
        updated_at=now,
    )
    await deny_version(
        install_dal, app_id="waddles.socials.music.default", version="9.9.9", reason="bad"
    )
    active = await _active_rows(install_dal, app_id="waddles.socials.music.default", tenant_id=1)
    assert not active


def test_classify_diff_widened_when_a_new_table_is_added() -> None:
    previous = {
        "database": [{"table": "music_queue"}],
        "egress": [],
        "streams": [],
        "capabilities": [],
        "routesTo": [],
    }
    new = {
        "database": [{"table": "music_queue"}, {"table": "music_history"}],
        "egress": [],
        "streams": [],
        "capabilities": [],
        "routesTo": [],
    }
    assert classify_diff(new, previous) == "widened"


def test_classify_diff_narrowed_when_a_table_is_removed() -> None:
    previous = {
        "database": [{"table": "music_queue"}, {"table": "music_history"}],
        "egress": [],
        "streams": [],
        "capabilities": [],
        "routesTo": [],
    }
    new = {
        "database": [{"table": "music_queue"}],
        "egress": [],
        "streams": [],
        "capabilities": [],
        "routesTo": [],
    }
    assert classify_diff(new, previous) == "narrowed"


def test_classify_diff_unchanged() -> None:
    summary = {
        "database": [{"table": "music_queue"}],
        "egress": [],
        "streams": [],
        "capabilities": [],
        "routesTo": [],
    }
    assert classify_diff(summary, summary) == "unchanged"


def test_classify_diff_initial_with_no_previous() -> None:
    summary = {"database": [], "egress": [], "streams": [], "capabilities": [], "routesTo": []}
    assert classify_diff(summary, None) == "initial"


async def test_deny_version_sets_rejected_from_a_valid_state(install_dal: Any) -> None:
    """Deny is legal from any non-terminal state (spec Sec9.1), exercised straight off UPLOADED."""
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="UPLOADED",
        manifest_json=_MANIFEST,
        created_at=now,
        updated_at=now,
    )
    await deny_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        reason="egress host not acceptable",
    )
    row = (await install_dal(install_dal.app_version_uploads.version == "3.0.1").select()).first()
    assert row.status == "REJECTED"
    assert row.reject_reason == "egress host not acceptable"


async def test_deny_version_on_a_published_row_is_refused(install_dal: Any) -> None:
    """PUBLISHED is terminal (spec Sec9.1) -- deny must route through the state machine.

    Regression: `deny_version()` used to write `status="REJECTED"` directly,
    letting it mutate a terminal PUBLISHED row instead of refusing the
    illegal transition.
    """
    await _seed_published(install_dal)
    with pytest.raises(ApiError) as exc:
        await deny_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.1",
            reason="late objection",
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "invalid_state_transition"
    row = (await install_dal(install_dal.app_version_uploads.version == "3.0.1").select()).first()
    assert row.status == "PUBLISHED"  # untouched -- PUBLISHED is never mutated


async def test_deny_version_unknown_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await deny_version(
            install_dal, app_id="waddles.x.y.default", version="1.0.0", reason="nope"
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# routes_to cross-tenant refusal (spec Sec5.9, D30)
# ---------------------------------------------------------------------------


async def _seed_uploaded_version_with_routes_to(install_dal: Any, *, routes_to: list[str]) -> None:
    manifest = {
        "schema_version": 2,
        "app_id": "waddles.socials.music.default",
        "name": "Music Station",
        "version": "3.0.2",
        "feature": "waddles.socials.music",
        "module": "socials",
        "provider": "builtin",
        "language": "python",
        "artifact": "source",
        "routes_to": routes_to,
        "stages": {
            "process": {
                "entry": "x:y",
                "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
            }
        },
    }
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.2",
        artifact_digest="sha256:" + "c" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.2",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=manifest,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )


async def test_approve_version_refuses_a_routes_to_target_that_does_not_exist(
    install_dal: Any,
) -> None:
    await _seed_uploaded_version_with_routes_to(install_dal, routes_to=["waddles.nope.default"])
    with pytest.raises(ApiError) as excinfo:
        await approve_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.2",
            tenant_id=1,
            community_id=None,
            approved_by=1,
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "routes_to_target_not_found"


async def test_approve_version_refuses_a_cross_tenant_routes_to_target(
    bundle_install_db: Any, install_dal: Any
) -> None:
    dal = bundle_install_db.dal
    # int(...) -- pydal's insert() returns a Reference (int subclass with
    # a __clause_element__ attribute); SQLAlchemy's bind-value coercion
    # chokes on that attribute when the value crosses into an install_dal
    # (penguin-dal/SQLAlchemy) insert, so it is normalized to a plain int
    # the moment it leaves pydal.
    other_tenant_id = int(
        dal.tenants.insert(slug="other-corp", display_name="Other Corp", is_active=True)
    )
    dal.app_catalog.insert(
        app_id="waddles.socials.forums.default",
        name="Forums",
        manifest_version="3.0.0",
        module="socials",
        feature="waddles.socials.forums",
        provider="builtin",
        execution_model="native",
        is_default=False,
        platform_compatibility={"tested_with": "3.0.0", "min_version": None, "max_version": None},
        status="active",
        stages={},
    )
    dal.commit()
    await install_dal.app_install_approvals.async_insert(
        tenant_id=other_tenant_id,
        community_id=None,
        app_id="waddles.socials.forums.default",
        version="1.0.0",
        permission_hash="sha256:" + "b" * 64,
        summary_json={},
        approved_by=1,
        approved_at=datetime.now(UTC),
    )
    await _seed_uploaded_version_with_routes_to(
        install_dal, routes_to=["waddles.socials.forums.default"]
    )
    with pytest.raises(ApiError) as excinfo:
        await approve_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.2",
            tenant_id=1,
            community_id=None,
            approved_by=1,
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "routes_to_cross_tenant"


async def test_approve_version_allows_a_same_tenant_routes_to_target(
    bundle_install_db: Any, install_dal: Any
) -> None:
    dal = bundle_install_db.dal
    dal.app_catalog.insert(
        app_id="waddles.socials.forums.default",
        name="Forums",
        manifest_version="3.0.0",
        module="socials",
        feature="waddles.socials.forums",
        provider="builtin",
        execution_model="native",
        is_default=False,
        platform_compatibility={"tested_with": "3.0.0", "min_version": None, "max_version": None},
        status="active",
        stages={},
    )
    dal.commit()
    await install_dal.app_install_approvals.async_insert(
        tenant_id=1,
        community_id=None,
        app_id="waddles.socials.forums.default",
        version="1.0.0",
        permission_hash="sha256:" + "b" * 64,
        summary_json={},
        approved_by=1,
        approved_at=datetime.now(UTC),
    )
    await _seed_uploaded_version_with_routes_to(
        install_dal, routes_to=["waddles.socials.forums.default"]
    )
    result = await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.2",
        tenant_id=1,
        community_id=None,
        approved_by=1,
    )
    assert result.app_id == "waddles.socials.music.default"


async def test_approve_version_audits_a_routes_to_refusal(install_dal: Any) -> None:
    await _seed_uploaded_version_with_routes_to(install_dal, routes_to=["waddles.nope.default"])
    with pytest.raises(ApiError):
        await approve_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.2",
            tenant_id=1,
            community_id=None,
            approved_by=1,
        )
    audit_rows = await raw_sql_rows(
        install_dal, "SELECT details FROM audit_log WHERE action = :a", {"a": "routes_to_refused"}
    )
    audit_row = audit_rows.first()
    assert audit_row is not None
    details = audit_row["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["target_app_id"] == "waddles.nope.default"
    assert details["reason"] == "routes_to_target_not_found"


async def test_approve_version_with_no_routes_to_skips_the_check_entirely(install_dal: Any) -> None:
    """A manifest with no `routes_to` behaves exactly as before D30 (no regression)."""
    await _seed_published(install_dal)
    row = await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=None,
        approved_by=1,
    )
    assert row.app_id == "waddles.socials.music.default"


# ---------------------------------------------------------------------------
# communityId tenant ownership (an admin from tenant A must not be able to
# approve into a community belonging to tenant B)
# ---------------------------------------------------------------------------


async def test_approve_version_refuses_a_community_from_a_different_tenant(
    install_dal: Any,
) -> None:
    await _seed_published(install_dal)
    other_community_id = await install_dal.communities.async_insert(
        tenant_id=999, name="other-corp-community"
    )
    with pytest.raises(ApiError) as exc:
        await approve_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.1",
            tenant_id=1,
            community_id=other_community_id,
            approved_by=1,
        )
    assert exc.value.status_code == 404


async def test_approve_version_refuses_an_unknown_community_id(install_dal: Any) -> None:
    await _seed_published(install_dal)
    with pytest.raises(ApiError) as exc:
        await approve_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.1",
            tenant_id=1,
            community_id=999999,
            approved_by=1,
        )
    assert exc.value.status_code == 404


async def test_approve_version_allows_a_community_in_the_same_tenant(install_dal: Any) -> None:
    await _seed_published(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    row = await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=community_id,
        approved_by=1,
    )
    assert row.community_id == community_id


# ---------------------------------------------------------------------------
# AUTO-BIND + PROVISION (app_source_bindings, spec Sec5.1/Sec9.5) --
# _MANIFEST's own `stages.process.consumes` is a single `platform: twitch`
# rule (see this file's own module-level fixture above).
# ---------------------------------------------------------------------------


async def _seed_ingest_source(
    install_dal: Any,
    *,
    tenant_id: int = 1,
    community_id: int | None = None,
    platform: str = "twitch",
    source_id: str = "tw-a",
    enabled: bool = True,
) -> None:
    now = datetime.now(UTC)
    await install_dal.ingest_sources.async_insert(
        tenant_id=tenant_id,
        community_id=community_id,
        platform=platform,
        source_id=source_id,
        label=source_id,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


async def _bindings(install_dal: Any, *, app_id: str) -> Any:
    return await install_dal(install_dal.app_source_bindings.app_id == app_id).select()


async def test_approve_version_auto_binds_matching_sources_and_provisions_groups(
    install_dal: Any,
) -> None:
    await _seed_published(install_dal)
    await _seed_ingest_source(install_dal, source_id="tw-a")
    await _seed_ingest_source(install_dal, source_id="tw-b")
    fake_client = AsyncMock()

    await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=None,
        approved_by=1,
        valkey_client=fake_client,
    )

    rows = await _bindings(install_dal, app_id="waddles.socials.music.default")
    assert {r.source_id for r in rows} == {"tw-a", "tw-b"}
    assert all(r.platform == "twitch" and r.community_id == 0 for r in rows)

    assert fake_client.xgroup_create.await_count == 2
    called = {call.args[0]: call.args[1] for call in fake_client.xgroup_create.await_args_list}
    app_id = "waddles.socials.music.default"
    assert called == {
        f"waddles:t:{TENANT_SLUG}:c:_tenant:src:twitch:tw-a:events": app_id,
        f"waddles:t:{TENANT_SLUG}:c:_tenant:src:twitch:tw-b:events": app_id,
    }
    fake_client.aclose.assert_not_called()  # caller-supplied client is never closed here


async def test_approve_version_zero_matching_sources_binds_nothing_and_provisions_nothing(
    install_dal: Any,
) -> None:
    await _seed_published(install_dal)  # _MANIFEST consumes twitch; no ingest_sources configured
    fake_client = AsyncMock()

    await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=None,
        approved_by=1,
        valkey_client=fake_client,
    )

    assert not await _bindings(install_dal, app_id="waddles.socials.music.default")
    fake_client.xgroup_create.assert_not_called()


async def test_approve_version_reapproval_replaces_bindings(install_dal: Any) -> None:
    await _seed_published(install_dal)
    await _seed_ingest_source(install_dal, source_id="tw-a")
    await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=None,
        approved_by=1,
        valkey_client=AsyncMock(),
    )
    first_rows = await _bindings(install_dal, app_id="waddles.socials.music.default")
    assert {r.source_id for r in first_rows} == {"tw-a"}

    # tw-a is removed, tw-b is added, then the same app/tenant/community is re-approved
    await install_dal(install_dal.ingest_sources.source_id == "tw-a").delete()
    await _seed_ingest_source(install_dal, source_id="tw-b")
    newer_manifest = {**_MANIFEST, "version": "3.0.2"}
    await _seed_published(install_dal, manifest=newer_manifest)

    await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.2",
        tenant_id=1,
        community_id=None,
        approved_by=1,
        valkey_client=AsyncMock(),
    )
    rows = await _bindings(install_dal, app_id="waddles.socials.music.default")
    assert {r.source_id for r in rows} == {"tw-b"}  # tw-a's stale binding is gone, not appended-to


async def test_approve_version_community_scoped_binds_with_the_community_segment(
    install_dal: Any,
) -> None:
    await _seed_published(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await _seed_ingest_source(install_dal, community_id=community_id, source_id="tw-community")
    fake_client = AsyncMock()

    await approve_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        community_id=community_id,
        approved_by=1,
        valkey_client=fake_client,
    )

    rows = await _bindings(install_dal, app_id="waddles.socials.music.default")
    assert rows.first().community_id == community_id
    fake_client.xgroup_create.assert_awaited_once_with(
        f"waddles:t:{TENANT_SLUG}:c:acme-community:src:twitch:tw-community:events",
        "waddles.socials.music.default",
        id="$",
        mkstream=True,
    )


async def test_approve_version_rollback_also_rolls_back_bindings(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extends the activation-rollback regression: AUTO-BIND runs in the SAME transaction.

    A matching `ingest_sources` row is seeded so `sync_bindings()` writes
    a real row inside the transaction BEFORE the simulated
    `app_active_versions` insert failure -- proving the whole
    `engine.begin()` block (approval + AUTO-BIND + activation) rolls back
    together, not just the approval/activation half.
    """
    from sqlalchemy import Table

    await _seed_published(install_dal)
    await _seed_ingest_source(install_dal, source_id="tw-a")

    original_insert = Table.insert

    def _failing_insert(self: Table, *args: Any, **kwargs: Any) -> Any:
        if self.name == "app_active_versions":
            raise RuntimeError("simulated activation failure")
        return original_insert(self, *args, **kwargs)

    monkeypatch.setattr(Table, "insert", _failing_insert)

    with pytest.raises(RuntimeError, match="simulated activation failure"):
        await approve_version(
            install_dal,
            app_id="waddles.socials.music.default",
            version="3.0.1",
            tenant_id=1,
            community_id=None,
            approved_by=1,
            valkey_client=AsyncMock(),
        )

    monkeypatch.undo()

    assert not await _bindings(install_dal, app_id="waddles.socials.music.default")


def _parse_derived_manifest(**overrides: object) -> Any:
    manifest = {**_MANIFEST, **overrides}
    return parse_bundle_manifest_v2(
        manifest,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )


# Coordinator fix on PR #425 (`docs/superpowers/specs/
# 2026-09-28-bundle-permissions-and-capability-gate.md` PR #419's
# `storage.kv` permission id): `kv` is no longer in `_derive_capabilities`'s
# unconditional "always" set -- it is only derived when the manifest's own
# `permissions:` list declares `storage.kv`, the same shape-derived pattern
# `http`/`db` already use.


def test_derive_capabilities_omits_kv_when_undeclared() -> None:
    manifest = _parse_derived_manifest(permissions=[])
    caps = _derive_capabilities(manifest)
    assert KV_PERMISSION_ID not in caps


def test_derive_capabilities_includes_kv_when_declared() -> None:
    manifest = _parse_derived_manifest(permissions=[KV_PERMISSION_ID])
    caps = _derive_capabilities(manifest)
    assert KV_PERMISSION_ID in caps


def test_derive_capabilities_always_includes_the_unconditional_set() -> None:
    manifest = _parse_derived_manifest(permissions=[])
    caps = _derive_capabilities(manifest)
    assert {"context", "flags", "log", "clock"} <= caps
    assert "http" in caps  # `_MANIFEST` declares `egress`
    assert "db" in caps  # `_MANIFEST` declares `data.tables`
