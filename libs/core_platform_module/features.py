"""
Core/Platform Module -- Feature contracts + default App bindings
====================================================================

Declares the 15 Core/platform capability Features spanning the six
namespaces added to :data:`flask_core.app_manifest.KNOWN_MODULES` alongside
the 4 product Modules (see that module's docstring): ``analytics``,
``video_proxy``, ``auth``, ``compliance``, ``integrations``, ``tenancy``.
Tiers below are copied VERBATIM from the merged license catalog -- they are
load-bearing and must not drift from the catalog on a future edit here.
``min_tier`` is ENFORCED at runtime: ``EntitlementClient.required_tier`` takes
the stricter of the live registry contract and the static
``flask_core.tier_catalog.FEATURE_MIN_TIERS`` snapshot, and a feature is granted
only when its PostHog flag is on AND the tenant's effective tier is >= that
``min_tier`` -- a flag alone never unlocks a Professional/Enterprise feature.
The snapshot exists because hub-api installs ``flask_core`` without these
``*_module`` packages, so a tier changed here MUST also be changed in
``tier_catalog``; ``flask_core/tests/test_tier_enforcement.py::
TestCatalogMatchesContracts`` fails on any drift.

| Feature id                            | Tier         | Flag                                          |
|-----------------------------------------|--------------|------------------------------------------------|
| ``analytics.community_health``          | professional | ``waddles.analytics.community_health``          |
| ``analytics.bad_actor_detection``       | professional | ``waddles.analytics.bad_actor_detection``       |
| ``analytics.user_journey``              | professional | ``waddles.analytics.user_journey``              |
| ``analytics.retention_cohorts``         | professional | ``waddles.analytics.retention_cohorts``         |
| ``analytics.engagement_funnels``        | professional | ``waddles.analytics.engagement_funnels``        |
| ``analytics.advanced``                  | enterprise   | ``waddles.analytics.advanced``                  |
| ``video_proxy.streaming``               | free         | ``waddles.video_proxy.streaming``               |
| ``video_proxy.premium_limits``          | professional | ``waddles.video_proxy.premium_limits``          |
| ``auth.sso_google``                     | professional | ``waddles.auth.sso_google``                     |
| ``auth.sso_saml``                       | enterprise   | ``waddles.auth.sso_saml``                       |
| ``compliance.audit_logs``               | enterprise   | ``waddles.compliance.audit_logs``               |
| ``compliance.audit_export``             | enterprise   | ``waddles.compliance.audit_export``             |
| ``compliance.external_kms``             | enterprise   | ``waddles.compliance.external_kms``             |
| ``integrations.waddleai``               | enterprise   | ``waddles.integrations.waddleai``               |
| ``tenancy.multi_tenant``                | enterprise   | ``waddles.tenancy.multi_tenant``                |

Each Feature gets exactly one shipped default App (``provider="builtin"``,
``is_default=True``), mirroring :mod:`bot_module.features`'s shape. Most of
these namespaces are platform/config surfaces rather than Bot's
ingest/process/action pipeline (SSO, KMS, multi-tenancy have no code here
to gate yet -- see module docstring below and the worked gate in
``core/analytics_core_module/services/health_service.py``); this module is
the Feature-contract registration point those capabilities gate against as
they're wired up, not a build-out of SSO/KMS/multi-tenant themselves.

Call :func:`register_all` once at process startup to register these
against the process-wide singletons. Tests pass fresh
:class:`~flask_core.feature_registry.FeatureRegistry` /
:class:`~flask_core.app_registry.AppRegistry` instances for isolation
instead of calling it bare.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from flask_core.app_manifest import AppManifest, parse_manifest
from flask_core.app_registry import AppRegistry
from flask_core.app_registry import get_registry as get_app_registry
from flask_core.feature_contract import FeatureContract, parse_feature_contract
from flask_core.feature_registry import FeatureRegistry
from flask_core.feature_registry import get_registry as get_feature_registry


class ScopeWideningError(Exception):
    """
    Raised when a default App's manifest ``permissions`` are not a subset
    of its Feature contract's ``requires_scopes``.

    A default App is the shipped fallback every deployment trusts as a
    known-good baseline (design doc ``Apps -> Binding resolution``); one
    that grants itself scopes beyond what its own Feature declares would
    silently widen what "the box" is allowed to do relative to what the
    Feature's contract -- and by extension its MCP tool derivation -- says
    it can do. Caught at registration time, not at an authz boundary.
    """


# ---------------------------------------------------------------------------
# Feature contracts -- raw dicts, validated by parse_feature_contract().
# module is each id's own namespace prefix (analytics/video_proxy/auth/
# compliance/integrations/tenancy), not a single constant -- unlike Bot's
# one-Module MODULE constant, this package spans six namespaces.
# ---------------------------------------------------------------------------
_FEATURE_DEFS: Tuple[Dict[str, Any], ...] = (
    {
        "id": "analytics.community_health",
        "version": 1,
        "module": "analytics",
        "requires_scopes": frozenset({"analytics.community_health:read"}),
        "min_tier": "professional",
        "flag": "waddles.analytics.community_health",
    },
    {
        "id": "analytics.bad_actor_detection",
        "version": 1,
        "module": "analytics",
        "requires_scopes": frozenset({"analytics.bad_actor:read"}),
        "min_tier": "professional",
        "flag": "waddles.analytics.bad_actor_detection",
    },
    {
        "id": "analytics.user_journey",
        "version": 1,
        "module": "analytics",
        "requires_scopes": frozenset({"analytics.user_journey:read"}),
        "min_tier": "professional",
        "flag": "waddles.analytics.user_journey",
    },
    {
        "id": "analytics.retention_cohorts",
        "version": 1,
        "module": "analytics",
        "requires_scopes": frozenset({"analytics.retention:read"}),
        "min_tier": "professional",
        "flag": "waddles.analytics.retention_cohorts",
    },
    {
        "id": "analytics.engagement_funnels",
        "version": 1,
        "module": "analytics",
        "requires_scopes": frozenset({"analytics.engagement:read"}),
        "min_tier": "professional",
        "flag": "waddles.analytics.engagement_funnels",
    },
    {
        "id": "analytics.advanced",
        "version": 1,
        "module": "analytics",
        "requires_scopes": frozenset({"analytics.advanced:read"}),
        "min_tier": "enterprise",
        "flag": "waddles.analytics.advanced",
    },
    {
        "id": "video_proxy.streaming",
        "version": 1,
        "module": "video_proxy",
        "requires_scopes": frozenset({"video_proxy.stream:write"}),
        "min_tier": "free",
        "flag": "waddles.video_proxy.streaming",
    },
    {
        "id": "video_proxy.premium_limits",
        "version": 1,
        "module": "video_proxy",
        "requires_scopes": frozenset({"video_proxy.premium:write"}),
        "min_tier": "professional",
        "flag": "waddles.video_proxy.premium_limits",
    },
    {
        "id": "auth.sso_google",
        "version": 1,
        "module": "auth",
        "requires_scopes": frozenset({"auth.sso:admin"}),
        "min_tier": "professional",
        "flag": "waddles.auth.sso_google",
    },
    {
        "id": "auth.sso_saml",
        "version": 1,
        "module": "auth",
        "requires_scopes": frozenset({"auth.sso:admin"}),
        "min_tier": "enterprise",
        "flag": "waddles.auth.sso_saml",
    },
    {
        # Scope is `:admin`, not `:read`: `create_session_token` hands EVERY session the
        # `*:read` wildcard, and `require_scope` honours `*:<action>` wildcards, so a
        # `compliance.audit:read` requirement would be satisfied by any logged-in user.
        # `*:admin` is held only by platform super-admins, and the tenant-owner bundle
        # lists `compliance.audit:admin` explicitly (flask_core.auth.SCOPE_BUNDLES).
        "id": "compliance.audit_logs",
        "version": 1,
        "module": "compliance",
        "requires_scopes": frozenset({"compliance.audit:admin"}),
        "min_tier": "enterprise",
        "flag": "waddles.compliance.audit_logs",
    },
    {
        # Exporting the tamper-evident log is a separately licensed capability on top of
        # recording/reading it (GRC #3: "export capability also Enterprise").
        "id": "compliance.audit_export",
        "version": 1,
        "module": "compliance",
        "requires_scopes": frozenset({"compliance.audit:admin"}),
        "min_tier": "enterprise",
        "flag": "waddles.compliance.audit_export",
    },
    {
        "id": "compliance.external_kms",
        "version": 1,
        "module": "compliance",
        "requires_scopes": frozenset({"compliance.kms:admin"}),
        "min_tier": "enterprise",
        "flag": "waddles.compliance.external_kms",
    },
    {
        "id": "integrations.waddleai",
        "version": 1,
        "module": "integrations",
        "requires_scopes": frozenset({"integrations.waddleai:write"}),
        "min_tier": "enterprise",
        "flag": "waddles.integrations.waddleai",
    },
    {
        "id": "tenancy.multi_tenant",
        "version": 1,
        "module": "tenancy",
        "requires_scopes": frozenset({"tenancy.tenant:admin"}),
        "min_tier": "enterprise",
        "flag": "waddles.tenancy.multi_tenant",
    },
)

# ---------------------------------------------------------------------------
# Shipped default Apps -- raw dicts, validated by parse_manifest(). Each
# `feature` is the corresponding contract's `flag` ("waddles." + id), and
# each `permissions` set is a subset of that contract's `requires_scopes`
# (checked in register_all(), not just by construction here).
# ---------------------------------------------------------------------------
_DEFAULT_APP_DEFS: Tuple[Dict[str, Any], ...] = (
    {
        "app_id": "waddles.analytics.community_health.default",
        "name": "Community Health Scoring (default)",
        "version": "1.0.0",
        "feature": "waddles.analytics.community_health",
        "module": "analytics",
        "provider": "builtin",
        "surfaces": ("process",),
        "permissions": ("analytics.community_health:read",),
        "is_default": True,
    },
    {
        "app_id": "waddles.analytics.bad_actor_detection.default",
        "name": "Bad Actor Detection (default)",
        "version": "1.0.0",
        "feature": "waddles.analytics.bad_actor_detection",
        "module": "analytics",
        "provider": "builtin",
        "surfaces": ("process",),
        "permissions": ("analytics.bad_actor:read",),
        "is_default": True,
    },
    {
        "app_id": "waddles.analytics.user_journey.default",
        "name": "User Journey Analytics (default)",
        "version": "1.0.0",
        "feature": "waddles.analytics.user_journey",
        "module": "analytics",
        "provider": "builtin",
        "surfaces": ("process",),
        "permissions": ("analytics.user_journey:read",),
        "is_default": True,
    },
    {
        "app_id": "waddles.analytics.retention_cohorts.default",
        "name": "Retention Cohorts (default)",
        "version": "1.0.0",
        "feature": "waddles.analytics.retention_cohorts",
        "module": "analytics",
        "provider": "builtin",
        "surfaces": ("process",),
        "permissions": ("analytics.retention:read",),
        "is_default": True,
    },
    {
        "app_id": "waddles.analytics.engagement_funnels.default",
        "name": "Engagement Funnels (default)",
        "version": "1.0.0",
        "feature": "waddles.analytics.engagement_funnels",
        "module": "analytics",
        "provider": "builtin",
        "surfaces": ("process",),
        "permissions": ("analytics.engagement:read",),
        "is_default": True,
    },
    {
        "app_id": "waddles.analytics.advanced.default",
        "name": "Advanced Analytics (default)",
        "version": "1.0.0",
        "feature": "waddles.analytics.advanced",
        "module": "analytics",
        "provider": "builtin",
        "surfaces": ("process",),
        "permissions": ("analytics.advanced:read",),
        "is_default": True,
    },
    {
        "app_id": "waddles.video_proxy.streaming.default",
        "name": "Video Streaming Proxy (default)",
        "version": "1.0.0",
        "feature": "waddles.video_proxy.streaming",
        "module": "video_proxy",
        "provider": "builtin",
        "surfaces": ("ingest", "action"),
        "permissions": ("video_proxy.stream:write",),
        "is_default": True,
    },
    {
        "app_id": "waddles.video_proxy.premium_limits.default",
        "name": "Premium Streaming Limits (default)",
        "version": "1.0.0",
        "feature": "waddles.video_proxy.premium_limits",
        "module": "video_proxy",
        "provider": "builtin",
        "surfaces": ("action",),
        "permissions": ("video_proxy.premium:write",),
        "is_default": True,
    },
    {
        "app_id": "waddles.auth.sso_google.default",
        "name": "Google SSO (default)",
        "version": "1.0.0",
        "feature": "waddles.auth.sso_google",
        "module": "auth",
        "provider": "builtin",
        "surfaces": ("action",),
        "permissions": ("auth.sso:admin",),
        "is_default": True,
    },
    {
        "app_id": "waddles.auth.sso_saml.default",
        "name": "SAML/OIDC SSO (default)",
        "version": "1.0.0",
        "feature": "waddles.auth.sso_saml",
        "module": "auth",
        "provider": "builtin",
        "surfaces": ("action",),
        "permissions": ("auth.sso:admin",),
        "is_default": True,
    },
    {
        "app_id": "waddles.compliance.audit_logs.default",
        "name": "Audit Logs (default)",
        "version": "1.0.0",
        "feature": "waddles.compliance.audit_logs",
        "module": "compliance",
        "provider": "builtin",
        "surfaces": ("process",),
        "permissions": ("compliance.audit:admin",),
        "is_default": True,
    },
    {
        "app_id": "waddles.compliance.audit_export.default",
        "name": "Audit Log Export (default)",
        "version": "1.0.0",
        "feature": "waddles.compliance.audit_export",
        "module": "compliance",
        "provider": "builtin",
        "surfaces": ("process",),
        "permissions": ("compliance.audit:admin",),
        "is_default": True,
    },
    {
        "app_id": "waddles.compliance.external_kms.default",
        "name": "External KMS (default)",
        "version": "1.0.0",
        "feature": "waddles.compliance.external_kms",
        "module": "compliance",
        "provider": "builtin",
        "surfaces": ("process",),
        "permissions": ("compliance.kms:admin",),
        "is_default": True,
    },
    {
        "app_id": "waddles.integrations.waddleai.default",
        "name": "WaddleAI Integration (default)",
        "version": "1.0.0",
        "feature": "waddles.integrations.waddleai",
        "module": "integrations",
        "provider": "builtin",
        "surfaces": ("action",),
        "permissions": ("integrations.waddleai:write",),
        "is_default": True,
    },
    {
        "app_id": "waddles.tenancy.multi_tenant.default",
        "name": "Multi-Tenant Management (default)",
        "version": "1.0.0",
        "feature": "waddles.tenancy.multi_tenant",
        "module": "tenancy",
        "provider": "builtin",
        "surfaces": ("process",),
        "permissions": ("tenancy.tenant:admin",),
        "is_default": True,
    },
)


def build_contracts() -> Tuple[FeatureContract, ...]:
    """Parse and validate the 15 Core/platform Feature contracts, without registering them."""
    return tuple(parse_feature_contract(raw) for raw in _FEATURE_DEFS)


def build_default_apps() -> Tuple[AppManifest, ...]:
    """Parse and validate the 15 Core/platform default App manifests, without registering them."""
    return tuple(parse_manifest(raw) for raw in _DEFAULT_APP_DEFS)


def register_all(
    *,
    feature_registry: Optional[FeatureRegistry] = None,
    app_registry: Optional[AppRegistry] = None,
) -> Tuple[Tuple[FeatureContract, ...], Tuple[AppManifest, ...]]:
    """
    Parse, validate and register all 15 Core/platform Features and their
    shipped default Apps.

    Defaults to the process-wide singletons
    (:func:`flask_core.feature_registry.get_registry`,
    :func:`flask_core.app_registry.get_registry`); tests pass fresh
    instances for isolation. Every default App's ``permissions`` is checked
    against its own Feature's ``requires_scopes`` -- raising
    :class:`ScopeWideningError` -- before anything is registered, so a
    violation never leaves the registries partially populated.
    """
    f_registry = feature_registry if feature_registry is not None else get_feature_registry()
    a_registry = app_registry if app_registry is not None else get_app_registry()

    contracts = build_contracts()
    manifests = build_default_apps()

    contracts_by_id = {contract.id: contract for contract in contracts}
    for manifest in manifests:
        contract_id = manifest.feature.removeprefix("waddles.")
        contract = contracts_by_id[contract_id]
        widened = set(manifest.permissions) - contract.requires_scopes
        if widened:
            raise ScopeWideningError(
                f"default app {manifest.app_id!r} widens feature {contract.id!r}'s "
                f"scopes: {sorted(widened)}"
            )

    for contract in contracts:
        f_registry.register(contract)
    for manifest in manifests:
        a_registry.register(manifest)

    return contracts, manifests
