"""
App manifest schema
====================

Per docs/plans/2026-08-26-v3-scbm-apps-design.md ``Apps``: an App implements
one or more Feature contracts. "First-party defaults are Apps too — shipping
in the box is a default *binding*, not a different kind of code." This
module defines the manifest shape every App (first-party or marketplace)
must declare, and the validator that keeps a bad manifest from ever reaching
the registry.

Naming is deliberately load-bearing: ``app_id`` is namespaced
``waddles.<module>.<feature>.<app>`` so the App's Feature and Module are
recoverable from the id alone, and ``feature`` (``waddles.<module>.<feature>``)
must be the exact prefix of ``app_id`` minus its trailing App segment — the
manifest cannot claim to implement one Feature while being named for another.

Extended per docs/plans/2026-08-31-app-bundle-sdk-design.md §3 (App Bundle
SDK): a bundle's ``bundle.yaml`` may declare a richer ``stages`` map in
place of the plain ``surfaces`` tuple, plus ``execution_model``,
``compatible_with``/``incompatible_with``, and ``platform_compatibility``.
``AppManifest.surfaces`` is kept as a derived, backward-compatible field so
existing code that only asks "does this App touch process" never has to
change; ``AppManifest.permissions`` is kept as the internal field name
(bundle.yaml's ``requires_scopes`` is accepted as an alias at parse time,
per §3.3 — the design doc leaves the rename-vs-alias choice to
implementation time, and this module picks alias, since five modules'
``features.py`` already construct/read ``.permissions`` directly).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from flask_core.bundle_attribution import (
    MARKETPLACE_CATEGORIES,
    is_https_url,
    is_known_spdx,
    license_requires_review,
    valid_alternative_to_entry,
    valid_notice,
)

# SCCEBM product modules (docs/plans/2026-08-31-v3-sccebm-program-plan.md
# §1.1/§9 P4) -- 7 modules, each independently toggleable as a Helm
# deployment grouping (values.yaml `modules.<name>.enabled`): Socials,
# Customers, Community, Event, Marketing, Bot, Streaming. This is P4's
# "migrate KNOWN_MODULES to SCCEBM" step. The remaining namespaces
# (analytics, video_proxy, auth, compliance, integrations, tenancy, core)
# are Core/platform capability namespaces: always deployed (no Helm toggle
# of their own -- Core ships with every install), but their *Features*
# still go through the same tier gate as product-module Features (see
# flask_core.entitlement / feature_flags.feature_enabled). Single source of
# truth: feature_contract.py imports this set rather than re-declaring it,
# so App and Feature validation can never drift onto two different lists.
#
# "social"/"customer" (singular) are pre-P4 aliases, NOT part of the
# SCCEBM taxonomy: `social_module`/`customer_module`'s already-registered
# Feature contracts (9 + 5, landed pre-P4) still declare `module="social"`/
# `module="customer"` -- dropping those strings here would break every one
# of those registrations at import time (parse_feature_contract's
# REASON_UNKNOWN_MODULE), violating P4's own "don't break existing
# registrations" exit-gate condition. Kept additively as a transitional
# alias pair; renaming social_module/customer_module onto socials/customers
# (and retiring these two aliases) is a follow-up, scoped separately from
# this taxonomy-alignment change to avoid an unscoped rename touching live
# gates outside flask_core/hub_api (action/interactive/quote_interaction_
# module, welcome_interaction_module also key off `waddles.social.*`).
KNOWN_MODULES = frozenset({
    # SCCEBM product modules (canonical, P4)
    "socials",
    "customers",
    "community",
    "event",
    "marketing",
    "bot",
    "streaming",
    # Legacy product-module aliases -- see docstring above.
    "social",
    "customer",
    # Core/platform namespaces (always deployed, no Helm module toggle)
    "analytics",
    "video_proxy",
    "auth",
    "compliance",
    "integrations",
    "tenancy",
    "core",
})

# Pipeline stages an App's manifest may declare it touches ("stages" in the
# design doc's YAML example; named "surfaces" per this task's schema).
# ingest/process/action are async script stages (StageSpec.entrypoint,
# executed by the svc-{ingest,process,action} stage-runners). presentation
# is a 4th, non-script surface: a per-community HTML/JS overlay (e.g. a
# giveaway wheel) rendered client-side in an OBS browser source by
# svc-presentation, described by StageSpec.html_entrypoint/assets/
# browser_source_path rather than an async entrypoint -- see
# docs/plans/2026-08-31-app-bundle-sdk-design.md §3.2a.
KNOWN_SURFACES = frozenset({"ingest", "process", "action", "presentation"})

KNOWN_PROVIDERS = frozenset({"builtin", "thirdparty"})

# App Bundle SDK spec §3.1: `execution_model` is orthogonal to `provider` --
# a `builtin` bundle may still front a `thirdparty` endpoint (e.g. wrapping
# a SaaS API), so this is a distinct enum from KNOWN_PROVIDERS even though
# the two share member names.
KNOWN_EXECUTION_MODELS = frozenset({"native", "thirdparty"})

# communication_model (StageSpec, §3.2's webhook_push/rest_pull pair) --
# both thirdparty-vendor-only values (marketplace_execution_service.py).
# A persistent-socket ingest stage (Discord gateway etc.) is deliberately
# NOT a third member here: that transport shape is modeled by the shared
# `libs/waddle_transports` library's own `Transport`/`TransportType.
# SOCKET`/`Direction.INBOUND` (`core/svc_ingest/receivers/discord_gateway.
# py`'s `DiscordGatewayReceiver`) in the CODE that implements the
# receiver, not by the bundle manifest schema -- duplicating that
# classification into `communication_model` would just be a second,
# competing vocabulary.
KNOWN_COMMUNICATION_MODELS = frozenset({"webhook_push", "rest_pull"})

_SEGMENT = r"[a-z0-9][a-z0-9_-]*"
# waddles.<module>.<feature>.<app> -- exactly four dot-separated tokens.
_APP_ID_RE = re.compile(rf"^waddles\.{_SEGMENT}\.{_SEGMENT}\.{_SEGMENT}$")
# waddles.<module>.<feature> -- exactly three dot-separated tokens.
_FEATURE_RE = re.compile(rf"^waddles\.{_SEGMENT}\.{_SEGMENT}$")
# SemVer 2.0.0 core + optional pre-release/build metadata.
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$"
)


class ManifestError(Exception):
    """
    Raised when a manifest dict fails validation. ``reason`` is a stable
    machine-checkable code (see the ``REASON_*`` constants below) so callers
    and tests can assert on *why* a manifest was rejected, not just that it
    was.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


REASON_MISSING_FIELD = "missing_field"
REASON_BAD_SEMVER = "bad_semver"
REASON_NOT_NAMESPACED = "not_namespaced"
REASON_UNKNOWN_MODULE = "unknown_module"
REASON_FEATURE_PREFIX_MISMATCH = "feature_prefix_mismatch"
REASON_INVALID_PROVIDER = "invalid_provider"
REASON_UNKNOWN_SURFACE = "unknown_surface"
# App Bundle SDK spec §3.5 -- three new validation steps, run in the same
# parse_manifest() after the original seven.
REASON_INVALID_EXECUTION_MODEL = "invalid_execution_model"
REASON_BAD_PLATFORM_COMPAT_SEMVER = "bad_platform_compat_semver"
REASON_INVALID_COMPAT_APP_ID = "invalid_compat_app_id"
# App Bundle SDK spec §3.2a -- presentation component (4th bundle surface,
# client-side HTML/JS overlay, not a script stage): a presentation stage
# entry must declare html_entrypoint and must not declare a script
# entrypoint; a script stage (ingest/process/action) must not declare
# html_entrypoint.
REASON_PRESENTATION_MISSING_HTML_ENTRYPOINT = "presentation_missing_html_entrypoint"
REASON_PRESENTATION_HAS_SCRIPT_ENTRYPOINT = "presentation_has_script_entrypoint"
REASON_SCRIPT_STAGE_HAS_HTML_ENTRYPOINT = "script_stage_has_html_entrypoint"
# communication_model has a fixed enum (see KNOWN_COMMUNICATION_MODELS
# above); a bad value is rejected with the same stable-reason-code
# convention as every other enum field.
REASON_INVALID_COMMUNICATION_MODEL = "invalid_communication_model"
# Attribution/marketplace metadata (bundle.yaml's optional `author`/
# `license`/`source_url`/`alternative_to`/`homepage_url`/`notice`/
# `category` block, always optional in this schema -- see this module's
# own comment above the block in `parse_manifest` for why the
# mandatory-for-vendors gate lives in `hub_api.services.bundle_manifest_v2`
# instead). Shared shape rules live in `flask_core.bundle_attribution`;
# this module only owns the reason codes below.
REASON_UNKNOWN_SPDX_LICENSE = "unknown_spdx_license"
REASON_INVALID_SOURCE_URL = "invalid_source_url"
REASON_INVALID_HOMEPAGE_URL = "invalid_homepage_url"
REASON_INVALID_ALTERNATIVE_TO = "invalid_alternative_to"
REASON_INVALID_NOTICE = "invalid_notice"
REASON_INVALID_CATEGORY = "invalid_category"

_REQUIRED_STR_FIELDS = ("app_id", "name", "version", "feature", "module", "provider")


@dataclass(slots=True, frozen=True)
class StageSpec:
    """
    One compiled entry of a bundle's ``stages`` map (App Bundle SDK spec
    §3.2) -- the richer, per-stage replacement for a bare ``surfaces``
    tuple entry.

    ``entrypoint`` (``module.py:function``, awaited by the stage-runner
    loader) is populated for native stages and left ``None`` for
    ``thirdparty`` ones, which are reached over the network instead of
    invoked in-process. The ``execution_model``/``communication_model``/
    ``webhook_url``/``api_base_url``/``secret_ref``/``timeout_ms`` fields
    are only meaningful on a thirdparty stage entry -- fields lifted from
    ``marketplace_modules`` per the spec's own note
    (``059_marketplace_consolidation.sql:15-22``), generalized here to
    cover both ``webhook_push`` (uses ``webhook_url``) and ``rest_pull``
    (uses ``api_base_url``) communication models with one shared
    ``secret_ref``/``timeout_ms`` pair rather than duplicating them.

    A ``presentation`` stage entry (§3.2a) is a different shape entirely --
    it has no async ``entrypoint``: ``html_entrypoint`` (the overlay HTML
    file), ``assets`` (its JS/CSS/image files), and ``browser_source_path``
    (the per-community route ``svc-presentation`` serves it at) describe a
    client-side OBS browser-source overlay instead of an invoked script.
    ``consumes`` is still meaningful (the Valkey streams the overlay's own
    JS polls/subscribes to for live data, e.g. giveaway entrant counts);
    ``produces``/``config``/``spec`` and the thirdparty-only fields above
    are not used by a presentation stage.
    """

    entrypoint: Optional[str] = None
    consumes: Tuple[str, ...] = ()
    produces: Tuple[str, ...] = ()
    config: Optional[str] = None
    spec: Optional[str] = None
    execution_model: Optional[str] = None
    communication_model: Optional[str] = None  # 'webhook_push' | 'rest_pull'
    webhook_url: Optional[str] = None
    api_base_url: Optional[str] = None
    secret_ref: Optional[str] = None
    timeout_ms: Optional[int] = None
    html_entrypoint: Optional[str] = None
    assets: Tuple[str, ...] = ()
    browser_source_path: Optional[str] = None


@dataclass(slots=True, frozen=True)
class PlatformCompat:
    """
    A bundle's declared platform compatibility (App Bundle SDK spec §3.4),
    modeled on npm's ``engines`` field. ``min_version``/``max_version``
    reuse :data:`_SEMVER_RE` -- no new version grammar. ``tested_with`` is
    free text matching the repo's own release-branch convention
    (``release/v{Major}.{Minor}.X``); it is informational only and not
    semver-validated. Enforcement policy (block vs warn against the running
    platform version) is an open decision per the spec §3.4/§9 and is out
    of scope for parsing.
    """

    tested_with: str = ""
    min_version: Optional[str] = None
    max_version: Optional[str] = None


_DEFAULT_PLATFORM_COMPAT = PlatformCompat()


@dataclass(slots=True, frozen=True)
class AppManifest:
    """
    A validated, installable App descriptor.

    Built exclusively by :func:`parse_manifest` -- constructing one directly
    bypasses the semver/namespacing/module/feature-prefix checks, so callers
    reading manifests from disk, a marketplace payload, or a DB row must
    always go through ``parse_manifest``.

    ``surfaces`` is kept as a plain, backward-compatible tuple field --
    :func:`parse_manifest` derives it from ``stage_specs``' keys when the
    input dict uses the new ``stages`` map, or takes it as given when the
    input uses the legacy bare ``surfaces`` tuple. Either way, existing code
    that only reads ``manifest.surfaces`` needs no changes.
    """

    app_id: str
    name: str
    version: str
    feature: str
    module: str
    provider: str  # 'builtin' | 'thirdparty'
    surfaces: Tuple[str, ...] = ()
    permissions: Tuple[str, ...] = ()
    config_schema: Dict[str, Any] = field(default_factory=dict)
    is_default: bool = False
    stage_specs: Mapping[str, StageSpec] = field(default_factory=dict)
    execution_model: str = "native"  # 'native' | 'thirdparty'
    compatible_with: Tuple[str, ...] = ()
    incompatible_with: Tuple[str, ...] = ()
    platform_compatibility: PlatformCompat = _DEFAULT_PLATFORM_COMPAT
    author: Optional[str] = None
    license: Optional[str] = None
    license_requires_review: bool = False
    source_url: Optional[str] = None
    alternative_to: Tuple[str, ...] = ()
    homepage_url: Optional[str] = None
    notice: Optional[str] = None
    category: Optional[str] = None


def _require_str(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(REASON_MISSING_FIELD, f"{key!r} must be a non-empty string")
    return value


def _compile_stage_spec(stage_name: str, raw: Dict[str, Any]) -> StageSpec:
    """
    Validate one ``stages`` map key/entry pair and build its :class:`StageSpec`.

    ``presentation`` (§3.2a) is not a script stage: it requires
    ``html_entrypoint`` and rejects a script ``entrypoint``; the three
    script stages (``ingest``/``process``/``action``) are the reverse --
    they reject ``html_entrypoint``, which only ever describes a
    client-side overlay.
    """
    if stage_name not in KNOWN_SURFACES:
        raise ManifestError(
            REASON_UNKNOWN_SURFACE,
            f"stage {stage_name!r} is not one of {sorted(KNOWN_SURFACES)}",
        )

    entrypoint = raw.get("entrypoint")
    html_entrypoint = raw.get("html_entrypoint")
    communication_model = raw.get("communication_model")

    if communication_model is not None and communication_model not in KNOWN_COMMUNICATION_MODELS:
        raise ManifestError(
            REASON_INVALID_COMMUNICATION_MODEL,
            f"stage {stage_name!r} communication_model {communication_model!r} is not "
            f"one of {sorted(KNOWN_COMMUNICATION_MODELS)}",
        )

    if stage_name == "presentation":
        if not html_entrypoint:
            raise ManifestError(
                REASON_PRESENTATION_MISSING_HTML_ENTRYPOINT,
                "presentation stage requires html_entrypoint",
            )
        if entrypoint:
            raise ManifestError(
                REASON_PRESENTATION_HAS_SCRIPT_ENTRYPOINT,
                "presentation stage must not declare a script entrypoint",
            )
    elif html_entrypoint:
        raise ManifestError(
            REASON_SCRIPT_STAGE_HAS_HTML_ENTRYPOINT,
            f"stage {stage_name!r} is a script stage and must not declare html_entrypoint",
        )

    return StageSpec(
        entrypoint=entrypoint,
        consumes=tuple(raw.get("consumes", ())),
        produces=tuple(raw.get("produces", ())),
        config=raw.get("config"),
        spec=raw.get("spec"),
        execution_model=raw.get("execution_model"),
        communication_model=communication_model,
        webhook_url=raw.get("webhook_url"),
        api_base_url=raw.get("api_base_url"),
        secret_ref=raw.get("secret_ref"),
        timeout_ms=raw.get("timeout_ms"),
        html_entrypoint=html_entrypoint,
        assets=tuple(raw.get("assets", ())),
        browser_source_path=raw.get("browser_source_path"),
    )


def _parse_platform_compatibility(raw: Optional[Dict[str, Any]]) -> PlatformCompat:
    """
    Build a :class:`PlatformCompat` from the manifest's optional
    ``platform_compatibility`` dict, or the shared empty default when the
    manifest doesn't declare one (legacy manifests, pre-§3.4).
    """
    if raw is None:
        return _DEFAULT_PLATFORM_COMPAT

    min_version = raw.get("min_version")
    max_version = raw.get("max_version")
    for label, candidate in (("min_version", min_version), ("max_version", max_version)):
        if candidate is not None and not _SEMVER_RE.match(candidate):
            raise ManifestError(
                REASON_BAD_PLATFORM_COMPAT_SEMVER,
                f"platform_compatibility.{label} {candidate!r} is not valid SemVer 2.0.0",
            )

    return PlatformCompat(
        tested_with=str(raw.get("tested_with", "")),
        min_version=min_version,
        max_version=max_version,
    )


def parse_manifest(data: Dict[str, Any]) -> AppManifest:
    """
    Validate a raw manifest dict (parsed YAML/JSON, or a marketplace
    payload) and build an :class:`AppManifest`.

    Rejects, in order, with a typed :class:`ManifestError`:

    1. missing/empty required fields
    2. ``version`` that is not valid SemVer 2.0.0
    3. ``app_id`` that is not namespaced ``waddles.<module>.<feature>.<app>``
    4. ``module`` not one of the known Modules
    5. ``app_id``'s feature-prefix (``app_id`` minus its trailing App
       segment) not equal to ``feature``
    6. ``provider`` outside ``{builtin, thirdparty}``
    7. any ``surfaces``/``stages`` entry outside
       ``{ingest, process, action, presentation}``
    8. ``execution_model`` outside ``{native, thirdparty}``
    9. ``platform_compatibility.min_version``/``max_version`` that are not
       valid SemVer 2.0.0
    10. any ``compatible_with``/``incompatible_with`` entry that is not a
        namespaced ``app_id``
    11. a ``presentation`` stage entry in ``stages`` missing
        ``html_entrypoint``, or declaring a script ``entrypoint``
    12. an ``ingest``/``process``/``action`` stage entry in ``stages``
        declaring ``html_entrypoint``
    13. any ``stages`` entry's ``communication_model`` outside
        ``{webhook_push, rest_pull}`` (when set)
    14. a present ``license`` that is not an allowlisted SPDX id
        (``flask_core.bundle_attribution``); a ``source_url``/``homepage_url``
        that is not ``https://``; an invalid ``alternative_to`` entry; an
        empty/oversize ``notice``; a ``category`` outside the marketplace
        enum. ``author``/``license``/etc. are always optional here -- see
        the attribution block's own comment below for why the mandatory-
        for-vendors gate lives in ``hub_api.services.bundle_manifest_v2``
        instead, not this function.

    Two things this function deliberately does **not** check (per App
    Bundle SDK spec §3.5, left for a later, registry-aware pass): whether
    ``compatible_with``/``incompatible_with`` entries name an ``app_id``
    actually present in the registry, and whether ``requires_scopes`` is a
    subset of the declared Feature's ``FeatureContract.requires_scopes``
    (spec §3.3) -- both require cross-referencing state outside this single
    manifest dict, which would couple ``parse_manifest`` to the registry/
    feature-contract module at parse time. TODO(bundle-sdk): add both once
    the registry-aware validation pass lands.
    """
    for key in _REQUIRED_STR_FIELDS:
        _require_str(data, key)

    app_id = data["app_id"]
    name = data["name"]
    version = data["version"]
    feature = data["feature"]
    module = data["module"]
    provider = data["provider"]

    if not _SEMVER_RE.match(version):
        raise ManifestError(REASON_BAD_SEMVER, f"{version!r} is not valid SemVer 2.0.0")

    if not _APP_ID_RE.match(app_id):
        raise ManifestError(
            REASON_NOT_NAMESPACED,
            f"app_id {app_id!r} must be namespaced 'waddles.<module>.<feature>.<app>'",
        )

    if not _FEATURE_RE.match(feature):
        raise ManifestError(
            REASON_NOT_NAMESPACED,
            f"feature {feature!r} must be namespaced 'waddles.<module>.<feature>'",
        )

    if module not in KNOWN_MODULES:
        raise ManifestError(
            REASON_UNKNOWN_MODULE, f"module {module!r} is not one of {sorted(KNOWN_MODULES)}"
        )

    app_id_feature_prefix = app_id.rsplit(".", 1)[0]
    if app_id_feature_prefix != feature:
        raise ManifestError(
            REASON_FEATURE_PREFIX_MISMATCH,
            f"app_id {app_id!r} implies feature {app_id_feature_prefix!r}, "
            f"but manifest declares feature {feature!r}",
        )

    if provider not in KNOWN_PROVIDERS:
        raise ManifestError(
            REASON_INVALID_PROVIDER, f"provider {provider!r} is not one of {sorted(KNOWN_PROVIDERS)}"
        )

    # App Bundle SDK spec §3.2/§3.5: `stages` (new, richer form) supersedes
    # `surfaces` (legacy, bare tuple form) -- `surfaces` is derived from
    # `stages`' keys when present, otherwise it's taken as given so every
    # existing manifest (five modules' features.py) keeps parsing unchanged.
    stages_raw = data.get("stages")
    if stages_raw:
        stage_specs: Dict[str, StageSpec] = {
            stage_name: _compile_stage_spec(stage_name, stage_data)
            for stage_name, stage_data in stages_raw.items()
        }
        surfaces = tuple(stage_specs.keys())
    else:
        stage_specs = {}
        surfaces = tuple(data.get("surfaces", ()))
        for surface in surfaces:
            if surface not in KNOWN_SURFACES:
                raise ManifestError(
                    REASON_UNKNOWN_SURFACE,
                    f"surface {surface!r} is not one of {sorted(KNOWN_SURFACES)}",
                )

    execution_model = str(data.get("execution_model", "native"))
    if execution_model not in KNOWN_EXECUTION_MODELS:
        raise ManifestError(
            REASON_INVALID_EXECUTION_MODEL,
            f"execution_model {execution_model!r} is not one of {sorted(KNOWN_EXECUTION_MODELS)}",
        )

    platform_compatibility = _parse_platform_compatibility(data.get("platform_compatibility"))

    compatible_with = tuple(data.get("compatible_with", ()))
    incompatible_with = tuple(data.get("incompatible_with", ()))
    for other_app_id in (*compatible_with, *incompatible_with):
        if not _APP_ID_RE.match(other_app_id):
            raise ManifestError(
                REASON_INVALID_COMPAT_APP_ID,
                f"compatible_with/incompatible_with entry {other_app_id!r} must be "
                "namespaced 'waddles.<module>.<feature>.<app>'",
            )

    # requires_scopes (bundle.yaml's name, spec §3.3) aliases the existing
    # `permissions` field -- either raw dict key populates it; the
    # dataclass field itself stays `permissions` since five modules'
    # features.py already construct/read `.permissions` directly.
    permissions = tuple(data.get("requires_scopes", data.get("permissions", ())))
    config_schema = dict(data.get("config_schema", {}))
    is_default = bool(data.get("is_default", False))

    # Attribution/marketplace metadata -- always optional here. `provider:
    # thirdparty` in THIS schema means "wraps a third-party endpoint"
    # (App Bundle SDK spec §3.1, `execution_model`'s own docstring above)
    # and is not by itself evidence of who submitted the App -- pre-existing
    # fixtures (`test_app_framework.py`'s "Acme Shoutout Pro") legitimately
    # declare `provider: thirdparty` with no author/license at all. The
    # actual vendor-onboarding gate (mandatory `author`/`license` for a
    # caller authenticated via `vendor:onboard`, see `services/vendor_
    # bundle_authz.py`) lives in `hub_api.services.bundle_manifest_v2`
    # instead, which both knows the caller's onboarding path and is the
    # schema real vendor uploads are actually validated against. Shared
    # SPDX/URL/shape rules live in `flask_core.bundle_attribution` so the
    # two manifest schemas can never silently drift on what counts as a
    # valid license or URL for whichever fields ARE present.
    author = data.get("author")
    license_id = data.get("license")
    license_needs_review = False
    if license_id:
        if not is_known_spdx(license_id):
            raise ManifestError(
                REASON_UNKNOWN_SPDX_LICENSE,
                f"{license_id!r} is not an allowlisted SPDX identifier",
            )
        license_needs_review = license_requires_review(license_id)

    source_url = data.get("source_url")
    if source_url is not None and not is_https_url(source_url):
        raise ManifestError(
            REASON_INVALID_SOURCE_URL, f"{source_url!r} must be an https:// URL"
        )

    homepage_url = data.get("homepage_url")
    if homepage_url is not None and not is_https_url(homepage_url):
        raise ManifestError(
            REASON_INVALID_HOMEPAGE_URL, f"{homepage_url!r} must be an https:// URL"
        )

    alternative_to = tuple(data.get("alternative_to") or ())
    for entry in alternative_to:
        if not valid_alternative_to_entry(entry):
            raise ManifestError(REASON_INVALID_ALTERNATIVE_TO, f"{entry!r}")

    notice = data.get("notice")
    if notice is not None and not valid_notice(notice):
        raise ManifestError(
            REASON_INVALID_NOTICE, "notice must be non-empty and <=10000 chars"
        )

    category = data.get("category")
    if category is not None and category not in MARKETPLACE_CATEGORIES:
        raise ManifestError(
            REASON_INVALID_CATEGORY,
            f"{category!r} is not one of {sorted(MARKETPLACE_CATEGORIES)}",
        )

    return AppManifest(
        app_id=app_id,
        name=name,
        version=version,
        feature=feature,
        module=module,
        provider=provider,
        surfaces=surfaces,
        permissions=permissions,
        config_schema=config_schema,
        is_default=is_default,
        stage_specs=stage_specs,
        execution_model=execution_model,
        compatible_with=compatible_with,
        incompatible_with=incompatible_with,
        platform_compatibility=platform_compatibility,
        author=author,
        license=license_id,
        license_requires_review=license_needs_review,
        source_url=source_url,
        alternative_to=alternative_to,
        homepage_url=homepage_url,
        notice=notice,
        category=category,
    )
