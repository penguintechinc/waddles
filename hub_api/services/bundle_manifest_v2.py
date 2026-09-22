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

import re
from dataclasses import dataclass, field
from typing import Any

from flask_core.app_manifest import KNOWN_MODULES

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
_ALLOWED_LANGUAGES = frozenset({"python", "rust", "javascript", "typescript", "other"})
_ALLOWED_STAGES = frozenset({"process", "action", "presentation"})
_MAX_TIMEOUT_MS = 10000
_MAX_MEMORY_MB = 256
_MAX_EGRESS_RPS = 10


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
        permissions=tuple(raw.get("permissions") or []),
        routes_to=tuple(raw.get("routes_to") or []),
        consumes=consumes,
    )
