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

import pytest

from services.bundle_approval_service import (
    approve_version,
    classify_diff,
    deny_version,
    get_permission_summary,
)
from services.bundle_install_dal import raw_sql_rows
from services.errors import ApiError

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


async def test_deny_version_sets_rejected(install_dal: Any) -> None:
    await _seed_published(install_dal)
    await deny_version(
        install_dal,
        app_id="waddles.socials.music.default",
        version="3.0.1",
        reason="egress host not acceptable",
    )
    row = (await install_dal(install_dal.app_version_uploads.version == "3.0.1").select()).first()
    assert row.status == "REJECTED"
    assert row.reject_reason == "egress host not acceptable"


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
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.2",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=manifest,
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
