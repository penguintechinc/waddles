"""Operator-triggered rotation and retirement of platform secrets (T8, security review H4/H7).

Rotation is NEVER implicit: upgrades, restarts and bootstrap never rotate. An authorised operator
calls the admin API (or the CLI that calls the same API); this module is what that call runs.

State machine per (type, scope) -- the database trigger enforces the same moves, forward only:

  rotate:  ACTIVE vN  ->  VERIFY-ONLY (at `rotated_at`)      and a NEW version N+1 is ACTIVE
  retire:  VERIFY-ONLY vK -> RETIRED (explicit, separate step; refused while the verify window
           of the type is still open, measured from the NEW active version's `activated_at`,
           server-side -- a client cannot shorten it)

A verify-only version still verifies (the cache honours it until `verify_until` = `rotated_at` +
the type's window) but is never used to sign. Each request is:

  * **claimed** in the replay ledger (`Idempotency-Key`, unique) -- a replayed key never executes;
  * **rate limited** (rotate: at most one new version per type per 10 minutes);
  * **audited** in `platform_vault.secret_events` -- actor UUID, int event code, int reason code,
    outcome, and NEVER a value, fingerprint, key id or free text -- including refusals (outcome
    failure), all inside the hash chain, under the same advisory lock the bootstrap uses so the
    chain stays linear.

Business refusals are RETURNED (:class:`Refusal`), not raised, so the session commits the claim and
the denial event; only programming / infrastructure errors raise and roll back. The functions here
are sync and repository-based (the HTTP layer runs them in a thread).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Any, Final

from services.envelope.platform_kek import PlatformKekAdapter
from services.platform_secrets import telemetry
from services.platform_secrets.cache import verify_until_for
from services.platform_secrets.escrow import (
    EscrowStore,
    escrow_store_from_env,
    sync_escrow,
)
from services.platform_secrets.event_chain import append_event
from services.platform_secrets.registry import (
    REGISTRY,
    ScopeRule,
    SecretSpec,
    SecretType,
)
from services.platform_secrets.repository import (
    ACTION_RETIRE,
    ACTION_ROTATE,
    EVENT_ACTIVATED,
    EVENT_RETIRED,
    EVENT_ROTATION_REQUESTED,
    EVENT_VERIFY_ONLY,
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    REASON_OP_COMPROMISE,
    REASON_OP_MAINTENANCE,
    REASON_OP_SCHEDULED,
    REASON_RETIRED,
    REASON_ROTATED,
    STATE_ACTIVE,
    STATE_RETIRED,
    STATE_VERIFY_ONLY,
    NewSecret,
    PlatformVaultRepository,
    SecretRow,
    SqlRepository,
)
from services.provider_connections.vault import (
    generate_platform_key_material,
    platform_secret_fingerprint,
    seal_platform_secret,
)

logger = logging.getLogger(__name__)

#: At most one rotation per (type, scope) in this window (H7). Retire is bounded by state instead.
ROTATION_MIN_INTERVAL: Final = timedelta(minutes=10)


class RotationReason(IntEnum):
    """Operator reason codes accepted on the API (stored as the migration-0110 codes 8/9/10)."""

    SCHEDULED = 1
    COMPROMISE = 2
    MAINTENANCE = 3


_REASON_TO_DB: Final = {
    RotationReason.SCHEDULED: REASON_OP_SCHEDULED,
    RotationReason.COMPROMISE: REASON_OP_COMPROMISE,
    RotationReason.MAINTENANCE: REASON_OP_MAINTENANCE,
}


def db_reason(reason: RotationReason) -> int:
    """The `reason` column code for an API reason."""
    return _REASON_TO_DB[reason]


@dataclass(slots=True, frozen=True)
class Refusal:
    """A business refusal: stable `code` word and HTTP `status`. Never carries a value."""

    code: str
    status: int


@dataclass(slots=True, frozen=True)
class RotateOutcome:
    """A completed rotation: names and ints only (no key id, value or fingerprint)."""

    type_name: str
    new_version: int
    previous_version: int
    verify_until: datetime | None


@dataclass(slots=True, frozen=True)
class RetireOutcome:
    """A completed retirement."""

    type_name: str
    version: int
    retired_at: datetime


@dataclass(slots=True, frozen=True)
class VersionStatus:
    """No-secrets summary of one version (state is the int code)."""

    version: int
    state: int
    activated_at: datetime | None
    rotated_at: datetime | None
    retired_at: datetime | None
    verify_until: datetime | None


@dataclass(slots=True, frozen=True)
class TypeStatus:
    """No-secrets summary of one registered type."""

    secret_type: int
    name: str
    versions: tuple[VersionStatus, ...]


def resolve_type(raw: str) -> SecretSpec | None:
    """Map the URL's `{type}` (registry name or int code) to a GLOBAL spec, else None."""
    spec: SecretSpec | None = None
    if raw.isdigit():
        try:
            spec = REGISTRY[SecretType(int(raw))]
        except (ValueError, KeyError):
            spec = None
    else:
        spec = next((s for s in REGISTRY.values() if s.name == raw), None)
    if spec is None or spec.scope_rule is not ScopeRule.GLOBAL:
        return None
    return spec


def _deny(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    spec: SecretSpec,
    actor: uuid.UUID,
    reason: RotationReason,
    code: str,
    status: int,
    now: datetime,
) -> Refusal:
    """Audit a refusal (event + metric + log) and build it."""
    append_event(
        repo,
        kek,
        secret_id=None,
        secret_type=int(spec.secret_type),
        scope_id=None,
        version=None,
        event_type=EVENT_ROTATION_REQUESTED,
        outcome=OUTCOME_FAILURE,
        reason=db_reason(reason),
        actor_uuid=actor,
        now=now,
    )
    telemetry.count_rotation(spec.name, code)
    logger.warning(
        "platform secret request refused type=%s code=%s actor=%s", spec.name, code, actor
    )
    return Refusal(code, status)


def record_denial(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    *,
    spec: SecretSpec,
    actor: uuid.UUID,
    reason: RotationReason,
    code: str,
    now: datetime | None = None,
) -> None:
    """Audit a refusal decided OUTSIDE this module (not system tenant / no MFA / CSRF / bad key)."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    _deny(repo, kek, spec, actor, reason, code, 403, moment)


def rotate_secret(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    *,
    spec: SecretSpec,
    actor: uuid.UUID,
    reason: RotationReason,
    idempotency_key: uuid.UUID,
    now: datetime | None = None,
) -> RotateOutcome | Refusal:
    """Mint version N+1 ACTIVE and demote the current active version to verify-only.

    Caller holds the advisory lock and the transaction. Refusals (returned, audited, committed by
    the session): `replayed` 409, `no_active` 409, `rate_limited` 429.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    secret_type = int(spec.secret_type)
    with telemetry.span("rotate", type=spec.name):
        if not repo.claim_request(
            idempotency_key, secret_type=secret_type, action=ACTION_ROTATE, actor_uuid=actor
        ):
            return _deny(repo, kek, spec, actor, reason, "replayed", 409, moment)
        rows = repo.rows_for(secret_type, None)
        active = [r for r in rows if r.state == STATE_ACTIVE]
        if len(active) != 1:
            return _deny(repo, kek, spec, actor, reason, "no_active", 409, moment)
        newest = max((r.activated_at for r in rows if r.activated_at), default=None)
        if newest is not None and moment - newest < ROTATION_MIN_INTERVAL:
            return _deny(repo, kek, spec, actor, reason, "rate_limited", 429, moment)
        old = active[0]
        version = max(r.version for r in rows) + 1
        material = generate_platform_key_material(int(spec.algorithm))
        public_id = uuid.uuid4()
        sealed = seal_platform_secret(
            material.plaintext,
            secret_type=secret_type,
            scope_id=None,
            version=version,
            public_id=public_id,
            kek=kek,
        )
        fingerprint = platform_secret_fingerprint(material.plaintext, kek=kek, kek_id=sealed.kek_id)
        if not repo.update_state(
            old.id, STATE_VERIFY_ONLY, at=moment, reason=REASON_ROTATED, actor_uuid=actor
        ):
            return _deny(repo, kek, spec, actor, reason, "no_active", 409, moment)
        created = repo.insert_secret(
            NewSecret(
                public_id=public_id,
                secret_type=secret_type,
                scope_id=None,
                version=version,
                algorithm=int(spec.algorithm),
                sealed_value=sealed.sealed_value,
                kek_id=sealed.kek_id,
                seal_version=sealed.seal_version,
                kid=material.kid,
                public_material=material.public_material,
                fingerprint=fingerprint,
                reason=REASON_ROTATED,
                activated_at=moment,
                actor_uuid=actor,
            )
        )
        if created is None:  # a concurrent writer won; the transaction rolls back (we raise)
            raise RuntimeError("platform secret rotation lost an insert race")
        common = {"scope_id": None, "outcome": OUTCOME_SUCCESS, "actor_uuid": actor, "now": moment}
        for secret_id, ver, event in (
            (old.id, old.version, EVENT_ROTATION_REQUESTED),
            (old.id, old.version, EVENT_VERIFY_ONLY),
            (created.id, version, EVENT_ACTIVATED),
        ):
            append_event(
                repo,
                kek,
                secret_id=secret_id,
                secret_type=secret_type,
                version=ver,
                event_type=event,
                reason=db_reason(reason),
                **common,  # type: ignore[arg-type]
            )
        repo.notify_changed()
        telemetry.count_rotation(spec.name, "ok")
        logger.info(
            "platform secret rotated type=%s version=%d previous=%d actor=%s",
            spec.name,
            version,
            old.version,
            actor,
        )
        return RotateOutcome(
            type_name=spec.name,
            new_version=version,
            previous_version=old.version,
            verify_until=verify_until_for(secret_type, STATE_VERIFY_ONLY, moment),
        )


def retire_secret(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    *,
    spec: SecretSpec,
    version: int,
    actor: uuid.UUID,
    reason: RotationReason,
    idempotency_key: uuid.UUID,
    now: datetime | None = None,
) -> RetireOutcome | Refusal:
    """Retire a verify-only version once the verify window (from the new active one) has passed.

    Refusals: `replayed` 409, `version_not_found` 404, `not_verify_only` 409 (an active version is
    never retired directly -- rotate first), `window_open` 409 (server-computed, not overridable).
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    secret_type = int(spec.secret_type)
    with telemetry.span("retire", type=spec.name):
        if not repo.claim_request(
            idempotency_key, secret_type=secret_type, action=ACTION_RETIRE, actor_uuid=actor
        ):
            return _deny(repo, kek, spec, actor, reason, "replayed", 409, moment)
        rows = repo.rows_for(secret_type, None)
        target = next((r for r in rows if r.version == version), None)
        if target is None:
            return _deny(repo, kek, spec, actor, reason, "version_not_found", 404, moment)
        if target.state != STATE_VERIFY_ONLY:
            return _deny(repo, kek, spec, actor, reason, "not_verify_only", 409, moment)
        active = next((r for r in rows if r.state == STATE_ACTIVE), None)
        window_end = _window_end(spec, active, target)
        if moment < window_end:
            return _deny(repo, kek, spec, actor, reason, "window_open", 409, moment)
        if not repo.update_state(
            target.id, STATE_RETIRED, at=moment, reason=REASON_RETIRED, actor_uuid=actor
        ):
            return _deny(repo, kek, spec, actor, reason, "not_verify_only", 409, moment)
        for event in (EVENT_ROTATION_REQUESTED, EVENT_RETIRED):
            append_event(
                repo,
                kek,
                secret_id=target.id,
                secret_type=secret_type,
                scope_id=None,
                version=target.version,
                event_type=event,
                outcome=OUTCOME_SUCCESS,
                reason=db_reason(reason),
                actor_uuid=actor,
                now=moment,
            )
        repo.notify_changed()
        telemetry.count_rotation(spec.name, "retired")
        logger.info(
            "platform secret retired type=%s version=%d actor=%s", spec.name, version, actor
        )
        return RetireOutcome(type_name=spec.name, version=version, retired_at=moment)


def _window_end(spec: SecretSpec, active: SecretRow | None, target: SecretRow) -> datetime:
    """When retirement becomes allowed: the new active version's `activated_at` + the window.

    Falls back to the target's own demotion time when there is no active row (cannot happen
    through the API, which rotates first, but a retire must never be easier than the rule).
    """
    anchor = (active.activated_at if active else None) or target.rotated_at or target.activated_at
    return (anchor or datetime.max.replace(tzinfo=UTC)) + timedelta(seconds=spec.verify_window_s)


def list_status(repo: PlatformVaultRepository) -> tuple[TypeStatus, ...]:
    """No-secrets status of every global registered type (versions, states, windows)."""
    out: list[TypeStatus] = []
    for spec in REGISTRY.values():
        if spec.scope_rule is not ScopeRule.GLOBAL:
            continue
        rows = repo.rows_for(int(spec.secret_type), None)
        out.append(
            TypeStatus(
                secret_type=int(spec.secret_type),
                name=spec.name,
                versions=tuple(
                    VersionStatus(
                        version=r.version,
                        state=r.state,
                        activated_at=r.activated_at,
                        rotated_at=r.rotated_at,
                        retired_at=r.retired_at,
                        verify_until=verify_until_for(r.secret_type, r.state, r.rotated_at),
                    )
                    for r in rows
                ),
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- sessions

SessionFactory = Callable[[], AbstractContextManager[PlatformVaultRepository]]

_STATE: dict[str, Any] = {}


def configure_session_factory(factory: SessionFactory | None) -> None:
    """Install (or clear) the repository session factory; the app wires the SQL one lazily."""
    _STATE["factory"] = factory


def sql_session_factory(database_url: str) -> SessionFactory:
    """A factory of locked, transactional SQL sessions for hub-api's real DB role.

    Each session is one transaction that first takes the bootstrap advisory lock (transaction
    scope, same key as the bootstrap's session lock), so rotations serialise with each other and
    with bootstrap, and the audit chain stays linear. Parameters are hidden from driver errors.
    """
    import sqlalchemy
    from sqlalchemy import text

    from bootstrap import BOOTSTRAP_LOCK_KEY

    sync_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = sqlalchemy.create_engine(
        sync_url, pool_pre_ping=True, pool_size=2, max_overflow=0, hide_parameters=True
    )

    @contextmanager
    def session() -> Iterator[PlatformVaultRepository]:
        with engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": BOOTSTRAP_LOCK_KEY})
            yield SqlRepository(conn)

    return session


def get_session_factory(database_url: str) -> SessionFactory:
    """The installed factory, else a SQL one built (once) from `database_url`."""
    factory = _STATE.get("factory")
    if factory is None:
        factory = sql_session_factory(database_url)
        _STATE["factory"] = factory
    return factory


def get_kek() -> PlatformKekAdapter:
    """The platform KEK from the environment (a missing/malformed one raises, never defaults)."""
    return PlatformKekAdapter.from_env()


def escrow_after_change(
    factory: SessionFactory,
    kek: PlatformKekAdapter,
    store: EscrowStore | None = None,
) -> None:
    """Escrow the new/changed versions after a committed rotation or retirement; never raises.

    Own locked session (an `escrowed` event extends the chain). An unreachable store is a metric
    and a log line; the next boot or rotation retries, and the rotation itself already succeeded.
    """
    try:
        target = store if store is not None else escrow_store_from_env()
        if target is None:
            return
        with factory() as repo:
            report = sync_escrow(repo, kek, target)
    except Exception as exc:  # noqa: BLE001 -- best effort by design; class name only logged
        telemetry.count_escrow("all", "error")
        logger.error("platform secret escrow after change failed (%s)", type(exc).__name__)
        return
    logger.info("platform secret escrow after change status=%s", report.status)
