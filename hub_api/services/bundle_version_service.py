"""Bundle version upload + the pre-publish state machine (spec Sec9.1, Sec9.2).

`app_version_uploads` is hub-api's own pre-publish lifecycle tracker --
`app_versions` (migration 0022) has no status column by design (spec
Sec6.10) and is written only once a publisher measures a compiled
artifact. R52 (coordinator ruling): this table is one of the M2a
milestone's own new tables, so every function here queries it through
the penguin-dal `install_dal: AsyncDB` (`services/bundle_install_dal.py`),
never a new pydal binder/query.

**Scope note.** `create_version()` validates the manifest, enforces the
size ceilings, and persists the `UPLOADED` row -- it deliberately does
NOT launch a compiler Job or stage bytes to a bucket: the bundle-compiler
and bucket-flow pieces (spec Sec16 M2's own "bundle-compiler" and
"Bucket flow" rows) are separate, not-yet-built deliverables. Advancing
a version past `UPLOADED` today happens by a direct `install_dal`
write (as the approval-service tests do) until that follow-on wires a
real callback path; `advance_state()` below is the transition-table
guard so that follow-on has a single, tested place to call into rather
than hand-rolling status writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml
from penguin_dal import AsyncDB

from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2
from services.errors import ApiError, conflict, not_found

STATUS_UPLOADED = "UPLOADED"
STATUS_VALIDATING = "VALIDATING"
STATUS_SCANNING = "SCANNING"
STATUS_INSPECTING = "INSPECTING"
STATUS_COMPILING = "COMPILING"
STATUS_ADDRESSING = "ADDRESSING"
STATUS_PUBLISHING = "PUBLISHING"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_REJECTED = "REJECTED"

#: The state machine's directed edges (spec Sec9.1's diagram). Every
#: non-terminal state may also transition to STATUS_REJECTED -- listed
#: explicitly per source state rather than as a blanket exception, so
#: `advance_state()` can name the exact failure state in its error.
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_UPLOADED: frozenset({STATUS_VALIDATING, STATUS_REJECTED}),
    STATUS_VALIDATING: frozenset({STATUS_SCANNING, STATUS_INSPECTING, STATUS_REJECTED}),
    STATUS_SCANNING: frozenset({STATUS_ADDRESSING, STATUS_REJECTED}),
    STATUS_INSPECTING: frozenset({STATUS_COMPILING, STATUS_ADDRESSING, STATUS_REJECTED}),
    STATUS_COMPILING: frozenset({STATUS_ADDRESSING, STATUS_REJECTED}),
    STATUS_ADDRESSING: frozenset({STATUS_PUBLISHING, STATUS_REJECTED}),
    STATUS_PUBLISHING: frozenset({STATUS_PUBLISHED, STATUS_REJECTED}),
    STATUS_PUBLISHED: frozenset(),  # terminal -- superseded via a new version, never mutated
    STATUS_REJECTED: frozenset(),  # terminal
}

BUNDLE_MAX_SOURCE_BYTES = 16_777_216
BUNDLE_MAX_COMPONENT_BYTES = 33_554_432
#: Generous ceiling for a YAML manifest -- also bounds the raw input size to
#: `yaml.safe_load()` below, which `safe_load` alone does not: it blocks
#: arbitrary code execution but not a small-input/huge-output alias-bomb
#: (anchors/aliases are core YAML, usable under `safe_load` too). Capping
#: the byte count the parser ever sees is the defensible bound available
#: without swapping in a anchor-limiting YAML loader.
BUNDLE_MAX_MANIFEST_BYTES = 1_048_576
#: The largest single multipart request this endpoint should ever accept --
#: manifest + source + component all present at once, plus a small margin
#: for multipart boundaries/headers. Wired into `app.py`'s
#: `MAX_CONTENT_LENGTH` so Quart refuses an oversize body while it is still
#: streaming in, instead of after buffering the whole thing.
BUNDLE_MAX_REQUEST_BYTES = (
    BUNDLE_MAX_SOURCE_BYTES + BUNDLE_MAX_COMPONENT_BYTES + BUNDLE_MAX_MANIFEST_BYTES + 65_536
)


def valid_transition(current: str, target: str) -> bool:
    """Whether `current -> target` is a legal edge of the spec Sec9.1 state machine."""
    return target in _TRANSITIONS.get(current, frozenset())


async def advance_state(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    target: str,
    reject_reason: str | None = None,
) -> Any:
    """Move an `app_version_uploads` row to `target`, refusing an illegal transition.

    Raises `ApiError` 409 `invalid_state_transition` if `target` is not
    reachable from the row's current status (spec Sec9.1's diagram).
    """
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    upload = rows.first()
    if upload is None:
        raise not_found(f"version {version} of {app_id} not found")
    if not valid_transition(upload.status, target):
        raise ApiError(
            f"cannot move {app_id}@{version} from {upload.status} to {target}",
            409,
            "invalid_state_transition",
        )
    await install_dal(install_dal.app_version_uploads.id == upload.id).update(
        status=target,
        reject_reason=reject_reason,
        updated_at=datetime.now(UTC),
    )
    return (await install_dal(install_dal.app_version_uploads.id == upload.id).select()).first()


def _require(condition: bool, message: str, code: str) -> None:
    """Raise `ApiError(message, 400, code)` when `condition` is false."""
    if not condition:
        raise ApiError(message, 400, code)


async def create_version(
    install_dal: AsyncDB,
    *,
    tenant_id: int,
    app_id: str,
    requested_by: int,
    manifest_bytes: bytes,
    source_bytes: bytes | None,
    component_bytes: bytes | None,
    known_custom_platforms: frozenset[str],
    allow_wildcard_consumes: bool,
    allow_prebuilt: bool,
) -> Any:
    """Validate and persist a new bundle version upload at status `UPLOADED`.

    Raises `ApiError` 400 (manifest rule failure or a manifest `app_id`
    that does not match `app_id`, `code` = the rule's `reason` /
    `"app_id_mismatch"`), 403 `prebuilt_not_allowed`, 409 (version
    already exists), or 413 (oversize part). See this module's own
    docstring for why no compiler Job is launched here.
    """
    if len(manifest_bytes) > BUNDLE_MAX_MANIFEST_BYTES:
        raise ApiError("manifest exceeds 1 MiB", 413, "PAYLOAD_TOO_LARGE")
    raw = yaml.safe_load(manifest_bytes)
    try:
        manifest = parse_bundle_manifest_v2(
            raw,
            known_custom_platforms=known_custom_platforms,
            allow_wildcard_consumes=allow_wildcard_consumes,
            allow_prebuilt=allow_prebuilt,
        )
    except ManifestV2Error as exc:
        status_code = 403 if exc.reason == "prebuilt_not_allowed" else 400
        raise ApiError(str(exc), status_code, exc.reason) from exc

    # The row this function writes stores `app_id` from the URL while
    # `manifest_json` keeps the YAML's own `app_id` verbatim -- unchecked,
    # the two can diverge, letting an admin scoped to app A upload (and
    # later have approved) a manifest that actually describes app B.
    _require(
        manifest.app_id == app_id,
        f"manifest app_id {manifest.app_id!r} does not match the URL app_id {app_id!r}",
        "app_id_mismatch",
    )

    if manifest.artifact == "source" and source_bytes is None:
        raise ApiError("source part is required when artifact: source", 400, "missing_part")
    if manifest.artifact == "prebuilt" and component_bytes is None:
        raise ApiError("component part is required when artifact: prebuilt", 400, "missing_part")
    if source_bytes is not None and len(source_bytes) > BUNDLE_MAX_SOURCE_BYTES:
        raise ApiError("source tarball exceeds 16 MiB", 413, "PAYLOAD_TOO_LARGE")
    if component_bytes is not None and len(component_bytes) > BUNDLE_MAX_COMPONENT_BYTES:
        raise ApiError("component exceeds 32 MiB", 413, "PAYLOAD_TOO_LARGE")

    existing = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == manifest.version)
    ).select()
    if existing:
        raise conflict(f"version {manifest.version} of {app_id} already exists")

    now = datetime.now(UTC)
    upload_id = await install_dal.app_version_uploads.async_insert(
        app_id=app_id,
        version=manifest.version,
        tenant_id=tenant_id,
        requested_by=requested_by,
        artifact_kind=manifest.artifact,
        language=manifest.language,
        status=STATUS_UPLOADED,
        manifest_json=raw,
        created_at=now,
        updated_at=now,
    )
    rows = await install_dal(install_dal.app_version_uploads.id == upload_id).select()
    return rows.first()


async def get_version(install_dal: AsyncDB, *, app_id: str, version: str) -> Any:
    """The `app_version_uploads` row for `(app_id, version)`. Raises 404 if absent."""
    rows = await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).select()
    first = rows.first()
    if first is None:
        raise not_found(f"version {version} of {app_id} not found")
    return first


async def list_versions(install_dal: AsyncDB, *, app_id: str) -> list[Any]:
    """Every uploaded version of `app_id`, newest first."""
    rows = await install_dal(install_dal.app_version_uploads.app_id == app_id).select(
        orderby=~install_dal.app_version_uploads.created_at,
    )
    return list(rows)
