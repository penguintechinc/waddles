"""
App Bundle 3-tier pydal model tests
======================================

Covers Phase C3 of docs/plans/2026-08-31-app-bundle-sdk-design.md §5.1 --
the ``app_catalog`` / ``app_tenant_availability`` / ``app_activations``
pydal table definitions in ``flask_core.app_bundle_tables``. Migration
069 (``config/postgres/migrations/069_app_bundle_tiers.sql``) was verified
separately against a live PostgreSQL 17 container (both a fresh apply and
an idempotent re-apply, plus a ``\\d`` schema dump matching the design
doc's DDL exactly) -- see the PR description for that log. These tests
prove the *pydal* side: define_table succeeds against an already-migrated
schema and CRUD round-trips through the field types (list:string, json,
reference-to-non-id-column) that Postgres alone cannot verify.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest
from pydal import DAL, Field

# --- flask_core import shim, mirrors conftest.py's approach for other
# leaf-module tests: register a namespace package so flask_core.app_bundle_tables
# imports without pulling in flask_core/__init__.py's full service stack.
_PKG_DIR = Path(__file__).resolve().parent.parent / "flask_core"
if "flask_core" not in sys.modules:
    _stub = types.ModuleType("flask_core")
    _stub.__path__ = [str(_PKG_DIR)]
    sys.modules["flask_core"] = _stub

from flask_core.app_bundle_tables import init_app_bundle_tables


@pytest.fixture
def db():
    """
    In-memory pydal DB modelling tenants -> communities (dependency order
    required by app_bundle_tables' reference fields) plus the three C3
    tables. ``migrate=True`` here (unlike production's ``migrate=False``)
    is deliberate: it lets pydal create these tables itself against sqlite,
    which is what proves the field type declarations
    (list:string/json/reference-to-app_id) are valid pydal types -- the
    same check migrate=False skips in production because the SQL migration
    already created the table.
    """
    dal = DAL("sqlite:memory")
    dal.define_table(
        "tenants", Field("slug", unique=True), Field("is_active", "boolean", default=True)
    )
    dal.define_table("communities", Field("tenant_id", "reference tenants"), Field("name"))

    # Patch define_table calls to actually migrate against sqlite for this
    # test DB, mirroring init_app_bundle_tables' field declarations exactly
    # but without the production migrate=False (see docstring above).
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
        Field("stages", "json", default={}),
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
        Field("activated_by", "integer"),
    )
    # pydal's define_table has no multi-column UNIQUE kwarg (only per-field
    # unique=True), so the composite UNIQUE(tenant_id, app_id) /
    # UNIQUE(community_id, app_id) constraints migration 069 declares are
    # added here via raw SQL against the tables pydal just created --
    # otherwise this sqlite fixture would silently under-constrain relative
    # to the real Postgres schema (verified separately, see module docstring).
    dal.executesql(
        "CREATE UNIQUE INDEX ux_app_tenant_availability_tenant_app "
        "ON app_tenant_availability(tenant_id, app_id)"
    )
    dal.executesql(
        "CREATE UNIQUE INDEX ux_app_activations_community_app "
        "ON app_activations(community_id, app_id)"
    )
    dal.commit()
    yield dal
    dal.close()


@pytest.fixture
def db_via_module():
    """
    A second fixture that exercises the *actual* production module
    (``init_app_bundle_tables``) against a schema pre-created via raw SQL
    -- mirroring exactly what happens in production: the SQL migration
    creates the table, pydal (``migrate=False``) maps onto it. Proves
    ``init_app_bundle_tables`` itself (not a hand-copied field list) is
    wired correctly.
    """
    dal = DAL("sqlite:memory")
    dal.define_table(
        "tenants", Field("slug", unique=True), Field("is_active", "boolean", default=True)
    )
    dal.define_table("communities", Field("tenant_id", "reference tenants"), Field("name"))

    # Pre-create the three tables via raw SQL (sqlite dialect stand-in for
    # migration 069 -- Postgres-specific syntax (SERIAL, TEXT[], JSONB) was
    # verified against real PostgreSQL separately; sqlite's untyped storage
    # accepts equivalent declarations without those Postgres-only keywords).
    dal.executesql(
        """
        CREATE TABLE app_catalog (
            app_id TEXT PRIMARY KEY,
            manifest_version TEXT NOT NULL,
            module TEXT NOT NULL,
            feature TEXT NOT NULL,
            provider TEXT NOT NULL,
            execution_model TEXT NOT NULL,
            is_default BOOLEAN DEFAULT 0,
            compatible_with TEXT DEFAULT '[]',
            incompatible_with TEXT DEFAULT '[]',
            platform_compatibility TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            stages TEXT DEFAULT '{}',
            installed_at TIMESTAMP,
            author TEXT,
            license TEXT,
            license_review_required BOOLEAN DEFAULT 0,
            source_url TEXT,
            alternative_to TEXT DEFAULT '[]',
            homepage_url TEXT,
            notice TEXT,
            category TEXT
        );
        """
    )
    dal.executesql(
        """
        CREATE TABLE app_tenant_availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            app_id TEXT NOT NULL REFERENCES app_catalog(app_id),
            available BOOLEAN DEFAULT 1,
            config_defaults TEXT DEFAULT '{}',
            UNIQUE(tenant_id, app_id)
        );
        """
    )
    dal.executesql(
        """
        CREATE TABLE app_activations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id INTEGER NOT NULL REFERENCES communities(id),
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            app_id TEXT NOT NULL REFERENCES app_catalog(app_id),
            enabled BOOLEAN DEFAULT 1,
            config TEXT DEFAULT '{}',
            activated_by INTEGER,
            activated_at TIMESTAMP,
            updated_at TIMESTAMP,
            UNIQUE(community_id, app_id)
        );
        """
    )
    dal.commit()

    init_app_bundle_tables(dal)
    yield dal
    dal.close()


class TestAppCatalogCRUD:
    def test_insert_and_select_round_trip(self, db):
        db.app_catalog.insert(
            app_id="waddles.bot.giveaway.giveaway-classic",
            manifest_version="1.0.0",
            module="bot",
            feature="waddles.bot.giveaway",
            provider="builtin",
            execution_model="native",
            is_default=True,
            compatible_with=[],
            incompatible_with=["waddles.bot.giveaway.legacy"],
            platform_compatibility={"tested_with": "v3.0.x", "min_version": "3.0.0"},
        )
        db.commit()
        row = db(db.app_catalog.app_id == "waddles.bot.giveaway.giveaway-classic").select().first()
        assert row.app_id == "waddles.bot.giveaway.giveaway-classic"
        assert row.feature == "waddles.bot.giveaway"
        assert row.is_default is True
        assert row.incompatible_with == ["waddles.bot.giveaway.legacy"]
        assert row.platform_compatibility["min_version"] == "3.0.0"
        assert row.status == "active"  # default applied

    def test_stages_field_round_trips_action_entrypoint(self, db):
        """`stages` (migration 071) added to this binding by the svc-action
        bundle-runtime proof -- confirms it's actually queryable, not just
        declared. Prior to this field's addition, `ActionConfigLookup`
        (core/svc_action/services/config_lookup.py) had no way to read
        `app_catalog.stages` at all.
        """
        db.app_catalog.insert(
            app_id="waddles.bot.discord.default",
            manifest_version="1.0.0",
            module="bot",
            feature="waddles.bot.discord",
            provider="builtin",
            execution_model="native",
            platform_compatibility={},
            stages={
                "action": {
                    "entrypoint": "builtin_handlers.discord_send_action:send_message",
                    "config": {"api_base": "https://discord.com/api/v10"},
                }
            },
        )
        db.commit()
        row = db(db.app_catalog.app_id == "waddles.bot.discord.default").select().first()
        assert row.stages["action"]["entrypoint"] == "builtin_handlers.discord_send_action:send_message"

    def test_stages_defaults_to_empty_dict(self, db):
        db.app_catalog.insert(
            app_id="waddles.core.demo.no-stages",
            manifest_version="1.0.0",
            module="core",
            feature="waddles.core.demo",
            provider="builtin",
            execution_model="native",
            platform_compatibility={},
        )
        db.commit()
        row = db(db.app_catalog.app_id == "waddles.core.demo.no-stages").select().first()
        assert row.stages == {}

    def test_app_id_is_the_primary_key(self, db):
        """primarykey=['app_id'] means no separate surrogate id column --
        re-inserting the same app_id must violate the PK, not silently
        create a second row."""
        db.app_catalog.insert(
            app_id="waddles.bot.giveaway.giveaway-classic",
            manifest_version="1.0.0",
            module="bot",
            feature="waddles.bot.giveaway",
            provider="builtin",
            execution_model="native",
            platform_compatibility={},
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.app_catalog.insert(
                app_id="waddles.bot.giveaway.giveaway-classic",
                manifest_version="2.0.0",
                module="bot",
                feature="waddles.bot.giveaway",
                provider="builtin",
                execution_model="native",
                platform_compatibility={},
            )


class TestAppTenantAvailabilityCRUD:
    def test_insert_and_unique_constraint(self, db):
        tenant = db.tenants.insert(slug="acme")
        db.app_catalog.insert(
            app_id="waddles.bot.giveaway.giveaway-classic",
            manifest_version="1.0.0",
            module="bot",
            feature="waddles.bot.giveaway",
            provider="builtin",
            execution_model="native",
            platform_compatibility={},
        )
        db.commit()
        db.app_tenant_availability.insert(
            tenant_id=tenant,
            app_id="waddles.bot.giveaway.giveaway-classic",
            available=True,
            config_defaults={"max_entries": 100},
        )
        db.commit()
        row = db(db.app_tenant_availability.tenant_id == tenant).select().first()
        assert row.available is True
        assert row.config_defaults == {"max_entries": 100}

        # UNIQUE(tenant_id, app_id) -- a second row for the same pair is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            db.app_tenant_availability.insert(
                tenant_id=tenant,
                app_id="waddles.bot.giveaway.giveaway-classic",
                available=False,
            )


class TestAppActivationsCRUD:
    def test_insert_and_unique_constraint(self, db):
        tenant = db.tenants.insert(slug="acme")
        community = db.communities.insert(tenant_id=tenant, name="c1")
        db.app_catalog.insert(
            app_id="waddles.bot.giveaway.giveaway-classic",
            manifest_version="1.0.0",
            module="bot",
            feature="waddles.bot.giveaway",
            provider="builtin",
            execution_model="native",
            platform_compatibility={},
        )
        db.commit()
        db.app_activations.insert(
            community_id=community,
            tenant_id=tenant,
            app_id="waddles.bot.giveaway.giveaway-classic",
            enabled=True,
            config={"window_seconds": 60},
        )
        db.commit()
        row = db(db.app_activations.community_id == community).select().first()
        assert row.enabled is True
        assert row.config == {"window_seconds": 60}
        assert row.tenant_id == tenant

        # UNIQUE(community_id, app_id) -- a second row for the same pair is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            db.app_activations.insert(
                community_id=community,
                tenant_id=tenant,
                app_id="waddles.bot.giveaway.giveaway-classic",
                enabled=False,
            )


class TestInitAppBundleTablesAgainstMigratedSchema:
    """Exercises the real init_app_bundle_tables() against a pre-existing
    schema (migrate=False), matching production's SQL-migration-then-pydal-
    maps-onto-it flow exactly."""

    def test_full_tier_round_trip(self, db_via_module):
        dal = db_via_module
        tenant = dal.tenants.insert(slug="acme")
        community = dal.communities.insert(tenant_id=tenant, name="c1")
        dal.app_catalog.insert(
            app_id="waddles.bot.giveaway.giveaway-classic",
            manifest_version="1.0.0",
            module="bot",
            feature="waddles.bot.giveaway",
            provider="builtin",
            execution_model="native",
            platform_compatibility={"tested_with": "v3.0.x"},
        )
        dal.app_tenant_availability.insert(
            tenant_id=tenant, app_id="waddles.bot.giveaway.giveaway-classic", available=True
        )
        dal.app_activations.insert(
            community_id=community,
            tenant_id=tenant,
            app_id="waddles.bot.giveaway.giveaway-classic",
            enabled=True,
        )
        dal.commit()

        catalog_row = dal(dal.app_catalog.app_id == "waddles.bot.giveaway.giveaway-classic").select().first()
        avail_row = dal(dal.app_tenant_availability.tenant_id == tenant).select().first()
        activation_row = dal(dal.app_activations.community_id == community).select().first()

        assert catalog_row.feature == "waddles.bot.giveaway"
        assert avail_row.available is True
        assert activation_row.enabled is True
        # activated ⊆ available ⊆ installed -- all three rows reference the
        # same app_id, proving the chain is queryable end to end.
        assert avail_row.app_id == catalog_row.app_id == activation_row.app_id
