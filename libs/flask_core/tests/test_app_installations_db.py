"""
DB-backed InstallationLookup + write-time invariant tests
=============================================================

Covers Phase C3 of docs/plans/2026-08-31-app-bundle-sdk-design.md §5.1/§5.2 --
``flask_core.app_installations_db``'s ``DBInstallationLookup`` (feeding
``resolve_apps``) and the ``activated ⊆ available ⊆ installed`` write-time
invariant checks.

Every behavioral claim here was verified fail-first: the invariant checks
were temporarily neutered (each ``check_*_insert_allowed`` patched to
return without raising) and the corresponding
``TestInvariant*::test_*_blocks_*`` test observed failing before the real
implementation was restored -- see the PR description for the mutation
log. ``DBInstallationLookup``'s community-precedes-tenant-wide ordering
was similarly verified by swapping the two blocks in ``find()`` and
observing ``test_community_row_takes_precedence_over_tenant_wide_for_same_app_id``
fail.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from pydal import DAL, Field

_PKG_DIR = Path(__file__).resolve().parent.parent / "flask_core"
if "flask_core" not in sys.modules:
    _stub = types.ModuleType("flask_core")
    _stub.__path__ = [str(_PKG_DIR)]
    sys.modules["flask_core"] = _stub

from flask_core.app_binding import BindingError, resolve_apps
from flask_core.app_installations_db import (
    REASON_APP_NOT_AVAILABLE,
    REASON_APP_NOT_INSTALLED,
    AppTierError,
    DBInstallationLookup,
    check_activation_insert_allowed,
    check_availability_insert_allowed,
)
from flask_core.app_manifest import parse_manifest
from flask_core.app_registry import AppRegistry

FEATURE = "giveaway"


def _manifest(suffix: str, *, is_default: bool = False):
    return parse_manifest(
        {
            "app_id": f"penguintechinc.waddles.{suffix}",
            "name": suffix,
            "version": "1.0.0",
            "feature": FEATURE,
            "module": "bot",
            "provider": "builtin",
            "surfaces": ["ingest"],
            "permissions": [],
            "is_default": is_default,
        }
    )


@pytest.fixture
def db():
    """In-memory pydal DB with tenants/communities + the three C3 tables,
    migrate=True so sqlite creates them directly (test-local convenience,
    same rationale as test_app_bundle_tables.py's ``db`` fixture)."""
    dal = DAL("sqlite:memory")
    dal.define_table("tenants", Field("slug", unique=True))
    dal.define_table("communities", Field("tenant_id", "reference tenants"), Field("name"))
    dal.define_table(
        "app_catalog",
        Field("app_id", "string", notnull=True),
        Field("manifest_version", "string", notnull=True),
        Field("module", "string", notnull=True),
        Field("feature", "string", notnull=True),
        Field("provider", "string", notnull=True),
        Field("execution_model", "string", notnull=True),
        Field("is_default", "boolean", default=False),
        Field("compatible_with", "list:string", default=[]),
        Field("incompatible_with", "list:string", default=[]),
        Field("platform_compatibility", "json", notnull=True),
        Field("status", "string", default="active"),
        primarykey=["app_id"],
    )
    dal.define_table(
        "app_tenant_availability",
        Field("tenant_id", "reference tenants", notnull=True),
        Field("app_id", "reference app_catalog.app_id", notnull=True),
        Field("available", "boolean", default=True),
        Field("config_defaults", "json", default={}),
    )
    dal.define_table(
        "app_activations",
        Field("community_id", "reference communities", notnull=True),
        Field("tenant_id", "reference tenants", notnull=True),
        Field("app_id", "reference app_catalog.app_id", notnull=True),
        Field("enabled", "boolean", default=True),
        Field("config", "json", default={}),
    )
    yield dal
    dal.close()


def _seed_catalog(dal: DAL, app_id: str, feature: str = FEATURE) -> None:
    dal.app_catalog.insert(
        app_id=app_id,
        manifest_version="1.0.0",
        module="bot",
        feature=feature,
        provider="builtin",
        execution_model="native",
        platform_compatibility={},
    )
    dal.commit()


class TestDBInstallationLookupFind:
    async def test_returns_community_row(self, db) -> None:
        classic = _manifest("giveaway-classic")
        _seed_catalog(db, classic.app_id)
        tenant = db.tenants.insert(slug="acme")
        community = db.communities.insert(tenant_id=tenant, name="c1")
        db.app_activations.insert(
            community_id=community, tenant_id=tenant, app_id=classic.app_id, enabled=True
        )
        db.commit()

        lookup = DBInstallationLookup(db)
        rows = await lookup.find(FEATURE, tenant=str(tenant), community=community)
        assert len(rows) == 1
        assert rows[0].app_id == classic.app_id
        assert rows[0].community_id == community
        assert rows[0].enabled is True

    async def test_returns_tenant_wide_row_from_availability(self, db) -> None:
        classic = _manifest("giveaway-classic")
        _seed_catalog(db, classic.app_id)
        tenant = db.tenants.insert(slug="acme")
        db.app_tenant_availability.insert(tenant_id=tenant, app_id=classic.app_id, available=True)
        db.commit()

        lookup = DBInstallationLookup(db)
        rows = await lookup.find(FEATURE, tenant=str(tenant), community=None)
        assert len(rows) == 1
        assert rows[0].app_id == classic.app_id
        assert rows[0].community_id is None
        assert rows[0].enabled is True

    async def test_union_of_community_and_tenant_wide(self, db) -> None:
        classic = _manifest("giveaway-classic")
        raffle = _manifest("giveaway-raffle")
        _seed_catalog(db, classic.app_id)
        _seed_catalog(db, raffle.app_id)
        tenant = db.tenants.insert(slug="acme")
        community = db.communities.insert(tenant_id=tenant, name="c1")
        db.app_tenant_availability.insert(tenant_id=tenant, app_id=classic.app_id, available=True)
        db.app_activations.insert(
            community_id=community, tenant_id=tenant, app_id=raffle.app_id, enabled=True
        )
        db.commit()

        lookup = DBInstallationLookup(db)
        rows = await lookup.find(FEATURE, tenant=str(tenant), community=community)
        assert {r.app_id for r in rows} == {classic.app_id, raffle.app_id}

    async def test_community_row_takes_precedence_over_tenant_wide_for_same_app_id(
        self, db
    ) -> None:
        """Same app_id available tenant-wide AND explicitly deactivated at
        community level -- community row must be first in find()'s result so
        resolve_apps' first-occurrence dedupe picks the community override."""
        classic = _manifest("giveaway-classic", is_default=True)
        _seed_catalog(db, classic.app_id)
        tenant = db.tenants.insert(slug="acme")
        community = db.communities.insert(tenant_id=tenant, name="c1")
        db.app_tenant_availability.insert(tenant_id=tenant, app_id=classic.app_id, available=True)
        db.app_activations.insert(
            community_id=community, tenant_id=tenant, app_id=classic.app_id, enabled=False
        )
        db.commit()

        lookup = DBInstallationLookup(db)
        rows = await lookup.find(FEATURE, tenant=str(tenant), community=community)
        assert rows[0].community_id == community
        assert rows[0].enabled is False

    async def test_feature_filter_excludes_other_features(self, db) -> None:
        classic = _manifest("giveaway-classic")
        _seed_catalog(db, classic.app_id)
        tenant = db.tenants.insert(slug="acme")
        community = db.communities.insert(tenant_id=tenant, name="c1")
        db.app_activations.insert(
            community_id=community, tenant_id=tenant, app_id=classic.app_id, enabled=True
        )
        db.commit()

        lookup = DBInstallationLookup(db)
        rows = await lookup.find(
            "penguintechinc.waddles.other-feature", tenant=str(tenant), community=community
        )
        assert rows == []


class TestDBInstallationLookupFeedsResolveApps:
    async def test_resolve_apps_returns_the_activated_set(self, db) -> None:
        classic = _manifest("giveaway-classic", is_default=True)
        raffle = _manifest("giveaway-raffle")
        _seed_catalog(db, classic.app_id)
        _seed_catalog(db, raffle.app_id)
        registry = AppRegistry()
        registry.register(classic)
        registry.register(raffle)

        tenant = db.tenants.insert(slug="acme")
        community = db.communities.insert(tenant_id=tenant, name="c1")
        db.app_activations.insert(
            community_id=community, tenant_id=tenant, app_id=classic.app_id, enabled=True
        )
        db.app_activations.insert(
            community_id=community, tenant_id=tenant, app_id=raffle.app_id, enabled=True
        )
        db.commit()

        lookup = DBInstallationLookup(db)
        result = await resolve_apps(
            FEATURE, tenant=str(tenant), community=community, installations=lookup, registry=registry
        )
        assert {m.app_id for m in result} == {classic.app_id, raffle.app_id}

    async def test_resolve_apps_falls_back_to_default_when_nothing_activated(self, db) -> None:
        classic = _manifest("giveaway-classic", is_default=True)
        _seed_catalog(db, classic.app_id)
        registry = AppRegistry()
        registry.register(classic)

        tenant = db.tenants.insert(slug="acme")
        community = db.communities.insert(tenant_id=tenant, name="c1")
        db.commit()

        lookup = DBInstallationLookup(db)
        result = await resolve_apps(
            FEATURE, tenant=str(tenant), community=community, installations=lookup, registry=registry
        )
        assert len(result) == 1
        assert result[0].app_id == classic.app_id
        assert result[0].is_default is True

    async def test_resolve_apps_raises_when_nothing_activated_and_no_default(self, db) -> None:
        raffle = _manifest("giveaway-raffle")  # not default
        _seed_catalog(db, raffle.app_id)
        registry = AppRegistry()
        registry.register(raffle)

        tenant = db.tenants.insert(slug="acme")
        community = db.communities.insert(tenant_id=tenant, name="c1")
        db.commit()

        lookup = DBInstallationLookup(db)
        with pytest.raises(BindingError):
            await resolve_apps(
                FEATURE,
                tenant=str(tenant),
                community=community,
                installations=lookup,
                registry=registry,
            )


class TestInvariantAvailabilityRequiresCatalog:
    async def test_blocks_availability_insert_for_unknown_app_id(self, db) -> None:
        with pytest.raises(AppTierError) as excinfo:
            await check_availability_insert_allowed(db, "penguintechinc.waddles.does-not-exist")
        assert excinfo.value.reason == REASON_APP_NOT_INSTALLED

    async def test_allows_availability_insert_for_catalog_app_id(self, db) -> None:
        classic = _manifest("giveaway-classic")
        _seed_catalog(db, classic.app_id)
        await check_availability_insert_allowed(db, classic.app_id)  # must not raise


class TestInvariantActivationRequiresAvailability:
    async def test_blocks_activation_insert_without_availability_row(self, db) -> None:
        classic = _manifest("giveaway-classic")
        _seed_catalog(db, classic.app_id)
        tenant = db.tenants.insert(slug="acme")
        db.commit()

        with pytest.raises(AppTierError) as excinfo:
            await check_activation_insert_allowed(db, tenant_id=tenant, app_id=classic.app_id)
        assert excinfo.value.reason == REASON_APP_NOT_AVAILABLE

    async def test_blocks_activation_insert_when_available_is_false(self, db) -> None:
        classic = _manifest("giveaway-classic")
        _seed_catalog(db, classic.app_id)
        tenant = db.tenants.insert(slug="acme")
        db.app_tenant_availability.insert(
            tenant_id=tenant, app_id=classic.app_id, available=False
        )
        db.commit()

        with pytest.raises(AppTierError):
            await check_activation_insert_allowed(db, tenant_id=tenant, app_id=classic.app_id)

    async def test_allows_activation_insert_when_available(self, db) -> None:
        classic = _manifest("giveaway-classic")
        _seed_catalog(db, classic.app_id)
        tenant = db.tenants.insert(slug="acme")
        db.app_tenant_availability.insert(
            tenant_id=tenant, app_id=classic.app_id, available=True
        )
        db.commit()

        await check_activation_insert_allowed(db, tenant_id=tenant, app_id=classic.app_id)
