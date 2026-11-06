"""Publishes the platform connector service bundles to the bundle bucket (executor boot pin).

`core/bundle_executor/src/connector_boot.rs` pins each connector at startup from
`connectors/<platform>/<digest>.wasm` plus a signed sidecar `connectors/<platform>/<digest>.json`
(Ed25519, `BUNDLE_SIGNING_PUBLIC_KEYS`) and refuses to start when either is missing or invalid.
Nothing wrote those objects until now: connector bundles seeded through the normal app-bundle
path land under `bundles/<app_id>/<version>/...` and are never visible to the pin loader.

This module is the single publish path, shared by (a) the in-cluster core-bundle seeder (which
already holds the signing key + scoped S3 identity and the reproducibly-built wasm) and (b) the
operator CLI `cli/publish_connector_bundles.py` (`make publish-connector-bundles`).

Sidecar shape is the exact one `bundle_signing_service` emits (same `PlatformSigner`, same
payload), with `approval_id=0`: a connector has no per-tenant install approval -- its trust anchor
is the platform key plus the digest pinned in Helm, both checked by the executor.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from services import storage_service
from services.bundle_signing_service import PlatformSigner
from services.errors import ApiError

logger = logging.getLogger(__name__)

CONNECTOR_PLATFORMS: tuple[str, ...] = ("twitch", "discord", "youtube")
#: Host-constant app id the executor pins each connector under (`connector_boot.rs`).
CONNECTOR_APP_ID_PREFIX = "waddles.core.service."
#: Connector sidecars carry no per-tenant install approval.
CONNECTOR_APPROVAL_ID = 0
KEY_FILE_ENV = "BUNDLE_SIGNING_PRIVATE_KEY_FILE"
_PRIVATE_KEY_ENV = "BUNDLE_SIGNING_PRIVATE_KEY"
_WASM_MAGIC = b"\x00asm"


@dataclass(slots=True, frozen=True)
class ConnectorArtifact:
    """One connector to publish: platform, host-constant app id, catalog version, built wasm."""

    platform: str
    app_id: str
    version: str
    wasm_path: Path


@dataclass(slots=True, frozen=True)
class PublishedConnector:
    """What was written: the digest the Helm value / executor pin must carry, and both keys."""

    platform: str
    app_id: str
    version: str
    digest: str
    component_key: str
    sidecar_key: str
    key_id: str


def connector_app_id(platform: str) -> str:
    """The host-constant `app_id` for `platform` (`waddles.core.service.<p>`)."""
    return f"{CONNECTOR_APP_ID_PREFIX}{platform}"


def load_connector_artifacts(
    catalog_path: Path, bundles_dir: Path, platforms: tuple[str, ...] = CONNECTOR_PLATFORMS
) -> list[ConnectorArtifact]:
    """Resolves each requested platform to its catalog row's version + built artifact.

    Fails loud on an unknown platform, a platform missing from the catalog, a missing artifact
    file, or a file that is not a wasm binary -- never a silently-shorter publish.
    """
    unknown = [p for p in platforms if p not in CONNECTOR_PLATFORMS]
    if unknown or not platforms:
        raise ApiError(
            f"connector platforms must be a non-empty subset of {CONNECTOR_PLATFORMS}, "
            f"got {platforms!r}",
            500,
            "connector_publish_bad_platform",
        )
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    by_app = {item["app_id"]: item for item in raw.get("bundles") or []}
    artifacts: list[ConnectorArtifact] = []
    for platform in platforms:
        app_id = connector_app_id(platform)
        item = by_app.get(app_id)
        if item is None:
            raise ApiError(
                f"{app_id} is not in {catalog_path}", 500, "connector_publish_not_in_catalog"
            )
        wasm_path = bundles_dir / item["artifact_path"]
        if not wasm_path.is_file():
            raise ApiError(
                f"connector artifact {wasm_path} does not exist",
                500,
                "connector_publish_missing_artifact",
            )
        artifacts.append(ConnectorArtifact(platform, app_id, str(item["version"]), wasm_path))
    return artifacts


def signer_from_env() -> PlatformSigner:
    """Loads the active signer, preferring `BUNDLE_SIGNING_PRIVATE_KEY_FILE` over the env value.

    The key is a USER-held secret: the file form keeps it off argv/history/process env of the
    parent shell. A group/world-readable key file is refused. Unset -> `PlatformSigner.from_env`
    fails closed (`artifact_signing_key_unavailable`) -- no unsigned mode exists.
    """
    key_file = os.environ.get(KEY_FILE_ENV)
    if key_file:
        path = Path(key_file)
        if not path.is_file():
            raise ApiError(
                f"{KEY_FILE_ENV} points at a missing file", 500, "artifact_signing_key_unavailable"
            )
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ApiError(
                f"{KEY_FILE_ENV} file must not be group/world accessible (chmod 600)",
                500,
                "artifact_signing_key_invalid",
            )
        os.environ[_PRIVATE_KEY_ENV] = path.read_text(encoding="utf-8").strip()
    return PlatformSigner.from_env()


def digest_of(data: bytes) -> str:
    """`sha256:<64 lowercase hex>` -- the exact form `CONNECTOR_PIN_DIGEST_<P>` takes."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


async def publish_connector(
    artifact: ConnectorArtifact, signer: PlatformSigner
) -> PublishedConnector:
    """Signs and uploads one connector (component, then sidecar). Idempotent for same bytes."""
    data = artifact.wasm_path.read_bytes()
    if not data.startswith(_WASM_MAGIC):
        raise ApiError(
            f"{artifact.wasm_path} is not a wasm binary",
            500,
            "connector_publish_not_wasm",
        )
    digest = digest_of(data)
    signature, key_id = signer.sign(
        app_id=artifact.app_id,
        version=artifact.version,
        digest=digest,
        approval_id=CONNECTOR_APPROVAL_ID,
    )
    document: dict[str, Any] = {
        "app_id": artifact.app_id,
        "version": artifact.version,
        "digest": digest,
        "approval_id": CONNECTOR_APPROVAL_ID,
        "key_id": key_id,
        "algorithm": "ed25519",
        "signature": signature,
    }
    component_key, sidecar_key = await storage_service.write_connector_objects(
        artifact.platform, digest, data, document
    )
    logger.info(
        "connector publish: objects written",
        extra={
            "platform": artifact.platform,
            "digest": digest,
            "key_id": key_id,
            "bytes": len(data),
        },
    )
    return PublishedConnector(
        artifact.platform,
        artifact.app_id,
        artifact.version,
        digest,
        component_key,
        sidecar_key,
        key_id,
    )


async def publish_connectors(
    artifacts: list[ConnectorArtifact], signer: PlatformSigner
) -> list[PublishedConnector]:
    """Publishes every artifact; zero artifacts is a failure, never a vacuous success."""
    if not artifacts:
        raise ApiError("no connector artifacts to publish", 500, "connector_publish_empty")
    return [await publish_connector(a, signer) for a in artifacts]


def values_snippet(published: list[PublishedConnector]) -> str:
    """Helm values block carrying the digests (executor pin + svc-ingest sender digest)."""
    lines = ["pipeline:", "  connectors:", "    digests:"]
    lines += [f'      {p.platform}: "{p.digest}"' for p in published]
    return "\n".join(lines)
