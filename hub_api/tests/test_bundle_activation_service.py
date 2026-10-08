"""Tests for the COMMUNITY tier: `activate_for_community()`/`deactivate_for_community()`.

Third of the App Bundle 3-tier split (see `services/bundle_approval_
service.py`'s own module docstring). Includes the `routes_to` cross-tenant
refusal (spec Sec5.9, D30, now checked against `bundle_tenant_
availability` instead of `app_install_approvals`) and the AUTO-BIND +
PROVISION tests (`app_source_bindings`, spec Sec5.1/Sec9.5) that used to
live in `approve_version()`'s own test file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.app_source_binding_service import TENANT_WIDE_COMMUNITY_SENTINEL
from services.bundle_approval_service import (
    activate_for_community,
    activate_tenant_wide,
    deactivate_for_community,
    deactivate_tenant_wide,
)
from services.bundle_install_dal import raw_sql_rows
from services.errors import ApiError
from services.tenant_app_availability_service import set_available
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


async def _install_and_make_available(
    install_dal: Any, *, tenant_id: int = 1, manifest: dict = _MANIFEST
) -> None:
    """GLOBAL install + TENANT availability -- the two prerequisites every activation test needs."""
    from services.bundle_approval_service import install_version_globally

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
        tenant_id=tenant_id,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=manifest,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )
    await install_version_globally(
        install_dal, app_id=manifest["app_id"], version=manifest["version"], installed_by=1
    )
    await set_available(install_dal, tenant_id=tenant_id, app_id=manifest["app_id"], updated_by=1)


async def _active_rows(install_dal: Any, *, app_id: str, tenant_id: int) -> Any:
    return await install_dal(
        (install_dal.app_active_versions.app_id == app_id)
        & (install_dal.app_active_versions.tenant_id == tenant_id)
    ).select()


async def test_activate_for_community_requires_availability(install_dal: Any) -> None:
    """409 if the app is not (yet) available in this tenant's marketplace (superset invariant)."""
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    with pytest.raises(ApiError) as exc:
        await activate_for_community(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            app_id="waddles.socials.music.default",
            activated_by=1,
        )
    assert exc.value.status_code == 409


async def test_activate_for_community_activates_it(install_dal: Any) -> None:
    await _install_and_make_available(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    row = await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=7,
    )
    active = (
        await install_dal(
            (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
            & (install_dal.app_active_versions.tenant_id == 1)
            & (install_dal.app_active_versions.community_id == community_id)
        ).select()
    ).first()
    assert active is not None
    assert active.activated_by == 7
    assert row.approved_by == 7
    assert row.community_id == community_id


async def test_activate_for_community_refuses_a_community_from_a_different_tenant(
    install_dal: Any,
) -> None:
    await _install_and_make_available(install_dal)
    other_community_id = await install_dal.communities.async_insert(
        tenant_id=999, name="other-corp-community"
    )
    with pytest.raises(ApiError) as exc:
        await activate_for_community(
            install_dal,
            tenant_id=1,
            community_id=other_community_id,
            app_id="waddles.socials.music.default",
            activated_by=1,
        )
    assert exc.value.status_code == 404


async def test_activate_for_community_refuses_an_unknown_community_id(install_dal: Any) -> None:
    await _install_and_make_available(install_dal)
    with pytest.raises(ApiError) as exc:
        await activate_for_community(
            install_dal,
            tenant_id=1,
            community_id=999999,
            app_id="waddles.socials.music.default",
            activated_by=1,
        )
    assert exc.value.status_code == 404


async def test_activate_for_community_reactivation_upserts_the_pointer(install_dal: Any) -> None:
    """Re-activating (after a newer version is installed+made available) upserts, not appends."""
    from services.bundle_approval_service import install_version_globally

    await _install_and_make_available(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=1,
    )

    newer_manifest = {**_MANIFEST, "version": "3.0.2"}
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id=newer_manifest["app_id"],
        version=newer_manifest["version"],
        artifact_digest="sha256:" + "d" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=newer_manifest["app_id"],
        version=newer_manifest["version"],
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=newer_manifest,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )
    await install_version_globally(
        install_dal,
        app_id=newer_manifest["app_id"],
        version=newer_manifest["version"],
        installed_by=1,
    )
    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=1,
    )

    active_rows = await install_dal(
        (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
        & (install_dal.app_active_versions.tenant_id == 1)
        & (install_dal.app_active_versions.community_id == community_id)
    ).select()
    assert len(active_rows) == 1
    upload_v2 = (
        await install_dal(install_dal.app_version_uploads.version == "3.0.2").select()
    ).first()
    assert active_rows.first().version_id == upload_v2.app_version_id


async def test_activate_for_community_rolls_back_if_activation_fails(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security-review regression: the approval write and activation must be one transaction."""
    from sqlalchemy import Table

    await _install_and_make_available(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")

    original_insert = Table.insert

    def _failing_insert(self: Table, *args: Any, **kwargs: Any) -> Any:
        if self.name == "app_active_versions":
            raise RuntimeError("simulated activation failure")
        return original_insert(self, *args, **kwargs)

    monkeypatch.setattr(Table, "insert", _failing_insert)

    with pytest.raises(RuntimeError, match="simulated activation failure"):
        await activate_for_community(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            app_id="waddles.socials.music.default",
            activated_by=1,
        )

    monkeypatch.undo()

    approvals = await install_dal(
        install_dal.app_install_approvals.app_id == "waddles.socials.music.default"
    ).select()
    assert not approvals, "the approval row must roll back together with the failed activation"
    active = await _active_rows(install_dal, app_id="waddles.socials.music.default", tenant_id=1)
    assert not active


async def test_deactivate_for_community_unknown_raises_404(install_dal: Any) -> None:
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    with pytest.raises(ApiError) as exc:
        await deactivate_for_community(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            app_id="waddles.socials.music.default",
            deactivated_by=1,
        )
    assert exc.value.status_code == 404


async def test_deactivate_for_community_removes_pointer(install_dal: Any) -> None:
    await _install_and_make_available(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=1,
    )
    await deactivate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        deactivated_by=1,
    )
    active = await _active_rows(install_dal, app_id="waddles.socials.music.default", tenant_id=1)
    assert not active


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
    from services.bundle_approval_service import install_version_globally

    await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.2", installed_by=1
    )
    await set_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
    )


async def _activate(install_dal: Any, *, community_id: int) -> Any:
    return await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=1,
    )


async def test_activate_for_community_refuses_a_routes_to_target_that_does_not_exist(
    install_dal: Any,
) -> None:
    await _seed_uploaded_version_with_routes_to(install_dal, routes_to=["waddles.nope.default"])
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    with pytest.raises(ApiError) as excinfo:
        await _activate(install_dal, community_id=community_id)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "routes_to_target_not_found"


async def test_activate_for_community_refuses_a_cross_tenant_routes_to_target(
    bundle_install_db: Any, install_dal: Any
) -> None:
    dal = bundle_install_db.dal
    # int(...) -- pydal's insert() returns a Reference (int subclass with a
    # __clause_element__ attribute) that chokes SQLAlchemy's bind-value
    # coercion when it crosses into an install_dal write.
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
    now = datetime.now(UTC)
    forums_version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.forums.default",
        version="1.0.0",
        artifact_digest="sha256:" + "e" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.forums.default",
        version="1.0.0",
        tenant_id=other_tenant_id,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json={
            "schema_version": 2,
            "app_id": "waddles.socials.forums.default",
            "name": "Forums",
            "version": "1.0.0",
            "feature": "waddles.socials.forums",
            "module": "socials",
            "provider": "builtin",
            "language": "python",
            "artifact": "source",
            "stages": {},
        },
        app_version_id=forums_version_id,
        created_at=now,
        updated_at=now,
    )
    from services.bundle_approval_service import install_version_globally

    await install_version_globally(
        install_dal, app_id="waddles.socials.forums.default", version="1.0.0", installed_by=1
    )
    # made available for the OTHER tenant only -- never TENANT_SLUG (id=1)
    await set_available(
        install_dal,
        tenant_id=other_tenant_id,
        app_id="waddles.socials.forums.default",
        updated_by=1,
    )

    await _seed_uploaded_version_with_routes_to(
        install_dal, routes_to=["waddles.socials.forums.default"]
    )
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    with pytest.raises(ApiError) as excinfo:
        await _activate(install_dal, community_id=community_id)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "routes_to_cross_tenant"


async def test_activate_for_community_allows_a_same_tenant_routes_to_target(
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
    now = datetime.now(UTC)
    forums_version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.forums.default",
        version="1.0.0",
        artifact_digest="sha256:" + "e" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.forums.default",
        version="1.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json={
            "schema_version": 2,
            "app_id": "waddles.socials.forums.default",
            "name": "Forums",
            "version": "1.0.0",
            "feature": "waddles.socials.forums",
            "module": "socials",
            "provider": "builtin",
            "language": "python",
            "artifact": "source",
            "stages": {},
        },
        app_version_id=forums_version_id,
        created_at=now,
        updated_at=now,
    )
    from services.bundle_approval_service import install_version_globally

    await install_version_globally(
        install_dal, app_id="waddles.socials.forums.default", version="1.0.0", installed_by=1
    )
    await set_available(
        install_dal, tenant_id=1, app_id="waddles.socials.forums.default", updated_by=1
    )

    await _seed_uploaded_version_with_routes_to(
        install_dal, routes_to=["waddles.socials.forums.default"]
    )
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    result = await _activate(install_dal, community_id=community_id)
    assert result.app_id == "waddles.socials.music.default"


async def test_activate_for_community_audits_a_routes_to_refusal(install_dal: Any) -> None:
    await _seed_uploaded_version_with_routes_to(install_dal, routes_to=["waddles.nope.default"])
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    with pytest.raises(ApiError):
        await _activate(install_dal, community_id=community_id)
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


async def test_activate_for_community_with_no_routes_to_skips_the_check_entirely(
    install_dal: Any,
) -> None:
    await _install_and_make_available(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    row = await _activate(install_dal, community_id=community_id)
    assert row.app_id == "waddles.socials.music.default"


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


async def test_activate_for_community_auto_binds_matching_sources_and_provisions_groups(
    install_dal: Any,
) -> None:
    await _install_and_make_available(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await _seed_ingest_source(install_dal, community_id=community_id, source_id="tw-a")
    await _seed_ingest_source(install_dal, community_id=community_id, source_id="tw-b")
    fake_client = AsyncMock()

    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=1,
        valkey_client=fake_client,
    )

    rows = await _bindings(install_dal, app_id="waddles.socials.music.default")
    assert {r.source_id for r in rows} == {"tw-a", "tw-b"}
    assert all(r.platform == "twitch" and r.community_id == community_id for r in rows)

    assert fake_client.xgroup_create.await_count == 2
    called = {call.args[0]: call.args[1] for call in fake_client.xgroup_create.await_args_list}
    app_id = "waddles.socials.music.default"
    assert called == {
        f"waddles:t:{TENANT_SLUG}:c:acme-community:src:twitch:tw-a:events": app_id,
        f"waddles:t:{TENANT_SLUG}:c:acme-community:src:twitch:tw-b:events": app_id,
    }
    fake_client.aclose.assert_not_called()  # caller-supplied client is never closed here


async def test_activate_for_community_zero_matching_sources_binds_nothing(
    install_dal: Any,
) -> None:
    await _install_and_make_available(install_dal)  # _MANIFEST consumes twitch; nothing configured
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    fake_client = AsyncMock()

    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=1,
        valkey_client=fake_client,
    )

    assert not await _bindings(install_dal, app_id="waddles.socials.music.default")
    fake_client.xgroup_create.assert_not_called()


async def test_activate_for_community_reactivation_replaces_bindings(install_dal: Any) -> None:
    from services.bundle_approval_service import install_version_globally

    await _install_and_make_available(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await _seed_ingest_source(install_dal, community_id=community_id, source_id="tw-a")
    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=1,
        valkey_client=AsyncMock(),
    )
    first_rows = await _bindings(install_dal, app_id="waddles.socials.music.default")
    assert {r.source_id for r in first_rows} == {"tw-a"}

    # tw-a is removed, tw-b is added, then a newer version is installed+made available+reactivated
    await install_dal(install_dal.ingest_sources.source_id == "tw-a").delete()
    await _seed_ingest_source(install_dal, community_id=community_id, source_id="tw-b")
    newer_manifest = {**_MANIFEST, "version": "3.0.2"}
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id=newer_manifest["app_id"],
        version=newer_manifest["version"],
        artifact_digest="sha256:" + "f" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=newer_manifest["app_id"],
        version=newer_manifest["version"],
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=newer_manifest,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )
    await install_version_globally(
        install_dal,
        app_id=newer_manifest["app_id"],
        version=newer_manifest["version"],
        installed_by=1,
    )

    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=1,
        valkey_client=AsyncMock(),
    )
    rows = await _bindings(install_dal, app_id="waddles.socials.music.default")
    assert {r.source_id for r in rows} == {"tw-b"}  # tw-a's stale binding is gone, not appended-to


async def test_deactivate_for_community_clears_bindings(install_dal: Any) -> None:
    await _install_and_make_available(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await _seed_ingest_source(install_dal, community_id=community_id, source_id="tw-a")
    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        activated_by=1,
        valkey_client=AsyncMock(),
    )
    assert await _bindings(install_dal, app_id="waddles.socials.music.default")

    await deactivate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id="waddles.socials.music.default",
        deactivated_by=1,
    )
    assert not await _bindings(install_dal, app_id="waddles.socials.music.default")


async def test_activate_for_community_rollback_also_rolls_back_bindings(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extends the activation-rollback regression: AUTO-BIND runs in the SAME transaction."""
    from sqlalchemy import Table

    await _install_and_make_available(install_dal)
    community_id = await install_dal.communities.async_insert(tenant_id=1, name="acme-community")
    await _seed_ingest_source(install_dal, community_id=community_id, source_id="tw-a")

    original_insert = Table.insert

    def _failing_insert(self: Table, *args: Any, **kwargs: Any) -> Any:
        if self.name == "app_active_versions":
            raise RuntimeError("simulated activation failure")
        return original_insert(self, *args, **kwargs)

    monkeypatch.setattr(Table, "insert", _failing_insert)

    with pytest.raises(RuntimeError, match="simulated activation failure"):
        await activate_for_community(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            app_id="waddles.socials.music.default",
            activated_by=1,
            valkey_client=AsyncMock(),
        )

    monkeypatch.undo()

    assert not await _bindings(install_dal, app_id="waddles.socials.music.default")


# ---------------------------------------------------------------------------
# TENANT-WIDE activation (`activate_tenant_wide()`) -- SYSTEM actor only,
# `hub_api/cli/seed_core_bundles.py`'s path for a catalog `community_id: null`
# target. Regression: seeder skipped activation for community_id null
# (alpha 2026-10-02) -- `activate_for_community()` itself now REFUSES
# `community_id=None`/0 (community_id is a required real int there, see this
# module's own docstring), so these exercise the dedicated sentinel-write
# path directly rather than that function.
# ---------------------------------------------------------------------------


async def test_activate_tenant_wide_requires_availability(install_dal: Any) -> None:
    """409 if the app is not (yet) available in this tenant's marketplace (superset invariant)."""
    with pytest.raises(ApiError) as exc:
        await activate_tenant_wide(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            activated_by=None,
        )
    assert exc.value.status_code == 409


async def test_activate_tenant_wide_writes_the_sentinel_split(install_dal: Any) -> None:
    """`app_active_versions.community_id=0` (sentinel), `app_install_approvals.community_id=NULL`.

    The two tables use DIFFERENT raw values for the one logical tenant-wide
    scope (schema convention, migrations 0022/0023 -- see `_write_tenant_
    wide_approval_and_activate()`'s own docstring) -- asserting both,
    distinctly, is the whole point of this test.
    """
    await _install_and_make_available(install_dal)
    approval_row = await activate_tenant_wide(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        activated_by=None,
        valkey_client=AsyncMock(),
    )
    assert approval_row.community_id is None
    assert approval_row.approval_source == "system:core-seeder"
    assert approval_row.approved_by is None

    active = (
        await install_dal(
            (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
            & (install_dal.app_active_versions.tenant_id == 1)
            & (install_dal.app_active_versions.community_id == TENANT_WIDE_COMMUNITY_SENTINEL)
        ).select()
    ).first()
    assert active is not None
    assert active.activated_by is None


async def test_activate_tenant_wide_auto_binds_every_source_of_the_consumed_platform(
    install_dal: Any,
) -> None:
    """Tenant-wide `ingest_sources` rows (community_id IS NULL) bind + provision one group each."""
    await _install_and_make_available(install_dal)
    await _seed_ingest_source(install_dal, community_id=None, source_id="tw-a")
    await _seed_ingest_source(install_dal, community_id=None, source_id="tw-b")
    fake_client = AsyncMock()

    await activate_tenant_wide(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        activated_by=None,
        valkey_client=fake_client,
    )

    rows = await _bindings(install_dal, app_id="waddles.socials.music.default")
    assert {r.source_id for r in rows} == {"tw-a", "tw-b"}
    assert all(
        r.platform == "twitch" and r.community_id == TENANT_WIDE_COMMUNITY_SENTINEL for r in rows
    )

    assert fake_client.xgroup_create.await_count == 2
    called = {call.args[0]: call.args[1] for call in fake_client.xgroup_create.await_args_list}
    app_id = "waddles.socials.music.default"
    assert called == {
        f"waddles:t:{TENANT_SLUG}:c:_tenant:src:twitch:tw-a:events": app_id,
        f"waddles:t:{TENANT_SLUG}:c:_tenant:src:twitch:tw-b:events": app_id,
    }
    fake_client.aclose.assert_not_called()  # caller-supplied client is never closed here


async def test_activate_tenant_wide_zero_matching_sources_binds_nothing(install_dal: Any) -> None:
    await _install_and_make_available(install_dal)  # _MANIFEST consumes twitch; nothing configured
    fake_client = AsyncMock()

    await activate_tenant_wide(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        activated_by=None,
        valkey_client=fake_client,
    )

    assert not await _bindings(install_dal, app_id="waddles.socials.music.default")
    fake_client.xgroup_create.assert_not_called()


# ---------------------------------------------------------------------------
# `deactivate_tenant_wide()` -- symmetric sibling of `activate_tenant_wide()`
# (requirement: "uninstall should delete them just like install adds them,
# otherwise our scale will get out of sync" -- the sentinel-write path needs
# the same hard-delete-the-runtime-row guarantee `deactivate_for_community()`
# already provides for a real community id).
# ---------------------------------------------------------------------------


async def test_deactivate_tenant_wide_unknown_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await deactivate_tenant_wide(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            deactivated_by=None,
        )
    assert exc.value.status_code == 404


async def test_deactivate_tenant_wide_removes_pointer(install_dal: Any) -> None:
    """The sentinel `app_active_versions` row (community_id=0) is HARD-deleted, not disabled."""
    await _install_and_make_available(install_dal)
    await activate_tenant_wide(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        activated_by=None,
        valkey_client=AsyncMock(),
    )
    active_before = (
        await install_dal(
            (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
            & (install_dal.app_active_versions.tenant_id == 1)
            & (install_dal.app_active_versions.community_id == TENANT_WIDE_COMMUNITY_SENTINEL)
        ).select()
    ).first()
    assert active_before is not None

    await deactivate_tenant_wide(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        deactivated_by=None,
    )

    active_after = await install_dal(
        (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
        & (install_dal.app_active_versions.tenant_id == 1)
        & (install_dal.app_active_versions.community_id == TENANT_WIDE_COMMUNITY_SENTINEL)
    ).select()
    assert not active_after  # regression: the row must be GONE, not merely disabled


async def test_deactivate_tenant_wide_clears_bindings(install_dal: Any) -> None:
    await _install_and_make_available(install_dal)
    await _seed_ingest_source(install_dal, community_id=None, source_id="tw-a")
    await activate_tenant_wide(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        activated_by=None,
        valkey_client=AsyncMock(),
    )
    assert await _bindings(install_dal, app_id="waddles.socials.music.default")

    await deactivate_tenant_wide(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        deactivated_by=None,
    )
    assert not await _bindings(install_dal, app_id="waddles.socials.music.default")
