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

**Follow-on: `process_prebuilt_component()`.** This is that follow-on
for the `artifact: prebuilt` (pre-built component) branch only -- it
drives a freshly-`UPLOADED` row through `VALIDATING` -> `INSPECTING`
(WIT world conformance, `bundle_component_validator.validate_component`)
-> `ADDRESSING` (stage to the bucket keyed by sha256, per spec Sec9.1's
own "ADDRESSING: sha256 over the component bytes"), provisioning both
the `process` and `action` consumer groups along the way, then
`_publish_prebuilt_version()` creates the `app_versions` row
(`component_key`/`sidecar_key`, migration 0024 -- the exact MinIO keys
already uploaded, so the data-plane loader needs no digest-to-key
re-derivation) and advances straight to `PUBLISHED`: a successfully
staged prebuilt component has no separate compile/SAST gate to wait on.
The `artifact: source` branch (compiler Job + SAST, spec Sec9.1's SCANNING/
COMPILING states, and the `PUBLISHING` intermediate) is still out of
scope -- `blueprints/v1/bundle_versions.py` never calls this function
for a source upload. `PUBLISHED` is "staged", not "approved"/"active" --
see `services/bundle_approval_service.py::approve_version()` (spec
Sec9.7) for the separate GLOBAL-ADMIN-gated activation step.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

import yaml
from flask_core.stream_pipeline import bundle_stream_key
from penguin_dal import AsyncDB

from services import storage_service, valkey_admin_client
from services.bundle_component_validator import (
    ComponentValidatorUnavailableError,
    validate_component,
)
from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2
from services.bundle_telemetry import bundle_span, get_meter
from services.errors import ApiError, conflict, not_found

logger = logging.getLogger(__name__)

_meter = get_meter()
_onboarding_counter = _meter.create_counter(
    "waddles_hub_component_onboarding_total",
    description="pre-built component onboarding attempts, by outcome",
)

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
    # STATUS_PUBLISHED is a direct edge here (not only via STATUS_PUBLISHING)
    # for the pre-built-component path: `process_prebuilt_component()`
    # publishes immediately once staging succeeds -- there is no separate
    # compile/SAST step to gate on for an already-validated component
    # (that gate is what STATUS_PUBLISHING exists for on the `source`
    # artifact path, still a not-yet-built follow-on, see this module's
    # own scope note above `process_prebuilt_component()`).
    STATUS_ADDRESSING: frozenset({STATUS_PUBLISHING, STATUS_PUBLISHED, STATUS_REJECTED}),
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
    requested_by: int | None,
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

    `requested_by=None` is the SYSTEM-actor case (`hub_api/cli/
    seed_core_bundles.py`, in-cluster at deploy time, no human
    requester) -- the column is a nullable FK (migration 0023), so this
    is a real NULL, never a fake `hub_users` row.
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


#: `?status=pending` (the global-admin approval queue's only caller-facing
#: value today, webui SuperAdminBundleApprovals.jsx) aliases to
#: `STATUS_PUBLISHED` -- "staged, not yet approved for any tenant" (see
#: this module's own docstring: PUBLISHED means staged, approval is the
#: separate `bundle_approval_service.approve_version()` step). Any other
#: literal `app_version_uploads.status` value (e.g. `REJECTED`) is passed
#: through unchanged so the same endpoint can also list denied versions.
_STATUS_ALIASES: dict[str, str] = {"pending": STATUS_PUBLISHED}


async def list_versions_by_status(
    install_dal: AsyncDB, *, status: str, page: int, limit: int
) -> tuple[list[Any], int]:
    """Cross-app `app_version_uploads` rows for the global-admin approval queue.

    Returns `(rows, total)` -- `rows` is the requested page (newest
    first), `total` is the full matching count for pagination metadata.
    Unlike `list_versions()` this is deliberately NOT scoped to one
    `app_id`: the approval queue spans every vendor/first-party namespace.
    """
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit
    resolved_status = _STATUS_ALIASES.get(status.lower(), status.upper())

    query = install_dal(install_dal.app_version_uploads.status == resolved_status)
    total = await query.count()
    rows = await query.select(
        orderby=~install_dal.app_version_uploads.created_at,
        limitby=(offset, offset + limit),
    )
    return list(rows), total


async def _set_staging_component_key(
    install_dal: AsyncDB, *, app_id: str, version: str, key: str
) -> None:
    """Record the bucket key `upload_bundle_component()` returned, independent of `advance_state()`.

    `advance_state()`'s own contract is the FSM guard only (status +
    `reject_reason`); this is a plain column write alongside it, kept as
    its own tiny helper rather than widening `advance_state()`'s
    signature and risking its existing, tested callers.
    """
    await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).update(staging_component_key=key, updated_at=datetime.now(UTC))


async def _publish_prebuilt_version(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    language: str,
    digest: str,
    component_key: str,
    sidecar_key: str,
) -> Any:
    """Create the `app_versions` row for a staged prebuilt component and advance it to PUBLISHED.

    Writes `component_key`/`sidecar_key` (migration 0024) -- the exact
    MinIO keys `process_prebuilt_component()` already uploaded to -- so
    the data-plane loader resolves a published version's bytes straight
    from `app_versions`, no digest-to-key re-derivation needed on that
    side. `scan_status="not_scanned"`: no SAST/scan step runs for the
    prebuilt-component path in this milestone (see this module's own
    scope note); `approve_version()` (bundle_approval_service.py) is the
    separate, GLOBAL-ADMIN-gated consent step that must still run before
    a PUBLISHED version is activated (`app_active_versions`) -- reaching
    PUBLISHED here is "successfully staged", never "approved" or "active".
    """
    now = datetime.now(UTC)
    new_version_id = await install_dal.app_versions.async_insert(
        app_id=app_id,
        version=version,
        artifact_digest=digest,
        component_key=component_key,
        sidecar_key=sidecar_key,
        language=language,
        artifact_kind="prebuilt",
        built_at=now,
        builder="hub_api",
        scan_status="not_scanned",
    )
    await install_dal(
        (install_dal.app_version_uploads.app_id == app_id)
        & (install_dal.app_version_uploads.version == version)
    ).update(app_version_id=new_version_id, updated_at=now)
    return await advance_state(install_dal, app_id=app_id, version=version, target=STATUS_PUBLISHED)


async def process_prebuilt_component(
    install_dal: AsyncDB,
    *,
    app_id: str,
    version: str,
    component_bytes: bytes,
    tenant_slug: str,
    valkey_client: Any | None = None,
) -> Any:
    """Drive a freshly-`UPLOADED` prebuilt-component row through `INSPECTING` -> `PUBLISHED`.

    Only ever called for `artifact_kind == "prebuilt"`; the caller
    (`blueprints/v1/bundle_versions.py`) never invokes this for a
    `source` upload. Order matters (this IS the security posture,
    matching this repo's own AUTHZ-before-body-work convention):

      1. `UPLOADED` -> `VALIDATING` -> `INSPECTING` (FSM entry).
      2. WIT world conformance check
         (`bundle_component_validator.validate_component`) -- a failure
         moves the row to `REJECTED` with the validator's reason and
         returns immediately, never reaching the bucket or Valkey.
      3. Stage the (now-validated) bytes to the bucket keyed by their own
         sha256 digest, record `staging_component_key`, advance to
         `ADDRESSING` (spec Sec9.1: "ADDRESSING: sha256 over the
         component bytes").
      4. Provision the `action` consumer group, tenant-scoped via
         `tenant_slug` (the tenant-middleware-derived value ONLY -- never
         client-supplied) and community-less (`community=None`, spec's
         tenant-wide `_tenant` segment): a version upload happens at the
         catalog level, before any community-specific install/activation
         (spec Sec9.5, out of this follow-on's scope). The `process`-stage
         group is deliberately NOT provisioned here -- svc-process reads
         each granted ingest-source's own stream
         (`waddles:t:{tenant}:c:{community|_tenant}:src:{platform}:
         {source_id}:events`, penguin-spine `Scope::source_stream`), not a
         per-app `...:app:{app_id}:process` key; provisioning that key
         instead grouped on a stream nothing ever writes to
         (`services/app_source_binding_service.py`'s own module docstring).
         `bundle_approval_service.approve_version()` provisions the real
         process-stage groups (AUTO-BIND) once an app is actually
         approved+installed and its consumed sources are known.
      5. `_publish_prebuilt_version()`: create the `app_versions` row
         (`component_key`/`sidecar_key` = the exact keys step 3 uploaded
         to, migration 0024) and advance `ADDRESSING` -> `PUBLISHED` --
         a successfully-staged prebuilt component is published
         immediately, no separate compile/SAST gate applies to it. This
         is "staged", not "approved" or "active" -- `approve_version()`
         (`bundle_approval_service.py`, GLOBAL-ADMIN-gated) is the
         separate step that activates it.

    `valkey_client`, when passed (tests only), is used as-is and left
    open for the caller to manage; when `None` (the real call site) a
    fresh client is built via `valkey_admin_client.build_client()` and
    always closed here.

    Raises `ApiError` 503 `component_validator_unavailable` -- NOT a
    `REJECTED` transition -- when the validator itself could not run
    (`ComponentValidatorUnavailableError`, e.g. `wasm-tools` missing): an
    infra problem is retriable and must never be conflated with "this
    component is non-conformant". The row is left at `INSPECTING` for a
    retry once the tool is available again.
    """
    async with bundle_span("hub.bundle.onboard_component", app_id=app_id, tenant=tenant_slug):
        await advance_state(install_dal, app_id=app_id, version=version, target=STATUS_VALIDATING)
        await advance_state(install_dal, app_id=app_id, version=version, target=STATUS_INSPECTING)
        logger.info(
            "bundle onboarding: inspecting component",
            extra={"app_id": app_id, "tenant": tenant_slug, "version": version},
        )

        try:
            result = await validate_component(component_bytes)
        except ComponentValidatorUnavailableError as exc:
            logger.error(
                "bundle onboarding: component validator unavailable",
                extra={"app_id": app_id, "tenant": tenant_slug, "version": version},
            )
            _onboarding_counter.add(1, {"outcome": "validator_unavailable"})
            raise ApiError(str(exc), 503, "component_validator_unavailable") from exc

        if not result.ok:
            logger.warning(
                "bundle onboarding: component rejected",
                extra={
                    "app_id": app_id,
                    "tenant": tenant_slug,
                    "version": version,
                    "reason": result.reason,
                },
            )
            _onboarding_counter.add(1, {"outcome": "rejected"})
            return await advance_state(
                install_dal,
                app_id=app_id,
                version=version,
                target=STATUS_REJECTED,
                reject_reason=(result.reason or "wit_conformance_failed")[:100],
            )

        digest = hashlib.sha256(component_bytes).hexdigest()
        key = await storage_service.upload_bundle_component(
            app_id, version, digest, component_bytes
        )
        await _set_staging_component_key(install_dal, app_id=app_id, version=version, key=key)
        row = await advance_state(
            install_dal, app_id=app_id, version=version, target=STATUS_ADDRESSING
        )
        logger.info(
            "bundle onboarding: component staged",
            extra={"app_id": app_id, "tenant": tenant_slug, "version": version, "key": key},
        )

        client = valkey_client if valkey_client is not None else valkey_admin_client.build_client()
        try:
            for stage in ("action",):
                stream_key = bundle_stream_key(tenant_slug, None, app_id, stage)
                await valkey_admin_client.ensure_group(client, stream=stream_key, group=app_id)
                logger.info(
                    "bundle onboarding: consumer group provisioned",
                    extra={"app_id": app_id, "tenant": tenant_slug, "stage": stage},
                )
        finally:
            if valkey_client is None:
                await client.aclose()

        sidecar_key = storage_service.bundle_sidecar_key(app_id, version, digest)
        published = await _publish_prebuilt_version(
            install_dal,
            app_id=app_id,
            version=version,
            language=row.language,
            digest=digest,
            component_key=key,
            sidecar_key=sidecar_key,
        )
        logger.info(
            "bundle onboarding: component published",
            extra={
                "app_id": app_id,
                "tenant": tenant_slug,
                "version": version,
                "component_key": key,
                "sidecar_key": sidecar_key,
            },
        )

        _onboarding_counter.add(1, {"outcome": "published"})
        return published
