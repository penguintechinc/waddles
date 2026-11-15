"""S3 escrow of the platform secrets: sealed DR copies, a signed manifest, guarded restore (T7).

Postgres is the source of truth; the escrow is the disaster-recovery copy that lets a wiped DB come
back with the SAME keys (same fingerprints, so tokens and bundle signatures stay valid). Layout in
the dedicated escrow bucket:

  platform/secrets-escrow/<type>/<scope|global>/<version>.sealed   one object per version
  platform/secrets-escrow/manifest.json                            signed list of those versions

Trust model (security review H5). The bucket is reachable only by a dedicated hub-api escrow S3
identity (never a bundle identity and never the general hub-api one), so a stolen bundle/data-plane
credential cannot write here -- but this module does not rely on that:

  * every object is AES-GCM sealed under a KEK-derived key (HKDF label `escrow/v1`) with an AAD
    binding type | scope | version | kek_id | public_id | fingerprint (`vault.escrow_aad`), so a
    swapped, re-labelled, wrong-version or wrong-KEK object fails authentication;
  * the manifest is MAC'd under a KEK-derived key (label `escrow-manifest/v1`) and lists, per
    version, the identity + keyed fingerprint to expect; restore verifies the signature FIRST,
    then each object against its entry, then re-derives the fingerprint from the opened value;
  * restore picks only manifest-listed versions, the highest one active; an unlisted object, an
    older-version object, a tampered or unsigned manifest, or an object sealed under another KEK
    refuses the whole restore (nothing is half-restored: the caller's transaction rolls back);
  * restore is never implicit outside alpha/local: it needs the explicit operator flag
    `WADDLES_PLATFORM_SECRETS_ESCROW_RESTORE=1`.

Writing is idempotent and never destructive: an object for a version that is already escrowed is
left alone; a remote manifest that does not verify, or that is AHEAD of the database (a rolled-back
DB), is reported as drift and never overwritten -- the bucket is versioned so history survives, and
an operator decides. All crypto is in `services.provider_connections.vault` (seal-callers guard).
Values, fingerprints, object bytes and exception messages never reach a log line, label or span:
only registry type names, ints and fixed outcome words.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from services.envelope.platform_kek import PlatformKekAdapter
from services.platform_secrets import telemetry
from services.platform_secrets.event_chain import append_event
from services.platform_secrets.registry import (
    REGISTRY,
    ScopeRule,
    SecretType,
    UnknownSecretTypeError,
    get_spec,
)
from services.platform_secrets.repository import (
    EVENT_DESTROYED,
    EVENT_ESCROWED,
    EVENT_RESTORED,
    EVENT_RETIRED,
    OUTCOME_SUCCESS,
    REASON_RESTORED,
    STATE_ACTIVE,
    STATE_VERIFY_ONLY,
    AnchorRow,
    NewSecret,
    PlatformVaultRepository,
    SecretRow,
)
from services.provider_connections.vault import (
    PlatformSecretError,
    constant_time_equal,
    open_escrow_object,
    open_platform_secret,
    platform_key_material_from_private,
    platform_secret_fingerprint,
    seal_escrow_object,
    seal_platform_secret,
    sign_escrow_anchor,
    sign_escrow_manifest,
    verify_escrow_anchor,
    verify_escrow_manifest,
)

logger = logging.getLogger(__name__)

ESCROW_PREFIX: Final = "platform/secrets-escrow/"
MANIFEST_KEY: Final = ESCROW_PREFIX + "manifest.json"
MANIFEST_VERSION: Final = 1
#: Write-once per-generation copies of every manifest (`manifest.json` is only the latest pointer).
GENERATION_PREFIX: Final = ESCROW_PREFIX + "manifests/"
#: Deployment tiers where a restore needs no operator flag (alpha is disposable by owner decision).
AUTO_RESTORE_TIERS: Final = frozenset({"alpha", "local"})
RESTORE_FLAG_ENV: Final = "WADDLES_PLATFORM_SECRETS_ESCROW_RESTORE"
_MAX_GENERATION_PROBES: Final = 8
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


class EscrowError(Exception):
    """Base of escrow failures. Messages are fixed words: never a value, key or object content."""


class EscrowUnavailableError(EscrowError):
    """The escrow store could not be reached (network, credentials, 5xx)."""


class EscrowMisconfiguredError(EscrowError):
    """The escrow is partly configured (bucket without credentials): refuse rather than skip."""


class EscrowRefusedError(EscrowError):
    """A restore was refused; `reason` is `escrow_invalid`, `escrow_tampered`, `escrow_missing`."""

    def __init__(self, reason: str) -> None:
        """Keep the reason code; the message carries nothing else."""
        super().__init__(f"platform secret escrow restore refused: {reason}")
        self.reason = reason


class EscrowStore(Protocol):
    """The two operations the escrow needs from an object store (S3 class / in-memory fake)."""

    def put(self, key: str, data: bytes) -> None:
        """Write one object (overwrites; the bucket is versioned)."""

    def get(self, key: str) -> bytes | None:
        """Read one object; None when it does not exist."""


class S3EscrowStore:
    """:class:`EscrowStore` over a dedicated bucket and a dedicated escrow S3 identity.

    Bucket default SSE (set by the bucket-init hook) applies on top of the object-level AEAD seal.
    Only the object key is ever passed on; error text from botocore is dropped (class name only).
    """

    def __init__(self, client: Any, bucket: str) -> None:
        """Hold the boto3 client and the bucket name."""
        self._client = client
        self._bucket = bucket

    def put(self, key: str, data: bytes) -> None:
        """PUT one object; any transport/service failure becomes :class:`EscrowUnavailableError`."""
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType="application/octet-stream"
            )
        except (BotoCoreError, ClientError):
            raise EscrowUnavailableError("escrow store write failed") from None

    def get(self, key: str) -> bytes | None:
        """GET one object (None for NoSuchKey/404)."""
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise EscrowUnavailableError("escrow store read failed") from None
        except BotoCoreError:
            raise EscrowUnavailableError("escrow store read failed") from None
        return bytes(body)


def escrow_store_from_env(environ: Mapping[str, str] | None = None) -> EscrowStore | None:
    """Build the S3 escrow store from `PLATFORM_ESCROW_S3_*`, or None when escrow is not configured.

    Unconfigured = no bucket name set (local dev without object storage). A bucket without BOTH
    credentials is a misconfiguration and raises -- a half-configured escrow must be loud, not a
    silent skip. The endpoint falls back to the chart's `S3_ENDPOINT_URL`; the CA bundle is the
    chart's `AWS_CA_BUNDLE` (read by botocore itself).
    """
    env = os.environ if environ is None else environ
    bucket = env.get("PLATFORM_ESCROW_S3_BUCKET", "").strip()
    if not bucket:
        return None
    access = env.get("PLATFORM_ESCROW_S3_ACCESS_KEY_ID", "").strip()
    secret = env.get("PLATFORM_ESCROW_S3_SECRET_ACCESS_KEY", "").strip()
    if not access or not secret:
        raise EscrowMisconfiguredError("escrow bucket is set but its S3 credentials are not")
    client = boto3.client(
        "s3",
        endpoint_url=env.get("PLATFORM_ESCROW_S3_ENDPOINT_URL")
        or env.get("S3_ENDPOINT_URL")
        or "http://infra-seaweedfs:8333",
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name=env.get("PLATFORM_ESCROW_S3_REGION", "us-east-1"),
        config=BotoConfig(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
    return S3EscrowStore(client, bucket)


def restore_permitted(environ: Mapping[str, str] | None = None) -> bool:
    """True when a restore may run: alpha/local tier, or the explicit operator flag is set."""
    env = os.environ if environ is None else environ
    tier = env.get("DEPLOYMENT_TIER", env.get("WADDLES_DEPLOYMENT_TIER", "")).strip().lower()
    flag = env.get(RESTORE_FLAG_ENV, "").strip().lower() in _TRUTHY
    return flag or tier in AUTO_RESTORE_TIERS


def object_key(secret_type: int, scope_id: int | None, version: int) -> str:
    """The escrow object key of one version: `<prefix><type>/<scope|global>/<version>.sealed`."""
    scope = "global" if scope_id is None else str(scope_id)
    return f"{ESCROW_PREFIX}{int(secret_type)}/{scope}/{int(version)}.sealed"


@dataclass(slots=True, frozen=True)
class EscrowEntry:
    """One manifest line: the identity and expected keyed fingerprint of an escrowed version."""

    secret_type: int
    scope_id: int | None
    version: int
    public_id: uuid.UUID
    kek_id: str
    fingerprint: bytes = field(repr=False)
    algorithm: int
    state: int
    activated_at: datetime
    rotated_at: datetime | None
    key: str
    kid: str | None = None

    def identity(self) -> tuple[int, int | None, int]:
        """`(type, scope, version)`."""
        return (self.secret_type, self.scope_id, self.version)

    def to_json(self) -> dict[str, Any]:
        """Canonical-JSON-able dict (ints, strings, nulls only)."""
        return {
            "type": self.secret_type,
            "scope": self.scope_id,
            "version": self.version,
            "public_id": str(self.public_id),
            "kek_id": self.kek_id,
            "fingerprint": self.fingerprint.hex(),
            "algorithm": self.algorithm,
            "state": self.state,
            "activated_at": _iso(self.activated_at),
            "rotated_at": None if self.rotated_at is None else _iso(self.rotated_at),
            "key": self.key,
            "kid": self.kid,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> EscrowEntry:
        """Parse one manifest line, strictly: a wrong shape or type is `escrow_invalid`."""
        try:
            scope = raw["scope"]
            rotated = raw["rotated_at"]
            entry = cls(
                secret_type=_strict_int(raw["type"]),
                scope_id=None if scope is None else _strict_int(scope),
                version=_strict_int(raw["version"]),
                public_id=uuid.UUID(str(raw["public_id"])),
                kek_id=str(raw["kek_id"]),
                fingerprint=bytes.fromhex(str(raw["fingerprint"])),
                algorithm=_strict_int(raw["algorithm"]),
                state=_strict_int(raw["state"]),
                activated_at=datetime.fromisoformat(str(raw["activated_at"])),
                rotated_at=None if rotated is None else datetime.fromisoformat(str(rotated)),
                key=str(raw["key"]),
                kid=None if raw.get("kid") is None else str(raw["kid"]),
            )
        except (KeyError, TypeError, ValueError):
            raise EscrowRefusedError("escrow_invalid") from None
        if len(entry.fingerprint) != 32 or entry.state not in (STATE_ACTIVE, STATE_VERIFY_ONLY):
            raise EscrowRefusedError("escrow_invalid")
        return entry


@dataclass(slots=True, frozen=True)
class EscrowManifest:
    """A verified manifest: its generation counter and the entries it lists."""

    generation: int
    entries: tuple[EscrowEntry, ...]
    digest: bytes = field(repr=False)


@dataclass(slots=True, frozen=True)
class EscrowSyncReport:
    """What one sync did. Counts only."""

    status: str  # `ok`, `unavailable`, `blocked_tampered`, `blocked_ahead`, `blocked_drift`
    examined: int = 0
    written: int = 0
    drift: int = 0


@dataclass(slots=True, frozen=True)
class EscrowRestoreReport:
    """What one restore did. Counts only."""

    restored: int
    types: tuple[str, ...] = ()


def _strict_int(value: Any) -> int:
    """An int that is not a bool (JSON `true` must not parse as 1)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("not an int")
    return int(value)


def _iso(moment: datetime) -> str:
    """UTC microsecond ISO form (round-trips exactly through Postgres and JSON)."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _canonical(body: Mapping[str, Any]) -> bytes:
    """Canonical manifest bytes the MAC covers: key-sorted compact JSON."""
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _build_document(
    entries: list[EscrowEntry], generation: int, kek: PlatformKekAdapter, now: datetime
) -> tuple[bytes, bytes]:
    """Serialize + sign a manifest as ONE object `{"manifest","kek_id","mac"}`; with its digest."""
    kek_id = kek.kek_ids[0]
    body = {
        "v": MANIFEST_VERSION,
        "generation": generation,
        "kek_id": kek_id,
        "written_at": _iso(now),
        "entries": [e.to_json() for e in sorted(entries, key=lambda e: _sortable(e.identity()))],
    }
    mac = sign_escrow_manifest(_canonical(body), kek=kek, kek_id=kek_id)
    document = json.dumps({"manifest": body, "kek_id": kek_id, "mac": mac.hex()}).encode()
    return document, hashlib.sha256(_canonical(body)).digest()


def _sortable(identity: tuple[int, int | None, int]) -> tuple[int, int, int]:
    """Sort key for a `(type, scope|None, version)` identity (global first)."""
    return (identity[0], -1 if identity[1] is None else identity[1], identity[2])


def parse_manifest(document: bytes, kek: PlatformKekAdapter) -> EscrowManifest:
    """Verify the signature, THEN parse; any defect raises :class:`EscrowRefusedError`.

    `escrow_tampered`: the MAC does not verify, or no loaded KEK can verify it (signed by a KEK
    that is gone -- the wrong-root case). `escrow_invalid`: validly signed but malformed.
    """
    try:
        doc = json.loads(document)
        body = doc["manifest"]
        kek_id = str(doc["kek_id"])
        mac = bytes.fromhex(str(doc["mac"]))
        if not isinstance(body, dict) or body.get("kek_id") != kek_id:
            raise ValueError("shape")
    except (KeyError, TypeError, ValueError):
        raise EscrowRefusedError("escrow_tampered") from None
    try:
        verified = verify_escrow_manifest(_canonical(body), mac, kek=kek, kek_id=kek_id)
    except PlatformSecretError:
        raise EscrowRefusedError("escrow_tampered") from None
    if not verified:
        raise EscrowRefusedError("escrow_tampered")
    try:
        if body["v"] != MANIFEST_VERSION:
            raise ValueError("version")
        generation = _strict_int(body["generation"])
        raw_entries = body["entries"]
        if not isinstance(raw_entries, list) or generation < 1:
            raise ValueError("entries")
    except (KeyError, TypeError, ValueError):
        raise EscrowRefusedError("escrow_invalid") from None
    entries = tuple(EscrowEntry.from_json(e) for e in raw_entries)
    if len({e.identity() for e in entries}) != len(entries):
        raise EscrowRefusedError("escrow_invalid")
    return EscrowManifest(
        generation=generation,
        entries=entries,
        digest=hashlib.sha256(_canonical(body)).digest(),
    )


def load_manifest(store: EscrowStore, kek: PlatformKekAdapter) -> EscrowManifest | None:
    """Fetch and verify the manifest. None when no manifest object exists.

    Raises :class:`EscrowUnavailableError` when the store cannot be read and
    :class:`EscrowRefusedError` when the document does not verify.
    """
    document = store.get(MANIFEST_KEY)
    return None if document is None else parse_manifest(document, kek)


def generation_key(generation: int) -> str:
    """Object key of the write-once copy of manifest `generation`."""
    return f"{GENERATION_PREFIX}{int(generation)}.json"


def verified_anchor(repo: PlatformVaultRepository, kek: PlatformKekAdapter) -> AnchorRow | None:
    """The freshness anchor from `root_marker`, MAC-verified; None when none was ever written.

    A MAC that does not verify (or a KEK that cannot check it) is `escrow_tampered`: the anchor is
    the one thing that makes an old signed manifest detectable, so it is never trusted blindly.
    """
    anchor = repo.read_anchor()
    if anchor is None:
        return None
    try:
        ok = verify_escrow_anchor(
            anchor.generation,
            anchor.manifest_digest,
            anchor.mac,
            kek=kek,
            kek_id=anchor.kek_id,
        )
    except PlatformSecretError:
        raise EscrowRefusedError("escrow_tampered") from None
    if not ok:
        raise EscrowRefusedError("escrow_tampered")
    return anchor


def _fresh(manifest: EscrowManifest, anchor: AnchorRow | None) -> bool:
    """True when `manifest` is at least as new as the anchor (equal generation: same digest)."""
    if anchor is None or manifest.generation > anchor.generation:
        return True
    return manifest.generation == anchor.generation and constant_time_equal(
        manifest.digest, anchor.manifest_digest
    )


def select_manifest(
    store: EscrowStore, kek: PlatformKekAdapter, anchor: AnchorRow | None
) -> EscrowManifest | None:
    """Pick the manifest a restore may use: the latest one, if it is not older than the anchor.

    None when the escrow holds no manifest at all. When the latest pointer is stale (an attacker
    replayed an older signed manifest) the write-once object of the ANCHORED generation is used
    instead, if it exists and matches the anchor digest; otherwise the restore is refused
    (`escrow_rollback` for an older generation, `escrow_tampered` for the same generation with
    another digest).
    """
    document = store.get(MANIFEST_KEY)
    latest: EscrowManifest | None = None
    parse_error: EscrowRefusedError | None = None
    if document is not None:
        try:
            latest = parse_manifest(document, kek)
        except EscrowRefusedError as exc:
            parse_error = exc
    if latest is not None and _fresh(latest, anchor):
        return latest
    if anchor is not None:
        anchored = store.get(generation_key(anchor.generation))
        if anchored is not None:
            try:
                candidate = parse_manifest(anchored, kek)
            except EscrowRefusedError:
                candidate = None
            if candidate is not None and _fresh(candidate, anchor):
                logger.warning("platform secret escrow: latest manifest is stale; using anchored")
                return candidate
    if document is None:
        return None
    if parse_error is not None:
        raise parse_error
    assert latest is not None and anchor is not None  # noqa: S101 -- narrowed by _fresh above
    raise EscrowRefusedError(
        "escrow_rollback" if latest.generation < anchor.generation else "escrow_tampered"
    )


def _retired_identities(repo: PlatformVaultRepository) -> set[tuple[int, int | None, int]]:
    """Versions the audit chain records as retired or destroyed (never restorable)."""
    return {
        (e.secret_type, e.scope_id, e.version)
        for e in repo.events()
        if e.event_type in (EVENT_RETIRED, EVENT_DESTROYED)
        and e.outcome == OUTCOME_SUCCESS
        and e.version is not None
    }


def _type_name(secret_type: int) -> str:
    """Registry name of a type code for telemetry; `unknown` otherwise (never a raw value)."""
    try:
        return get_spec(secret_type).name
    except UnknownSecretTypeError:
        return "unknown"


def _open_row(row: SecretRow, kek: PlatformKekAdapter) -> bytes | None:
    """Open a stored row's value, or None when it will not open (bootstrap already vetted it)."""
    if row.sealed_value is None:
        return None
    try:
        return open_platform_secret(
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
        return None


def _entry_matches(
    entry: EscrowEntry, row: SecretRow, plaintext: bytes, kek: PlatformKekAdapter
) -> bool:
    """True when a manifest entry escrows exactly this row and value (keyed fingerprint)."""
    if entry.public_id != row.public_id:
        return False
    try:
        expected = platform_secret_fingerprint(plaintext, kek=kek, kek_id=entry.kek_id)
    except PlatformSecretError:
        return False
    return constant_time_equal(expected, entry.fingerprint)


def sync_escrow(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    store: EscrowStore,
    *,
    now: datetime | None = None,
) -> EscrowSyncReport:
    """Make the escrow reflect the database: seal missing versions, then publish a signed manifest.

    Call it after any commit that created or changed a platform secret (bootstrap, rotate, retire)
    while holding the bootstrap advisory lock (an `escrowed` event extends the audit chain). Never
    raises for a store problem: the outcome is a metric and a report status, and the next call
    retries. It refuses to overwrite a remote manifest that does not verify (tamper) or that lists a
    version the database has never heard of (the database was rolled back) -- both are drift alerts.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    with telemetry.span("escrow"):
        try:
            remote = load_manifest(store, kek)
        except EscrowRefusedError as exc:
            _alert_all("escrow_tampered" if exc.reason == "escrow_tampered" else "escrow_invalid")
            logger.error("platform secret escrow manifest does not verify; not overwriting")
            return EscrowSyncReport("blocked_tampered", drift=1)
        except EscrowUnavailableError:
            _alert_all("unavailable", counter="escrow")
            logger.warning("platform secret escrow store unreachable; retry on next sync")
            return EscrowSyncReport("unavailable")
        try:
            anchor = verified_anchor(repo, kek)
        except EscrowRefusedError:
            _alert_all("escrow_tampered")
            logger.error("platform secret escrow anchor does not verify; not writing")
            return EscrowSyncReport("blocked_tampered", drift=1)
        healing = False
        if remote is not None and not _fresh(remote, anchor):
            # The bucket's manifest is OLDER than what we last wrote (replay / rollback) or the
            # same generation with another digest: alert, ignore it, and re-publish from the DB.
            assert anchor is not None  # noqa: S101 -- _fresh is True whenever anchor is None
            _alert_all(
                "escrow_rollback" if remote.generation < anchor.generation else "escrow_tampered"
            )
            logger.error("platform secret escrow manifest is stale or altered; re-publishing")
            remote, healing = None, True
        rows = [r for r in repo.openable_rows() if r.state in (STATE_ACTIVE, STATE_VERIFY_ONLY)]
        db_identities = {(r.secret_type, r.scope_id, r.version) for r in rows}
        known = {e.identity(): e for e in (remote.entries if remote else ())}
        if any(ident not in db_identities and _db_has_lower_active(rows, ident) for ident in known):
            _alert_all("escrow_ahead")
            logger.error("platform secret escrow is ahead of the database; not overwriting")
            return EscrowSyncReport("blocked_ahead", examined=len(rows), drift=1)
        entries: list[EscrowEntry] = []
        written = 0
        drift = 0
        for row in rows:
            plaintext = _open_row(row, kek)
            if plaintext is None:
                continue
            name = _type_name(row.secret_type)
            entry = known.get((row.secret_type, row.scope_id, row.version))
            if entry is not None:
                if not _entry_matches(entry, row, plaintext, kek):
                    drift += 1
                    telemetry.count_drift(name, "escrow_drift")
                    logger.error(
                        "platform secret escrow drift type=%s version=%d", name, row.version
                    )
                    entries.append(entry)  # keep what the remote already vouched for
                    continue
                if store.get(entry.key) is not None:
                    entries.append(_refreshed(entry, row))
                    continue
            try:
                fp = platform_secret_fingerprint(plaintext, kek=kek)
                blob, kek_id = seal_escrow_object(
                    plaintext,
                    secret_type=row.secret_type,
                    scope_id=row.scope_id,
                    version=row.version,
                    public_id=row.public_id,
                    fingerprint=fp,
                    kek=kek,
                )
                key = object_key(row.secret_type, row.scope_id, row.version)
                store.put(key, blob)
            except EscrowUnavailableError:
                telemetry.count_escrow(name, "error")
                logger.warning(
                    "platform secret escrow write failed type=%s version=%d", name, row.version
                )
                return EscrowSyncReport(
                    "unavailable", examined=len(rows), written=written, drift=drift
                )
            except PlatformSecretError:
                telemetry.count_escrow(name, "error")
                continue
            entries.append(
                EscrowEntry(
                    secret_type=row.secret_type,
                    scope_id=row.scope_id,
                    version=row.version,
                    public_id=row.public_id,
                    kek_id=kek_id,
                    fingerprint=fp,
                    algorithm=row.algorithm,
                    state=row.state,
                    activated_at=row.activated_at or moment,
                    rotated_at=row.rotated_at,
                    key=key,
                    kid=row.kid,
                )
            )
            written += 1
            telemetry.count_escrow(name, "ok")
            append_event(
                repo,
                kek,
                secret_id=row.id,
                secret_type=row.secret_type,
                scope_id=row.scope_id,
                version=row.version,
                event_type=EVENT_ESCROWED,
                outcome=OUTCOME_SUCCESS,
                reason=None,
                now=moment,
            )
        previous = {e.identity(): e for e in (remote.entries if remote else ())}
        if healing or remote is None or written or {e.identity(): e for e in entries} != previous:
            generation = max(remote.generation if remote else 0, anchor.generation if anchor else 0)
            try:
                generation += 1
                for _ in range(_MAX_GENERATION_PROBES):  # write-once: never reuse a taken key
                    if store.get(generation_key(generation)) is None:
                        break
                    generation += 1
                document, digest = _build_document(entries, generation, kek, moment)
                kek_id = kek.kek_ids[0]
                # Anchor FIRST: if a later write fails the anchor is ahead of the bucket, which only
                # ever refuses a stale restore loudly -- it can never let an old manifest through.
                if not repo.advance_anchor(
                    generation,
                    digest,
                    kek_id,
                    sign_escrow_anchor(generation, digest, kek=kek, kek_id=kek_id),
                ):
                    raise EscrowUnavailableError("anchor did not advance")
                store.put(generation_key(generation), document)
                store.put(MANIFEST_KEY, document)
            except EscrowUnavailableError:
                telemetry.count_escrow("all", "error")
                logger.warning("platform secret escrow manifest write failed")
                return EscrowSyncReport(
                    "unavailable", examined=len(rows), written=written, drift=drift
                )
        return EscrowSyncReport(
            "blocked_drift" if drift else "ok", examined=len(rows), written=written, drift=drift
        )


def _db_has_lower_active(rows: list[SecretRow], ident: tuple[int, int | None, int]) -> bool:
    """True when the DB holds this (type, scope) but only at versions BELOW the escrowed `ident`.

    A manifest listing a higher version than the database has means the database went backwards
    (restored from an old snapshot); a manifest listing a version for a (type, scope) the database
    has no row for at all is also ahead (a wiped table that the bootstrap should have refused).
    """
    versions = [r.version for r in rows if (r.secret_type, r.scope_id) == (ident[0], ident[1])]
    return not versions or max(versions) < ident[2]


def _refreshed(entry: EscrowEntry, row: SecretRow) -> EscrowEntry:
    """The entry with its mutable columns (state, rotated_at) brought up to the row's."""
    return EscrowEntry(
        secret_type=entry.secret_type,
        scope_id=entry.scope_id,
        version=entry.version,
        public_id=entry.public_id,
        kek_id=entry.kek_id,
        fingerprint=entry.fingerprint,
        algorithm=entry.algorithm,
        state=row.state,
        activated_at=entry.activated_at,
        rotated_at=row.rotated_at,
        key=entry.key,
        kid=entry.kid,
    )


def _alert_all(outcome: str, *, counter: str = "drift") -> None:
    """Count a store-wide condition once per registry type (drift counter or escrow counter)."""
    for spec in REGISTRY.values():
        if counter == "drift":
            telemetry.count_drift(spec.name, outcome)
        else:
            telemetry.count_escrow(spec.name, outcome)


def _validate_manifest_for_restore(
    manifest: EscrowManifest,
    retired: set[tuple[int, int | None, int]],
) -> dict[tuple[int, int | None], list[EscrowEntry]]:
    """Group by (type, scope) ascending; enforce the registry and 'highest version is the active'.

    Every refusal here is `escrow_invalid`: an entry for an unregistered type, a scope the type's
    rule forbids, a wrong algorithm, a key that is not the canonical object key, versions that do
    not end in exactly one ACTIVE (the highest) with every lower one verify-only.
    """
    groups: dict[tuple[int, int | None], list[EscrowEntry]] = {}
    for entry in manifest.entries:
        if entry.identity() in retired:
            raise EscrowRefusedError("escrow_rollback")
        try:
            spec = REGISTRY[SecretType(entry.secret_type)]
        except (ValueError, KeyError):
            raise EscrowRefusedError("escrow_invalid") from None
        if (spec.scope_rule is ScopeRule.GLOBAL) != (entry.scope_id is None):
            raise EscrowRefusedError("escrow_invalid")
        if entry.algorithm != int(spec.algorithm) or entry.version < 1:
            raise EscrowRefusedError("escrow_invalid")
        if entry.key != object_key(entry.secret_type, entry.scope_id, entry.version):
            raise EscrowRefusedError("escrow_invalid")
        groups.setdefault((entry.secret_type, entry.scope_id), []).append(entry)
    for entries in groups.values():
        entries.sort(key=lambda e: e.version)
        *lower, top = entries
        if top.state != STATE_ACTIVE or any(e.state != STATE_VERIFY_ONLY for e in lower):
            raise EscrowRefusedError("escrow_invalid")
    return groups


def restore_from_escrow(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    store: EscrowStore,
    *,
    now: datetime | None = None,
) -> EscrowRestoreReport | None:
    """Rebuild an EMPTY `secrets` table from the escrow; None when the escrow holds no manifest.

    Order matters and nothing is written until everything has verified: manifest signature, entry
    shape against the registry, then each object opened under its manifest-bound AAD and its
    fingerprint recomputed from the opened value. Only then are rows inserted (ascending version,
    older ones demoted to verify-only before the next goes active) and `restored` events appended.
    Raises :class:`EscrowRefusedError` (the caller's transaction rolls back, so a refused restore
    leaves the table empty) and :class:`EscrowUnavailableError` (store unreachable).
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    anchor = verified_anchor(repo, kek)
    manifest = select_manifest(store, kek, anchor)
    if manifest is None:
        return None
    groups = _validate_manifest_for_restore(manifest, _retired_identities(repo))
    opened: dict[tuple[int, int | None, int], bytes] = {}
    for entries in groups.values():
        for entry in entries:
            blob = store.get(entry.key)
            if blob is None:
                raise EscrowRefusedError("escrow_missing")
            try:
                plaintext = open_escrow_object(
                    blob,
                    secret_type=entry.secret_type,
                    scope_id=entry.scope_id,
                    version=entry.version,
                    public_id=entry.public_id,
                    fingerprint=entry.fingerprint,
                    kek_id=entry.kek_id,
                    kek=kek,
                )
                recomputed = platform_secret_fingerprint(plaintext, kek=kek, kek_id=entry.kek_id)
            except PlatformSecretError:
                raise EscrowRefusedError("escrow_invalid") from None
            if not constant_time_equal(recomputed, entry.fingerprint):
                raise EscrowRefusedError("escrow_invalid")
            opened[entry.identity()] = plaintext
    restored = 0
    names: list[str] = []
    for entries in groups.values():
        last = len(entries) - 1
        for index, entry in enumerate(entries):
            plaintext = opened[entry.identity()]
            _insert_restored(repo, kek, entry, plaintext, moment)
            if index != last:
                _demote(repo, kek, entry, moment)
            restored += 1
        names.append(_type_name(entries[0].secret_type))
    if anchor is None or manifest.generation > anchor.generation:
        kek_id = kek.kek_ids[0]
        repo.advance_anchor(
            manifest.generation,
            manifest.digest,
            kek_id,
            sign_escrow_anchor(manifest.generation, manifest.digest, kek=kek, kek_id=kek_id),
        )
    for name in names:
        telemetry.count_escrow(name, "restored")
    return EscrowRestoreReport(restored=restored, types=tuple(sorted(set(names))))


def _insert_restored(
    repo: PlatformVaultRepository,
    kek: PlatformKekAdapter,
    entry: EscrowEntry,
    plaintext: bytes,
    now: datetime,
) -> None:
    """Re-seal one restored value under the CURRENT KEK and insert it ACTIVE (same public_id)."""
    spec = REGISTRY[SecretType(entry.secret_type)]
    sealed = seal_platform_secret(
        plaintext,
        secret_type=entry.secret_type,
        scope_id=entry.scope_id,
        version=entry.version,
        public_id=entry.public_id,
        kek=kek,
    )
    material = (
        platform_key_material_from_private(int(spec.algorithm), plaintext, kid=entry.kid)
        if spec.is_asymmetric
        else None
    )
    row = repo.insert_secret(
        NewSecret(
            public_id=entry.public_id,
            secret_type=entry.secret_type,
            scope_id=entry.scope_id,
            version=entry.version,
            algorithm=int(spec.algorithm),
            sealed_value=sealed.sealed_value,
            kek_id=sealed.kek_id,
            seal_version=sealed.seal_version,
            kid=None if material is None else material.kid,
            public_material=None if material is None else material.public_material,
            fingerprint=platform_secret_fingerprint(plaintext, kek=kek, kek_id=sealed.kek_id),
            reason=REASON_RESTORED,
            activated_at=entry.activated_at,
        )
    )
    if row is None:
        raise EscrowRefusedError("escrow_invalid")
    append_event(
        repo,
        kek,
        secret_id=row.id,
        secret_type=row.secret_type,
        scope_id=row.scope_id,
        version=row.version,
        event_type=EVENT_RESTORED,
        outcome=OUTCOME_SUCCESS,
        reason=REASON_RESTORED,
        now=now,
    )


def _demote(
    repo: PlatformVaultRepository, kek: PlatformKekAdapter, entry: EscrowEntry, now: datetime
) -> None:
    """Move a just-restored lower version to verify-only so the next version can go active."""
    rows = repo.rows_for(entry.secret_type, entry.scope_id)
    row = next((r for r in rows if r.version == entry.version), None)
    if row is None or not repo.update_state(
        row.id,
        STATE_VERIFY_ONLY,
        at=entry.rotated_at or now,
        reason=REASON_RESTORED,
        actor_uuid=None,
    ):
        raise EscrowRefusedError("escrow_invalid")
