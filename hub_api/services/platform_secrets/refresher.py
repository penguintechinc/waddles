"""Keeps the cache fresh: a DB loader plus a thread waking on NOTIFY or every 60 seconds.

`make_loader` re-reads every active / verify-only row and opens it (any row that will not open
fails the whole load, so the previous last-known content stays); `CacheRefresher` calls
`cache.refresh()` when `platform_secrets_changed` fires or the 60 s poll elapses, whichever is
first. The waiting primitive is injected: `PgNotifyWaiter` is the real LISTEN connection, tests pass
a fake. A failed refresh is counted and logged by exception class only, never fatal, never silent.
"""

from __future__ import annotations

import logging
import select
import threading
import time
from collections.abc import Callable
from typing import Any, Final

from sqlalchemy.engine import Engine

from services.envelope.platform_kek import PlatformKekAdapter
from services.platform_secrets import telemetry
from services.platform_secrets.cache import (
    CachedSecret,
    Loader,
    PlatformSecretCache,
    verify_until_for,
)
from services.platform_secrets.registry import REGISTRY, SecretType
from services.platform_secrets.repository import NOTIFY_CHANNEL, SqlRepository
from services.provider_connections.vault import PlatformSecretError, open_platform_secret

logger = logging.getLogger(__name__)

#: Poll period when no NOTIFY arrives (seconds).
POLL_SECONDS: Final = 60.0


class CacheLoadError(RuntimeError):
    """A stored row could not be opened during a refresh (cache keeps its last-known)."""


def make_loader(engine: Engine, kek: PlatformKekAdapter) -> Loader:
    """Build the cache loader: read servable rows on a short connection and open each one."""

    def load() -> list[CachedSecret]:
        with engine.connect() as conn:
            rows = SqlRepository(conn).openable_rows()
        entries: list[CachedSecret] = []
        for row in rows:
            try:
                spec = REGISTRY[SecretType(row.secret_type)]
            except (ValueError, KeyError):
                continue
            if row.sealed_value is None:
                telemetry.count_open_failure(spec.name)
                raise CacheLoadError("a stored platform secret could not be opened")
            try:
                with telemetry.timed_open(spec.name):
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
                raise CacheLoadError("a stored platform secret could not be opened") from None
            entries.append(
                CachedSecret(
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
            )
        return entries

    return load


class PgNotifyWaiter:
    """A dedicated psycopg2 connection LISTENing on the channel; `wait(t)` -> True on a NOTIFY."""

    def __init__(self, dsn: str) -> None:
        """Remember the DSN; the connection opens lazily and reopens after any failure."""
        self._dsn = dsn
        self._conn: Any = None

    def _connect(self) -> Any:
        """Open the autocommit LISTEN connection."""
        import psycopg2

        conn = psycopg2.connect(self._dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"LISTEN {NOTIFY_CHANNEL}")  # fixed identifier, no input
        return conn

    def wait(self, timeout: float) -> bool:
        """Block up to `timeout` seconds; True if a NOTIFY arrived. Connection errors just sleep."""
        try:
            if self._conn is None:
                self._conn = self._connect()
            ready, _, _ = select.select([self._conn], [], [], timeout)
            if not ready:
                return False
            self._conn.poll()
            fired = bool(self._conn.notifies)
            self._conn.notifies.clear()
            return fired
        except Exception as exc:  # noqa: BLE001 -- a broken LISTEN falls back to the poll
            logger.warning("platform secret LISTEN failed error_type=%s", type(exc).__name__)
            self.close()
            time.sleep(min(timeout, 5.0))
            return False

    def close(self) -> None:
        """Drop the connection (best effort)."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001 -- closing a dead connection is best effort
                logger.debug(
                    "platform secret LISTEN close failed error_type=%s", type(exc).__name__
                )


class CacheRefresher:
    """Daemon thread: refresh on NOTIFY or every `poll_s`; stop with `stop()`."""

    def __init__(
        self,
        cache: PlatformSecretCache,
        wait: Callable[[float], bool],
        *,
        poll_s: float = POLL_SECONDS,
    ) -> None:
        """`wait(timeout)` returns True on a NOTIFY and False on a timeout."""
        self._cache = cache
        self._wait = wait
        self._poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> bool:
        """Wait once, then refresh; True if the refresh succeeded. Never raises."""
        self._wait(self._poll_s)
        try:
            self._cache.refresh()
        except Exception as exc:  # noqa: BLE001 -- keep last-known, count, retry next cycle
            telemetry.count_open_failure("all", "error")
            logger.error("platform secret refresh failed error_type=%s", type(exc).__name__)
            return False
        return True

    def _loop(self) -> None:
        """Refresh until stopped."""
        while not self._stop.is_set():
            self.run_once()

    def start(self) -> None:
        """Start the daemon thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="platform-secrets-refresh", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to stop after its current wait."""
        self._stop.set()
