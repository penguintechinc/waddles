"""Boot-time creation, import and verification of the platform secrets (B2, H1, H4, M5).

Runs from `hub_api/bootstrap.py::_run_first_run_scripts`, i.e. while the process holds the
bootstrap advisory lock and the schema is at head. One transaction: either everything below is
committed or nothing is (a refusal rolls back, so a refused boot never leaves a half-written
marker). The decision table, in order:

  kill-switch flag ON ................ touch nothing (legacy sources stay authoritative)
  marker present, KEK cannot open it . REFUSE root_mismatch  (root KEK changed; generate nothing)
  marker present, `secrets` empty .... restore from the S3 escrow when permitted + verified,
                                       else REFUSE store_wiped / restore_blocked / escrow_*
  marker absent, `secrets` not empty . REFUSE marker_missing
  event chain does not verify ........ REFUSE chain_break
  active row won't open .............. REFUSE unopenable     (never regenerate on open failure)
  rows exist for a type, none active . REFUSE inconsistent
  marker absent AND store empty ...... genuine first run: write the marker; if the escrow holds a
                                       manifest, restore from it (permitted) or REFUSE
                                       (restore_blocked / escrow_*); only with NO escrow: create
  type has no row .................... first run only: import an allowlisted value, else generate

"First run" is therefore never inferred from "a row would not open". Creation is
`INSERT ... ON CONFLICT DO NOTHING` followed by a re-SELECT of the winner, and an existing active
row is never updated. Refusals raise :class:`PlatformVaultRefusedError` (fixed message, a metric, an
ERROR log): `run_bootstrap_loop` treats that as fatal, so the pod crash-loops visibly instead of
serving with keys it cannot trust. Nothing here logs or labels a value, fingerprint or ciphertext.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from services.envelope.errors import PlatformKekError
from services.envelope.platform_kek import PlatformKekAdapter
from services.platform_secrets import telemetry
from services.platform_secrets.cache import CachedSecret, PlatformSecretCache, verify_until_for
from services.platform_secrets.escrow import (
    MANIFEST_KEY as ESCROW_MANIFEST_KEY,
)
from services.platform_secrets.escrow import (
    EscrowError,
    EscrowMisconfiguredError,
    EscrowRefusedError,
    EscrowStore,
    EscrowUnavailableError,
    escrow_store_from_env,
    restore_from_escrow,
    restore_permitted,
    sync_escrow,
)
from services.platform_secrets.event_chain import append_event, verify_chain
from services.platform_secrets.importer import import_candidate
from services.platform_secrets.refresher import CacheRefresher, PgNotifyWaiter, make_loader
from services.platform_secrets.registry import REGISTRY, ScopeRule, SecretSpec, SecretType
from services.platform_secrets.repository import (
    EVENT_GENERATED,
    EVENT_IMPORTED,
    OUTCOME_SUCCESS,
    REASON_GENERATED,
    REASON_IMPORTED,
    NewSecret,
    PlatformVaultRepository,
    SecretRow,
    SqlRepository,
)
from services.provider_connections.vault import (
    PlatformKeyMaterial,
    PlatformSecretError,
    PlatformSecretKekUnavailableError,
    generate_platform_key_material,
    open_platform_secret,
    platform_root_mac,
    platform_secret_fingerprint,
    seal_platform_secret,
    verify_platform_root_mac,
)

logger = logging.getLogger(__name__)

_MANIFEST_KEY = ESCROW_MANIFEST_KEY

#: Flag key of the opt-out kill-switch: unseen / OFF = the database is the source of platform
#: keys (core mechanism, so default ON-path); ON = legacy sources stay authoritative.
KILL_SWITCH_FLAG = "waddles.disable-db-platform-keys"


class PlatformVaultRefusedError(RuntimeError):
    """Boot refused: the store cannot be trusted or the root changed. Message is a fixed code word.

    `reason` is one of `kek_missing`, `root_mismatch`, `store_wiped`, `marker_missing`,
    `chain_break`, `unopenable`, `inconsistent`, `restore_blocked`, `escrow_invalid`,
    `escrow_tampered`, `escrow_missing`, `escrow_unavailable`, `escrow_misconfigured`.
    """

    def __init__(self, reason: str) -> None:
        """Keep the reason code; the message carries nothing else."""
        super().__init__(f"platform vault refused to start: {reason}")
        self.reason = reason


@dataclass(slots=True, frozen=True)
class BootstrapReport:
    """What one bootstrap did. Names only (registry words); no values."""

    kill_switched: bool = False
    first_run: bool = False
    generated: tuple[str, ...] = ()
    imported: tuple[str, ...] = ()
    rejected_imports: tuple[str, ...] = ()
    events_examined: int = 0
    entries: tuple[CachedSecret, ...] = field(default=(), repr=False)


def db_platform_keys_disabled() -> bool:
    """True when the kill-switch is ON (ENV baseline `FLAG_WADDLES_DISABLE_DB_PLATFORM_KEYS`).

    Unset / unrecognised / false = OFF = the database is the source. Synchronous on purpose: the
    bootstrap runs on a worker thread before PostHog is reachable, so only the ENV baseline layer
    of the flag chain applies here; the setting is a core-mechanism opt-out, never a licence gate.
    """
    from flask_core.entitlement import env_flag_value

    return env_flag_value(KILL_SWITCH_FLAG) is True


def _refuse(reason: str, type_name: str = "all") -> PlatformVaultRefusedError:
    """Count, log (reason code only) and build the refusal."""
    outcome = {
        "root_mismatch": "mismatch",
        "store_wiped": "wiped",
        "marker_missing": "wiped",
        "chain_break": "chain_break",
        "unopenable": "unopenable",
        "inconsistent": "inconsistent",
        "restore_blocked": "restore_blocked",
        "escrow_invalid": "escrow_invalid",
        "escrow_missing": "escrow_invalid",
        "escrow_tampered": "escrow_tampered",
        "escrow_unavailable": "unavailable",
        "escrow_misconfigured": "error",
    }.get(reason, "error")
    telemetry.count_drift(type_name, outcome)
    logger.error("platform vault refused to start reason=%s", reason)
    return PlatformVaultRefusedError(reason)


def _check_root(repo: PlatformVaultRepository, kek: PlatformKekAdapter) -> None:
    """Refuse unless the loaded KEK is the one that wrote the marker (current or previous)."""
    marker = repo.read_marker()
    if marker is None:  # pragma: no cover -- callers read the marker first
        raise _refuse("marker_missing")
    try:
        same_root = verify_platform_root_mac(marker.check_mac, kek=kek, kek_id=marker.kek_id)
    except PlatformSecretKekUnavailableError:
        same_root = False
    if not same_root:
        raise _refuse("root_mismatch")


def _restore_or_refuse(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    escrow: EscrowStore,
    *,
    permitted: bool,
    now: datetime,
    refusal_if_empty: str | None,
) -> bool:
    """Try to rebuild the empty store from the escrow; True when rows were restored.

    No manifest in the escrow -> False (`refusal_if_empty`, when given, is then raised: the marker
    proves this is not a first run, so an empty store with no escrow is `store_wiped`). A manifest
    that exists but may not be used (`restore_blocked`: not alpha/local and no operator flag) or
    that fails verification (`escrow_tampered` / `escrow_invalid` / `escrow_missing`, including an
    object or manifest sealed under another KEK) refuses the boot and generates nothing. The
    restore itself is all-or-nothing inside the caller's transaction.
    """
    try:
        manifest_present = escrow.get(_MANIFEST_KEY) is not None
    except EscrowUnavailableError:
        raise _refuse("escrow_unavailable") from None
    if not manifest_present:
        if refusal_if_empty is not None:
            raise _refuse(refusal_if_empty)
        return False
    if not permitted:
        raise _refuse("restore_blocked")
    try:
        report = restore_from_escrow(repo, kek, escrow, now=now)
    except EscrowRefusedError as exc:
        raise _refuse(exc.reason) from None
    except EscrowUnavailableError:
        raise _refuse("escrow_unavailable") from None
    if report is None:  # pragma: no cover -- the manifest vanished between the two reads
        raise _refuse("escrow_unavailable")
    logger.warning("platform secrets restored from escrow count=%d", report.restored)
    return report.restored > 0


def _open_row(spec: SecretSpec, row: SecretRow, kek: PlatformKekAdapter) -> CachedSecret:
    """Open one stored row for the cache; any failure refuses the boot (never regenerates)."""
    if row.sealed_value is None:
        raise _refuse("unopenable", spec.name)
    try:
        with (
            telemetry.timed_open(spec.name),
            telemetry.span("open", type=spec.name, version=row.version),
        ):
            plaintext = open_platform_secret(
                row.sealed_value,
                secret_type=row.secret_type,
                scope_id=row.scope_id,
                version=row.version,
                public_id=row.public_id,
                kek=kek,
                kek_id=row.kek_id,
                seal_version=row.seal_version,
            )
    except PlatformSecretError:
        telemetry.count_open_failure(spec.name)
        raise _refuse("unopenable", spec.name) from None
    return CachedSecret(
        secret_type=row.secret_type,
        scope_id=row.scope_id,
        version=row.version,
        state=row.state,
        kid=row.kid,
        public_id=row.public_id,
        public_material=row.public_material,
        activated_at=row.activated_at,
        verify_until=verify_until_for(row.secret_type, row.state, row.rotated_at),
        plaintext=plaintext,
    )


def _create_version_one(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    spec: SecretSpec,
    *,
    material: PlatformKeyMaterial,
    imported: bool,
    now: datetime,
) -> bool:
    """Seal and insert version 1 ACTIVE; True if this call created it, False if it lost a race."""
    public_id = uuid.uuid4()
    sealed = seal_platform_secret(
        material.plaintext,
        secret_type=int(spec.secret_type),
        scope_id=None,
        version=1,
        public_id=public_id,
        kek=kek,
    )
    fingerprint = platform_secret_fingerprint(material.plaintext, kek=kek, kek_id=sealed.kek_id)
    row = repo.insert_secret(
        NewSecret(
            public_id=public_id,
            secret_type=int(spec.secret_type),
            scope_id=None,
            version=1,
            algorithm=int(spec.algorithm),
            sealed_value=sealed.sealed_value,
            kek_id=sealed.kek_id,
            seal_version=sealed.seal_version,
            kid=material.kid,
            public_material=material.public_material,
            fingerprint=fingerprint,
            reason=REASON_IMPORTED if imported else REASON_GENERATED,
            activated_at=now,
        )
    )
    if row is None:
        return False
    append_event(
        repo,
        kek,
        secret_id=row.id,
        secret_type=row.secret_type,
        scope_id=None,
        version=row.version,
        event_type=EVENT_IMPORTED if imported else EVENT_GENERATED,
        outcome=OUTCOME_SUCCESS,
        reason=REASON_IMPORTED if imported else REASON_GENERATED,
        now=now,
    )
    return True


def _ensure_version_one(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    spec: SecretSpec,
    *,
    first_run: bool,
    environ: Mapping[str, str],
    now: datetime,
) -> tuple[str, str]:
    """Make sure `spec` has an active row; returns `(action, rejected_outcome)`.

    `action` is `exists`, `imported`, `generated` or `conflict` (lost the insert race and adopted
    the winner). Import is attempted only on a first run and only for a spec with an allowlisted
    source; a rejected value is never imported and a fresh one is generated instead.
    """
    rows = repo.rows_for(int(spec.secret_type), None)
    if any(r.state == 2 for r in rows):
        return "exists", ""
    if rows:
        raise _refuse("inconsistent", spec.name)
    rejected = ""
    material: PlatformKeyMaterial | None = None
    imported = False
    if first_run and spec.import_source is not None:
        decision = import_candidate(spec, environ)
        if decision.usable:
            material, imported = decision.material, True
        elif decision.outcome.startswith("rejected"):
            rejected = decision.outcome
            telemetry.count_imported(spec.name, decision.outcome)
            logger.warning(
                "platform secret import rejected type=%s source=%s outcome=%s",
                spec.name,
                decision.source,
                decision.outcome,
            )
    with telemetry.span("generate", type=spec.name):
        if material is None:
            material = generate_platform_key_material(int(spec.algorithm))
        created = _create_version_one(
            repo, kek, spec, material=material, imported=imported, now=now
        )
    if not created:
        telemetry.count_generated(spec.name, "conflict")
        return "conflict", rejected
    if imported:
        telemetry.count_imported(spec.name, "ok")
        logger.info(
            "platform secret imported type=%s source=%s",
            spec.name,
            spec.import_source.private_env if spec.import_source else "",
        )
        return "imported", rejected
    telemetry.count_generated(spec.name, "ok")
    logger.info("platform secret generated type=%s", spec.name)
    return "generated", rejected


def bootstrap_platform_secrets(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    *,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    escrow: EscrowStore | None = None,
    restore_allowed: bool = False,
) -> BootstrapReport:
    """Run the decision table against `repo`; the caller holds the lock and the transaction.

    Raises :class:`PlatformVaultRefusedError` on any condition that must stop the boot. Returns the
    report including the opened entries to seed the cache with AFTER the transaction commits.
    """
    env = os.environ if environ is None else environ
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    with telemetry.span("bootstrap"):
        marker = repo.read_marker()
        count = repo.count_secrets()
        first_run = marker is None
        if marker is None:
            if count > 0:
                raise _refuse("marker_missing")
        else:
            _check_root(repo, kek)
            if count == 0:
                if escrow is None or not _restore_or_refuse(
                    repo,
                    kek,
                    escrow,
                    permitted=restore_allowed,
                    now=moment,
                    refusal_if_empty="store_wiped",
                ):
                    raise _refuse("store_wiped")
        chain = verify_chain(repo, kek)
        if not chain.intact:
            raise _refuse("chain_break")
        if first_run and not repo.insert_marker(kek.kek_ids[0], platform_root_mac(kek)):
            # Lost the marker race: another bootstrap wrote it -- be a normal (non-first) boot.
            first_run = False
            _check_root(repo, kek)
        if (
            first_run
            and escrow is not None
            and _restore_or_refuse(
                repo,
                kek,
                escrow,
                permitted=restore_allowed,
                now=moment,
                refusal_if_empty=None,
            )
        ):
            first_run = False  # restored keys are in use: never import over them
        generated: list[str] = []
        imported: list[str] = []
        rejected: list[str] = []
        for spec in REGISTRY.values():
            if spec.scope_rule is not ScopeRule.GLOBAL:
                continue
            action, rejection = _ensure_version_one(
                repo, kek, spec, first_run=first_run, environ=env, now=moment
            )
            if action in ("generated",):
                generated.append(spec.name)
            elif action == "imported":
                imported.append(spec.name)
            if rejection:
                rejected.append(spec.name)
        entries: list[CachedSecret] = []
        for row in repo.openable_rows():
            try:
                spec = REGISTRY[SecretType(row.secret_type)]
            except (ValueError, KeyError):
                logger.warning("platform vault holds a row of an unregistered type; skipped")
                continue
            entries.append(_open_row(spec, row, kek))
        if generated or imported:
            repo.notify_changed()
        return BootstrapReport(
            kill_switched=False,
            first_run=first_run,
            generated=tuple(generated),
            imported=tuple(imported),
            rejected_imports=tuple(rejected),
            events_examined=chain.examined,
            entries=tuple(entries),
        )


_process_cache = PlatformSecretCache()


def get_process_cache() -> PlatformSecretCache:
    """The hub-api process-wide cache (seeded by the bootstrap; consumers arrive in later PRs)."""
    return _process_cache


def run_platform_secrets_bootstrap(
    engine: Engine,
    *,
    environ: Mapping[str, str] | None = None,
    kill_switch: Callable[[], bool] = db_platform_keys_disabled,
    cache: PlatformSecretCache | None = None,
    kek: PlatformKekAdapter | None = None,
    escrow: EscrowStore | None = None,
) -> BootstrapReport:
    """Entry point from hub-api's bootstrap, inside its advisory lock: one transaction, then seed.

    Kill-switch ON returns immediately without touching the database. A missing / malformed KEK
    refuses the boot (`kek_missing`) rather than skipping: an absent root must be loud. On success
    the process cache is seeded only AFTER the commit, so it never holds a value the database
    does not. With the S3 escrow configured (`PLATFORM_ESCROW_S3_*`) an empty store is restored
    from it when permitted (alpha/local, or `WADDLES_PLATFORM_SECRETS_ESCROW_RESTORE=1`), and after
    the commit a second short transaction escrows any version the escrow lacks (best effort: an
    unreachable store is a metric and a retry on the next boot, never a failed boot).
    """
    if kill_switch():
        logger.warning("platform vault bootstrap skipped: %s is ON", KILL_SWITCH_FLAG)
        telemetry.count_generated("all", "skipped")
        return BootstrapReport(kill_switched=True)
    try:
        adapter = kek if kek is not None else PlatformKekAdapter.from_env(environ)
    except PlatformKekError:
        raise _refuse("kek_missing") from None
    try:
        store = escrow if escrow is not None else escrow_store_from_env(environ)
    except EscrowMisconfiguredError:
        raise _refuse("escrow_misconfigured") from None
    with engine.begin() as conn:
        report = bootstrap_platform_secrets(
            SqlRepository(conn),
            adapter,
            environ=environ,
            escrow=store,
            restore_allowed=restore_permitted(environ),
        )
    if store is not None:
        _sync_escrow_best_effort(engine, adapter, store)
    (cache if cache is not None else _process_cache).seed(report.entries)
    telemetry.register_cache_gauges((cache or _process_cache).gauge_snapshot)
    return report


def _sync_escrow_best_effort(engine: Engine, kek: PlatformKekAdapter, store: EscrowStore) -> None:
    """Escrow new versions in their own transaction; any failure is counted and never fatal.

    Holds the caller's advisory lock (an `escrowed` event extends the audit chain). Only the
    exception CLASS is logged: driver / botocore messages can echo parameters.
    """
    try:
        with engine.begin() as conn:
            report = sync_escrow(SqlRepository(conn), kek, store)
    except (EscrowError, PlatformSecretError, SQLAlchemyError) as exc:
        telemetry.count_escrow("all", "error")
        logger.error("platform secret escrow sync failed (%s)", type(exc).__name__)
        return
    logger.info(
        "platform secret escrow sync status=%s examined=%d written=%d drift=%d",
        report.status,
        report.examined,
        report.written,
        report.drift,
    )


def start_cache_refresher(
    database_url: str,
    *,
    kill_switch: Callable[[], bool] = db_platform_keys_disabled,
    cache: PlatformSecretCache | None = None,
) -> CacheRefresher | None:
    """Start the NOTIFY + 60 s refresher over the process cache once the bootstrap is READY.

    Returns None when the kill-switch is ON. The loader gets its own small engine (the bootstrap
    engine is disposed once READY) with parameters hidden from driver errors.
    """
    import sqlalchemy

    if kill_switch():
        return None
    target = cache if cache is not None else _process_cache
    sync_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = sqlalchemy.create_engine(
        sync_url, pool_pre_ping=True, pool_size=1, max_overflow=0, hide_parameters=True
    )
    target._loader = make_loader(engine, PlatformKekAdapter.from_env())  # noqa: SLF001
    refresher = CacheRefresher(target, PgNotifyWaiter(database_url).wait)
    refresher.start()
    return refresher
