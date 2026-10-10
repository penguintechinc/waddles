"""Parse + validate `bundle.yaml` v2 against hub-api's own pure-YAML rule subset.

Covers spec Sec6.4.4's pure-YAML rules -- everything checkable without a
compiled component (schema shape, stage/consumes rules, egress/limits/
data-table bounds, prebuilt gating). The two artifact-based rules (WIT
export presence and the per-language import allowlist) are checkable
only against a compiled component and are the M2 bundle-compiler's own
scope, not hub-api's -- this module never claims to check them.

This is hub-api's pre-Job pure-YAML pre-check (spec Sec9.2: "400 with a
reason code for a manifest that fails a pure-YAML rule") -- run before
any compiler Job would be created.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from flask_core.app_manifest import KNOWN_MODULES
from flask_core.bundle_attribution import (
    MARKETPLACE_CATEGORIES,
    is_https_url,
    is_known_spdx,
    license_requires_review,
    valid_alternative_to_entry,
    valid_notice,
)

from services.bundle_permission_catalog import (
    NET_HTTP_IP_PREFIXES,
    NET_HTTP_PREFIXES,
    is_valid_egress_host,
    resolve_risk,
)

logger = logging.getLogger(__name__)

_SEGMENT = r"[a-z0-9][a-z0-9_-]*"
_APP_ID_RE = re.compile(rf"^waddles\.{_SEGMENT}\.{_SEGMENT}\.{_SEGMENT}$")
_FEATURE_RE = re.compile(rf"^waddles\.{_SEGMENT}\.{_SEGMENT}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
_EGRESS_HOST_RE = re.compile(
    r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_RESERVED_TABLES = frozenset(
    {"users", "tenants", "communities", "app_catalog", "app_activations", "app_tenant_availability"}
)
_ALLOWED_LANGUAGES = frozenset({"python", "rust", "javascript", "typescript", "csharp", "other"})
_ALLOWED_STAGES = frozenset({"process", "action", "presentation"})
_MAX_TIMEOUT_MS = 10000
_MAX_MEMORY_MB = 256
_MAX_EGRESS_RPS = 10
#: First-party ingest/chat platforms a bundle may name in `supported_platforms`
#: (a `custom:<name>` entry is additionally validated against the tenant's
#: registered custom platforms, same as `consumes[].platform`).
KNOWN_SUPPORTED_PLATFORMS = frozenset({"discord", "twitch", "slack", "youtube", "kick"})


class ManifestV2Error(ValueError):
    """Raised when a bundle.yaml v2 dict fails a pure-YAML rule. `reason` is machine-checkable."""

    def __init__(self, reason: str, detail: str) -> None:
        """Store the machine-checkable `reason` code alongside the human-readable `detail`."""
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


@dataclass(slots=True, frozen=True)
class ConsumeRule:
    """One `consumes` rule (spec Sec6.4.3) -- what ingest platform/events a process stage reads."""

    platform: str
    source_id: str | None
    event_types: tuple[str, ...]
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class EgressRule:
    """One `egress` entry -- an allowed outbound host and its permitted HTTP methods."""

    host: str
    methods: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class Limits:
    """The `limits` block, with hub-api's own defaults applied when a field is omitted."""

    timeout_ms: int
    memory_mb: int
    egress_rps: int


@dataclass(slots=True, frozen=True)
class PermissionDeclaration:
    """One structured `permissions:` entry (spec Sec2.1) -- id, justification, per-id params.

    Replaces the dead free-form `permissions: []` string-list parsing
    (spec Sec2.4) -- `id` is a catalog member (`bundle_permission_catalog.
    resolve_risk`), `justification` is mandatory 1-280 chars of plain text,
    `params` is the permission-specific dict validated per Sec2.2.
    """

    id: str
    risk: str
    justification: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class BundleManifestV2:
    """A validated `bundle.yaml` v2 manifest -- the shape every downstream reader consumes."""

    schema_version: int
    app_id: str
    name: str
    version: str
    feature: str
    module: str
    provider: str
    language: str
    artifact: str
    execution_model: str
    is_default: bool
    stages: dict[str, Any]
    egress: tuple[EgressRule, ...]
    data_tables: tuple[str, ...]
    limits: Limits
    permissions: tuple[str, ...]
    routes_to: tuple[str, ...]
    consumes: tuple[ConsumeRule, ...]
    # Attribution/marketplace metadata (migration 0026) -- defaulted so
    # every pre-existing direct `BundleManifestV2(...)` construction site
    # (test fixtures building one by hand, not through `parse_bundle_
    # manifest_v2`) keeps working unchanged; `parse_bundle_manifest_v2` and
    # `bundle_approval_service._reparse_trusted` both always pass these
    # explicitly.
    author: str | None = None
    license: str | None = None
    license_requires_review: bool = False
    source_url: str | None = None
    alternative_to: tuple[str, ...] = ()
    homepage_url: str | None = None
    notice: str | None = None
    category: str | None = None
    # spec Sec2.1 -- the structured permission-catalog block, replacing the
    # dead `permissions: []` string-list parsing above. Defaulted so every
    # pre-existing direct `BundleManifestV2(...)` construction site (test
    # fixtures, `bundle_approval_service._reparse_trusted`) keeps working
    # unchanged; `parse_bundle_manifest_v2` always passes it explicitly.
    permission_declarations: tuple[PermissionDeclaration, ...] = ()
    # Issue #685 -- platforms this bundle may run on. `None` (field absent)
    # means ALL platforms (back-compat for every pre-existing bundle). Rides
    # in the stored `manifest_json` blob; no DB column.
    supported_platforms: tuple[str, ...] | None = None

    def supports_platform(self, platform: str) -> bool:
        """Return True when this bundle may run on `platform` (absent field = every platform)."""
        return self.supported_platforms is None or platform in self.supported_platforms


def _parse_supported_platforms(
    raw_value: Any, *, known_custom_platforms: frozenset[str]
) -> tuple[str, ...] | None:
    """Validate the optional `supported_platforms` list; `None` when absent (all platforms)."""
    if raw_value is None:
        return None
    _require(
        isinstance(raw_value, list) and bool(raw_value),
        "invalid_supported_platforms",
        "supported_platforms must be a non-empty list when present",
    )
    seen: list[str] = []
    for entry in raw_value:
        _require(
            isinstance(entry, str), "invalid_supported_platforms", f"{entry!r} is not a string"
        )
        if entry.startswith("custom:"):
            _require(
                entry.removeprefix("custom:") in known_custom_platforms,
                "invalid_supported_platforms",
                f"{entry!r} is not registered for this tenant",
            )
        else:
            _require(
                entry in KNOWN_SUPPORTED_PLATFORMS,
                "invalid_supported_platforms",
                f"{entry!r} is not one of {sorted(KNOWN_SUPPORTED_PLATFORMS)}",
            )
        if entry not in seen:
            seen.append(entry)
    return tuple(seen)


def _require(condition: bool, reason: str, detail: str) -> None:
    """Raise `ManifestV2Error(reason, detail)` when `condition` is false."""
    if not condition:
        raise ManifestV2Error(reason, detail)


def _parse_consumes(
    raw_rules: list[dict[str, Any]],
    *,
    known_custom_platforms: frozenset[str],
    allow_wildcard: bool,
) -> tuple[ConsumeRule, ...]:
    """Validate and build every `consumes` rule of a `process` stage."""
    rules: list[ConsumeRule] = []
    for rule in raw_rules:
        platform = rule.get("platform", "")
        event_types = rule.get("event_types", [])
        _require(bool(platform), "missing_field", "consumes[].platform is required")
        _require(bool(event_types), "missing_field", "consumes[].event_types must be non-empty")
        if platform.startswith("custom:"):
            name = platform.removeprefix("custom:")
            _require(
                name in known_custom_platforms,
                "unknown_consumes_platform",
                f"{platform!r} is not registered for this tenant",
            )
        elif platform == "*":
            _require(
                allow_wildcard,
                "wildcard_consumes_not_allowed",
                "allow_wildcard_consumes is false for this tenant",
            )
        for event_type in event_types:
            if event_type == "**" or "**" in event_type.split("."):
                _require(
                    allow_wildcard,
                    "wildcard_consumes_not_allowed",
                    f"{event_type!r} requires allow_wildcard_consumes",
                )
        rules.append(
            ConsumeRule(
                platform=platform,
                source_id=rule.get("source_id"),
                event_types=tuple(event_types),
                filters=dict(rule.get("filters") or {}),
            )
        )
    return tuple(rules)


_MAX_JUSTIFICATION_LEN = 280
_MAX_OVERLAY_DURATION_S = 30
_MAX_REPUTATION_DELTA = 5
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_.]*$")


def parse_permission_declarations(
    raw_entries: list[dict[str, Any]],
) -> tuple[PermissionDeclaration, ...]:
    """Validate+build every structured `permissions:` entry (spec Sec2.1/2.2/2.3).

    The catalog is closed (`unknown_permission` on anything `resolve_risk`
    doesn't recognize); every `dangerous`-risk entry requires a non-empty
    `justification` (Sec2.3); `storage.tables` requires `params.schema` and
    vice versa (Sec2.3's mutual-requirement rule); `overlay.media` requires
    a non-empty `allowed_hosts` and a `max_duration_seconds` within the
    catalog ceiling; `reputation.*.write` bounds `delta_min`/`delta_max`
    within `|delta| <= 5` and validates every `reason_codes` entry.
    `interaction.pii.receive` (raw PII in form/modal/interaction inputs,
    DEFAULT NO/`dangerous`) takes no special `params` -- like any other
    dangerous, non-parameterized catalog entry it only needs the
    justification above; the host's default-filter behavior when it is
    NOT granted is enforced at delivery time, not at manifest-parse time.
    """
    declarations: list[PermissionDeclaration] = []
    for entry in raw_entries:
        permission_id = entry.get("id", "")
        risk = resolve_risk(permission_id)
        _require(risk is not None, "unknown_permission", f"{permission_id!r}")
        assert risk is not None  # nosec B101 -- _require already raised above otherwise

        justification = entry.get("justification", "")
        _require(
            0 < len(justification) <= _MAX_JUSTIFICATION_LEN,
            "missing_justification",
            f"{permission_id!r} requires justification (1-{_MAX_JUSTIFICATION_LEN} chars)",
        )

        params = dict(entry.get("params") or {})

        if permission_id.startswith(NET_HTTP_PREFIXES):
            methods = params.get("methods")
            _require(
                isinstance(methods, list) and bool(methods) and set(methods) <= _ALLOWED_METHODS,
                "invalid_net_http_method",
                f"{permission_id!r} params.methods must be a non-empty subset of "
                f"{sorted(_ALLOWED_METHODS)}, got {methods!r}",
            )
            if permission_id.startswith(NET_HTTP_IP_PREFIXES):
                logger.warning(
                    "bundle manifest declares an IP-literal outbound permission %r -- "
                    "prefer net.http.fqdn:<host> where a stable hostname is available; "
                    "net.http.public-ip/private-ip are dangerous-risk and require explicit "
                    "per-tenant/community re-consent",
                    permission_id,
                )
        elif "methods" in params:
            _require(
                False,
                "invalid_net_http_method",
                "params.methods is only valid for a net.http.fqdn/public-ip/private-ip permission",
            )

        if permission_id == "storage.tables":
            _require(
                bool(params.get("schema")),
                "storage_tables_requires_schema",
                "storage.tables requires params.schema",
            )
        elif "schema" in params:
            _require(
                False,
                "storage_tables_requires_schema",
                "params.schema is only valid for storage.tables",
            )

        if permission_id == "overlay.media":
            allowed_hosts = params.get("allowed_hosts") or []
            _require(
                bool(allowed_hosts), "invalid_overlay_hosts", "overlay.media requires allowed_hosts"
            )
            for host in allowed_hosts:
                _require(
                    is_valid_egress_host(host, allow_wildcard=False),
                    "invalid_overlay_hosts",
                    f"{host!r} is not a valid, non-wildcard host",
                )
            max_duration = int(params.get("max_duration_seconds", 0))
            _require(
                0 < max_duration <= _MAX_OVERLAY_DURATION_S,
                "invalid_overlay_duration",
                f"max_duration_seconds={max_duration}",
            )

        if permission_id in ("reputation.community.write", "reputation.tenant.write"):
            delta_min = int(params.get("delta_min", 0))
            delta_max = int(params.get("delta_max", 0))
            _require(
                -_MAX_REPUTATION_DELTA <= delta_min <= 0 <= delta_max <= _MAX_REPUTATION_DELTA,
                "delta_out_of_bounds",
                f"delta_min={delta_min} delta_max={delta_max}",
            )
            reason_codes = params.get("reason_codes") or []
            _require(bool(reason_codes), "missing_reason_codes", f"{permission_id!r}")
            for code in reason_codes:
                _require(bool(_REASON_CODE_RE.match(code)), "invalid_reason_code", f"{code!r}")

        declarations.append(
            PermissionDeclaration(
                id=permission_id, risk=risk, justification=justification, params=params
            )
        )
    return tuple(declarations)


def parse_bundle_manifest_v2(
    raw: dict[str, Any],
    *,
    known_custom_platforms: frozenset[str],
    allow_wildcard_consumes: bool,
    allow_prebuilt: bool,
) -> BundleManifestV2:
    """Parse+validate `raw` (a `yaml.safe_load`-d manifest). Raises `ManifestV2Error` on failure."""
    for key in (
        "schema_version",
        "app_id",
        "name",
        "version",
        "feature",
        "module",
        "provider",
        "language",
        "artifact",
        "stages",
    ):
        _require(key in raw, "missing_field", f"{key} is required")

    _require(
        raw["schema_version"] == 2,
        "unsupported_schema_version",
        f"got {raw['schema_version']!r}, expected 2",
    )
    _require(
        bool(_SEMVER_RE.match(raw["version"])),
        "bad_semver",
        f"{raw['version']!r} is not valid SemVer 2.0.0",
    )
    _require(
        bool(_APP_ID_RE.match(raw["app_id"])),
        "not_namespaced",
        f"{raw['app_id']!r} is not a valid app_id",
    )
    _require(
        bool(_FEATURE_RE.match(raw["feature"])),
        "not_namespaced",
        f"{raw['feature']!r} is not a valid feature id",
    )
    _require(
        raw["module"] in KNOWN_MODULES,
        "unknown_module",
        f"{raw['module']!r} is not a KNOWN_MODULES entry",
    )
    _require(
        raw["feature"] == raw["app_id"].rsplit(".", 1)[0],
        "feature_prefix_mismatch",
        "feature must equal app_id minus its last segment",
    )
    _require(
        raw["module"] == raw["feature"].split(".")[1],
        "feature_prefix_mismatch",
        "module must equal feature's second segment",
    )
    _require(
        raw["provider"] in {"builtin", "thirdparty"}, "invalid_provider", f"{raw['provider']!r}"
    )
    _require(raw["language"] in _ALLOWED_LANGUAGES, "unsupported_language", f"{raw['language']!r}")
    _require(raw["artifact"] in {"source", "prebuilt"}, "invalid_provider", f"{raw['artifact']!r}")
    if raw["language"] == "other":
        _require(
            raw["artifact"] == "prebuilt",
            "unsupported_language",
            "language 'other' requires artifact: prebuilt",
        )
    if raw["artifact"] == "prebuilt":
        _require(allow_prebuilt, "prebuilt_not_allowed", "bundles.allow_prebuilt is false")

    # Attribution/marketplace metadata (optional for a first-party
    # `builtin` bundle; mandatory for a `thirdparty` -- i.e. vendor --
    # bundle, per house supply-chain policy: a vendor port must be
    # credited and its license terms known before it is ever onboarded).
    is_vendor_bundle = raw["provider"] == "thirdparty"
    author = raw.get("author")
    _require(
        bool(author) or not is_vendor_bundle,
        "vendor_author_required",
        "author is required for provider: thirdparty (vendor) bundles",
    )
    license_id = raw.get("license")
    _require(
        bool(license_id) or not is_vendor_bundle,
        "vendor_license_required",
        "license is required for provider: thirdparty (vendor) bundles",
    )
    requires_review = False
    if license_id:
        _require(
            is_known_spdx(license_id),
            "unknown_spdx_license",
            f"{license_id!r} is not an allowlisted SPDX identifier",
        )
        requires_review = license_requires_review(license_id)

    source_url = raw.get("source_url")
    if source_url is not None:
        _require(
            is_https_url(source_url),
            "invalid_source_url",
            f"{source_url!r} must be an https:// URL",
        )

    homepage_url = raw.get("homepage_url")
    if homepage_url is not None:
        _require(
            is_https_url(homepage_url),
            "invalid_homepage_url",
            f"{homepage_url!r} must be an https:// URL",
        )

    alternative_to = tuple(raw.get("alternative_to") or [])
    for entry in alternative_to:
        _require(valid_alternative_to_entry(entry), "invalid_alternative_to", f"{entry!r}")

    notice = raw.get("notice")
    if notice is not None:
        _require(
            valid_notice(notice), "invalid_notice", "notice must be non-empty and <=10000 chars"
        )

    category = raw.get("category")
    if category is not None:
        _require(
            category in MARKETPLACE_CATEGORIES,
            "invalid_category",
            f"{category!r} is not one of {sorted(MARKETPLACE_CATEGORIES)}",
        )

    stages = raw["stages"]
    _require(bool(stages), "no_stages_declared", "stages must be non-empty")
    _require(
        "ingest" not in stages, "ingest_not_pluggable", "ingest is fixed code, not bundle-pluggable"
    )
    for stage_name in stages:
        _require(stage_name in _ALLOWED_STAGES, "unknown_surface", f"{stage_name!r}")

    consumes: tuple[ConsumeRule, ...] = ()
    if "process" in stages:
        process_consumes = stages["process"].get("consumes") or []
        _require(
            bool(process_consumes), "consumes_required", "a process stage must declare consumes"
        )
        consumes = _parse_consumes(
            process_consumes,
            known_custom_platforms=known_custom_platforms,
            allow_wildcard=allow_wildcard_consumes,
        )
    if "action" in stages:
        _require(
            not stages["action"].get("consumes"),
            "consumes_on_action_stage",
            "an action stage must not declare consumes",
        )

    supported_platforms = _parse_supported_platforms(
        raw.get("supported_platforms"), known_custom_platforms=known_custom_platforms
    )
    if supported_platforms is not None:
        for rule in consumes:
            _require(
                rule.platform in supported_platforms,
                "consumes_platform_unsupported",
                f"consumes platform {rule.platform!r} is not in supported_platforms",
            )

    egress_rules: list[EgressRule] = []
    for entry in raw.get("egress") or []:
        host = entry.get("host", "")
        _require(
            bool(_EGRESS_HOST_RE.match(host)) and "://" not in host,
            "invalid_egress_host",
            f"{host!r}",
        )
        methods = tuple(entry.get("methods") or sorted(_ALLOWED_METHODS))
        _require(set(methods) <= _ALLOWED_METHODS, "invalid_egress_method", f"{methods!r}")
        egress_rules.append(EgressRule(host=host, methods=methods))

    tables: list[str] = []
    for table in (raw.get("data") or {}).get("tables") or []:
        _require(bool(_TABLE_RE.match(table)), "invalid_data_table", f"{table!r}")
        _require(
            table not in _RESERVED_TABLES,
            "reserved_data_table",
            f"{table!r} is a reserved identity table",
        )
        tables.append(table)

    permission_entries = raw.get("permissions") or []
    # Back-compat: a pre-existing bare string-list `permissions: [...]`
    # (the dead, never-enforced shape, spec Sec2.4) parses to zero
    # structured declarations rather than erroring -- only dict entries
    # (the new, real schema) are validated against the catalog.
    permission_declarations = parse_permission_declarations(
        [e for e in permission_entries if isinstance(e, dict)]
    )

    raw_limits = raw.get("limits") or {}
    timeout_ms = int(raw_limits.get("timeout_ms", 2000))
    memory_mb = int(raw_limits.get("memory_mb", 64))
    egress_rps = int(raw_limits.get("egress_rps", 10))
    _require(50 <= timeout_ms <= _MAX_TIMEOUT_MS, "limit_out_of_range", f"timeout_ms={timeout_ms}")
    _require(8 <= memory_mb <= _MAX_MEMORY_MB, "limit_out_of_range", f"memory_mb={memory_mb}")
    _require(1 <= egress_rps <= _MAX_EGRESS_RPS, "limit_out_of_range", f"egress_rps={egress_rps}")

    return BundleManifestV2(
        schema_version=raw["schema_version"],
        app_id=raw["app_id"],
        name=raw["name"],
        version=raw["version"],
        feature=raw["feature"],
        module=raw["module"],
        provider=raw["provider"],
        language=raw["language"],
        artifact=raw["artifact"],
        execution_model=raw.get("execution_model", "native"),
        is_default=bool(raw.get("is_default", False)),
        stages=stages,
        egress=tuple(egress_rules),
        data_tables=tuple(tables),
        limits=Limits(timeout_ms=timeout_ms, memory_mb=memory_mb, egress_rps=egress_rps),
        permissions=tuple(e for e in permission_entries if isinstance(e, str)),
        permission_declarations=permission_declarations,
        routes_to=tuple(raw.get("routes_to") or []),
        consumes=consumes,
        author=author,
        license=license_id,
        license_requires_review=requires_review,
        source_url=source_url,
        alternative_to=alternative_to,
        homepage_url=homepage_url,
        notice=notice,
        category=category,
        supported_platforms=supported_platforms,
    )
