"""The install-time consent summary and its canonical `permission_hash` (spec Sec9.7)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from services.bundle_manifest_v2 import BundleManifestV2


def build_permission_summary(
    manifest: BundleManifestV2,
    *,
    grant_labels: list[dict[str, str]],
    component_capabilities: frozenset[str],
    min_tier: str,
    flag_key: str,
    allow_private_hosts: bool,
) -> dict[str, Any]:
    """Build the consent-screen summary -- the record `permission_hash()` hashes.

    `grant_labels` is the already-resolved, human-readable grant list
    (spec Sec5.2) -- this function renders it, it does not resolve it.
    `component_capabilities` is what the compiled component actually
    imports (cross-checked against the manifest's requests at install
    time, spec Sec9.7.1); until the M2 compiler reports an imports list
    on its artifact callback, callers derive this from the manifest's
    declared shape (see `bundle_approval_service._derive_capabilities`).
    """
    unusual: list[str] = []
    if allow_private_hosts:
        unusual.append("allow_private_hosts")
    if any(rule.platform == "*" for rule in manifest.consumes):
        unusual.append("wildcard_consumes")
    if manifest.routes_to:
        unusual.append("routes_to")
    if manifest.artifact == "prebuilt":
        unusual.append("prebuilt_artifact")

    return {
        "streams": list(grant_labels),
        "egress": [{"host": rule.host, "methods": list(rule.methods)} for rule in manifest.egress],
        "database": [{"table": table, "readWrite": "read_write"} for table in manifest.data_tables],
        "capabilities": sorted(component_capabilities),
        "routesTo": list(manifest.routes_to),
        "limits": {
            "timeoutMs": manifest.limits.timeout_ms,
            "memoryMb": manifest.limits.memory_mb,
            "egressRps": manifest.limits.egress_rps,
        },
        "provenance": {"language": manifest.language, "artifactKind": manifest.artifact},
        "entitlement": {"minTier": min_tier, "flagKey": flag_key},
        "unusual": unusual,
    }


def canonical_json(summary: dict[str, Any]) -> str:
    """Sorted-key, no-insignificant-whitespace JSON -- the same permissions always hash the same."""
    return json.dumps(summary, sort_keys=True, separators=(",", ":"))


def permission_hash(summary: dict[str, Any]) -> str:
    """`"sha256:" + 64 hex` over `canonical_json(summary)`."""
    return "sha256:" + hashlib.sha256(canonical_json(summary).encode("utf-8")).hexdigest()
