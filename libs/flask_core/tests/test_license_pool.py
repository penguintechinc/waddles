"""Tests for flask_core.license_pool against a real local HTTP(S) server and a real in-memory store."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import ssl
import tempfile
from pathlib import Path
from typing import Any

import flask_core.license_pool as lp
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from flask_core.entitlement import EntitlementClient
from flask_core.license_pool import (
    LicenseServerPoolProvider,
    PoolInfo,
    PoolRow,
    PoolUnavailable,
)

LICENSE_LEAK_CANARY = "canary-not-a-real-key-for-log-leak-test"
PRO, ENT = 2, 3


class MemoryStore:
    """Real in-memory PoolStore."""

    def __init__(self) -> None:
        self.rows: dict[tuple[int, int], PoolRow] = {}

    async def get(self, tenant_id: int, tier_code: int) -> PoolRow | None:
        return self.rows.get((tenant_id, tier_code))

    async def put(
        self, tenant_id, tier_code, limit_count, source_code, observed_at, set_by=None
    ):  # type: ignore[no-untyped-def]
        self.rows[(tenant_id, tier_code)] = PoolRow(
            limit_count, observed_at, source_code
        )

    async def delete_license_server_rows(self, tenant_id: int) -> int:
        gone = [
            k
            for k, r in self.rows.items()
            if k[0] == tenant_id and r.source_code == lp.SOURCE_LICENSE_SERVER
        ]
        for k in gone:
            del self.rows[k]
        return len(gone)


class Clock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 11, 17, tzinfo=dt.timezone.utc)

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


class Server:
    """Local stdlib asyncio HTTP/1.1 server whose behavior the test scripts."""

    def __init__(self) -> None:
        self.calls = 0
        self.bodies: list[dict[str, Any]] = []
        self.script: list[tuple[int, Any]] = []  # consumed per call; last repeats
        self.delay = 0.0
        self.server: asyncio.base_events.Server | None = None
        self.url = ""

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                headers = {}
                for line in head.decode("latin-1").split("\r\n")[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                raw = await reader.readexactly(int(headers.get("content-length", "0")))
                self.calls += 1
                self.bodies.append(json.loads(raw or b"{}"))
                if self.delay:
                    await asyncio.sleep(self.delay)
                status, body = (
                    self.script.pop(0) if len(self.script) > 1 else self.script[0]
                )
                out = json.dumps(body).encode()
                writer.write(
                    f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(out)}\r\n\r\n".encode()
                    + out
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    async def start(self, ssl_ctx: ssl.SSLContext | None = None) -> None:
        self.server = await asyncio.start_server(
            self._serve, "127.0.0.1", 0, ssl=ssl_ctx
        )
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"{'https' if ssl_ctx else 'http'}://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            # providers keep one shared keep-alive client open; don't wait on it
            try:
                await asyncio.wait_for(self.server.wait_closed(), 0.3)
            except asyncio.TimeoutError:
                pass


GOOD = {"team": {"professional": {"limit": 5}, "enterprise": {"limit": -1}}}


@pytest.fixture
async def server():
    s = Server()
    s.script = [(200, GOOD)]
    await s.start()
    yield s
    await s.stop()


async def _noop_sleep(_d: float) -> None:
    return None


def make(
    server_url: str,
    store: MemoryStore,
    clock: Clock,
    *,
    bypass=False,
    key=LICENSE_LEAK_CANARY,
    **kw: Any,
):
    async def resolver(_tid: int) -> str | None:
        return key

    return LicenseServerPoolProvider(
        store,
        resolver,
        lambda: bypass,
        base_url=server_url,
        clock=clock,
        sleep=_noop_sleep,
        **{"allow_loopback_http": True, **kw},
    )


async def test_bypass_uses_operator_row_and_never_calls_http(server: Server) -> None:
    store, clock = MemoryStore(), Clock()
    await store.put(1, PRO, 7, lp.SOURCE_OPERATOR_SET, clock())
    p = make(server.url, store, clock, bypass=True)
    info = await p.get_pool(1, PRO)
    assert (info.limit, info.source, info.stale) == (7, "operator_set", False)
    missing = await p.get_pool(1, ENT)
    assert missing.limit is None
    assert server.calls == 0


async def test_success_cached_within_ttl_and_unlimited(server: Server) -> None:
    store, clock = MemoryStore(), Clock()
    p = make(server.url, store, clock)
    a = await p.get_pool(1, ENT)
    assert a.limit == -1 and a.source == "license_server" and not a.stale
    b = await p.get_pool(1, PRO)
    assert b.limit == 5
    assert server.calls == 1
    clock.advance(301)
    await p.get_pool(1, PRO)
    assert server.calls == 2
    assert server.bodies[0]["license_key"] == LICENSE_LEAK_CANARY  # key only in body


async def test_absent_team_axis_logs_error_and_raises(server: Server, caplog) -> None:
    server.script = [(200, {"tier": "professional", "node_limit": 3})]
    p = make(server.url, MemoryStore(), Clock())
    with (
        caplog.at_level(logging.ERROR, logger="flask_core.license_pool"),
        pytest.raises(PoolUnavailable),
    ):
        await p.get_pool(1, PRO)
    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errs) == 1 and "license_pool.team_axis_absent" in errs[0].getMessage()


async def test_outage_within_grace_is_stale_and_beyond_raises(server: Server) -> None:
    store, clock = MemoryStore(), Clock()
    p = make(server.url, store, clock)
    await p.get_pool(1, PRO)
    server.script = [(500, {})]
    clock.advance(3600)
    info = await p.get_pool(1, PRO)
    assert info.stale and info.limit == 5
    clock.advance(72 * 3600)
    with pytest.raises(PoolUnavailable):
        await p.get_pool(1, PRO)


async def test_transient_5xx_retried_then_success(server: Server) -> None:
    server.script = [(503, {}), (502, {}), (200, GOOD)]
    p = make(server.url, MemoryStore(), Clock())
    assert (await p.get_pool(1, PRO)).limit == 5
    assert server.calls == 3


async def test_4xx_not_retried(server: Server) -> None:
    server.script = [(403, {"error": "no"})]
    p = make(server.url, MemoryStore(), Clock())
    with pytest.raises(PoolUnavailable):
        await p.get_pool(1, PRO)
    assert server.calls == 1


async def test_no_tenant_license_raises(server: Server) -> None:
    p = make(server.url, MemoryStore(), Clock(), key=None)  # type: ignore[arg-type]
    with pytest.raises(PoolUnavailable, match="no_tenant_license"):
        await p.get_pool(1, PRO)
    assert server.calls == 0


async def test_single_flight_coalesces(server: Server) -> None:
    server.delay = 0.1
    p = make(server.url, MemoryStore(), Clock())
    res = await asyncio.gather(*[p.get_pool(1, PRO) for _ in range(10)])
    assert all(r.limit == 5 for r in res)
    assert server.calls == 1


async def test_tls_verification_on_self_signed_fails(caplog) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]
            ),
            False,
        )
        .sign(key, hashes.SHA256())
    )
    with tempfile.TemporaryDirectory() as d:
        cp, kp = Path(d, "c.pem"), Path(d, "k.pem")
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        kp.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cp, kp)
        s = Server()
        s.script = [(200, GOOD)]
        await s.start(ctx)
        try:
            p = make(s.url, MemoryStore(), Clock())  # default factory: verify=True
            with pytest.raises(PoolUnavailable):
                await p.get_pool(1, PRO)
            assert s.calls == 0
        finally:
            await s.stop()


async def test_license_key_never_logged(server: Server, caplog) -> None:
    server.script = [(500, {})]
    p = make(server.url, MemoryStore(), Clock())
    with caplog.at_level(logging.DEBUG), pytest.raises(PoolUnavailable):
        await p.get_pool(1, PRO)
    assert LICENSE_LEAK_CANARY not in caplog.text


# -- entitlement hook -------------------------------------------------------
class _Flag:
    def is_enabled(self, *a: Any, **k: Any) -> bool:
        return True


class _Gate:
    def resolve_tier(self) -> str:
        return "free"


class _Source:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def community_tier(self, tenant_id: int, community_id: int) -> str | None:
        self.calls.append((tenant_id, community_id))
        return "enterprise"


async def test_entitlement_hook_inert_without_source_and_numeric_with_source() -> None:
    plain = EntitlementClient(flag_gate=_Flag(), license_gate=_Gate())
    assert await plain.effective_tier(tenant="acme", community=9, tenant_id=7) == "free"
    src = _Source()
    c = EntitlementClient(
        flag_gate=_Flag(), license_gate=_Gate(), community_tier_source=src
    )
    # numeric id supplied explicitly: the slug string is irrelevant to the lookup
    assert (
        await c.effective_tier(tenant="acme", community=9, tenant_id=7) == "enterprise"
    )
    assert src.calls == [(7, 9)]


async def test_entitlement_hook_never_coerces_slug_to_tenant_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: digits-only slug '1' must NOT become tenant id 1; no id => no uplift."""
    src = _Source()
    c = EntitlementClient(
        flag_gate=_Flag(), license_gate=_Gate(), community_tier_source=src
    )
    with caplog.at_level("ERROR"):
        assert await c.effective_tier(tenant="1", community=9) == "free"
        assert await c.effective_tier(tenant="acme", community=9) == "free"
        assert await c.effective_tier(tenant="1", community=9, tenant_id=True) == "free"  # type: ignore[arg-type]
    assert src.calls == []
    assert "no_tenant_id" in caplog.text
    # slug '1' with the real id 42: the source sees 42, never 1
    assert await c.effective_tier(tenant="1", community=9, tenant_id=42) == "enterprise"
    assert src.calls == [(42, 9)]


def test_poolinfo_poolrow_construction_and_frozen() -> None:
    now = dt.datetime(2026, 11, 17, tzinfo=dt.timezone.utc)
    row = PoolRow(3, now, lp.SOURCE_OFFLINE)
    info = PoolInfo(limit=3, observed_at=now, stale=False, source="offline")
    assert (row.limit_count, row.source_code, info.limit) == (3, 2, 3)
    with pytest.raises(AttributeError):
        info.limit = 4  # type: ignore[misc]


@pytest.mark.parametrize("bad", [0, 1, 4, -1])
async def test_invalid_tier_code_raises_value_error(server: Server, bad: int) -> None:
    p = make(server.url, MemoryStore(), Clock())
    with pytest.raises(ValueError):
        await p.get_pool(1, bad)


async def test_offline_row_is_read_and_returned(server: Server) -> None:
    store, clock = MemoryStore(), Clock()
    await store.put(1, PRO, 9, lp.SOURCE_OFFLINE, clock())
    info = await make(server.url, store, clock).get_pool(1, PRO)
    assert (info.limit, info.source, info.stale) == (9, "offline", False)
    assert server.calls == 0


async def test_grace_never_applies_to_403_or_missing_key_and_evicts(
    server: Server,
) -> None:
    """A cached pool must not survive a definitive refusal (revoked/removed key)."""
    store, clock = MemoryStore(), Clock()
    await make(server.url, store, clock).get_pool(1, PRO)
    assert (1, PRO) in store.rows
    clock.advance(3600)
    server.script = [(403, {"error": "revoked"})]
    with pytest.raises(PoolUnavailable):
        await make(server.url, store, clock).get_pool(1, PRO)
    assert (1, PRO) not in store.rows  # evicted, grace did not resurrect it

    store2 = MemoryStore()
    server.script = [(200, GOOD)]
    await make(server.url, store2, clock).get_pool(1, PRO)
    clock.advance(3600)
    with pytest.raises(PoolUnavailable, match="no_tenant_license"):
        await make(server.url, store2, clock, key=None).get_pool(1, PRO)  # type: ignore[arg-type]
    assert (1, PRO) not in store2.rows


async def test_operator_rows_survive_eviction() -> None:
    store, clock = MemoryStore(), Clock()
    await store.put(1, PRO, 4, lp.SOURCE_OPERATOR_SET, clock())
    await store.put(1, ENT, 4, lp.SOURCE_LICENSE_SERVER, clock())
    assert await store.delete_license_server_rows(1) == 1
    assert (1, PRO) in store.rows


async def test_naive_observed_at_is_normalized_not_typeerror(server: Server) -> None:
    store, clock = MemoryStore(), Clock()
    await store.put(1, PRO, 3, lp.SOURCE_OFFLINE, dt.datetime(2026, 11, 17))  # noqa: DTZ001 - naive on purpose
    info = await make(server.url, store, clock).get_pool(1, PRO)
    assert info.limit == 3 and info.observed_at.tzinfo is not None
    await store.put(1, ENT, 3, lp.SOURCE_LICENSE_SERVER, dt.datetime(2026, 11, 16))  # noqa: DTZ001 - naive on purpose
    server.script = [(500, {})]
    stale = await make(server.url, store, clock).get_pool(1, ENT)
    assert stale.stale and stale.limit == 3


async def test_total_deadline_bounds_slow_server(server: Server) -> None:
    server.delay = 5
    p = make(server.url, MemoryStore(), Clock(), total_timeout=0.2)
    with pytest.raises(PoolUnavailable, match="timeout"):
        await p.get_pool(1, PRO)
    await p.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "http://license.example.com",
        "https://user:pw@license.example.com",
        "https://license.example.com/?a=1",
        "https://license.example.com/#frag",
        "ftp://license.example.com",
        "https://",
        "http://127.0.0.1:9",  # loopback http needs the explicit opt-in
    ],
)
def test_base_url_validation_rejects(url: str) -> None:
    with pytest.raises(ValueError):
        lp.validate_base_url(url)


def test_base_url_validation_accepts() -> None:
    assert lp.validate_base_url("https://license.penguintech.io/") == (
        "https://license.penguintech.io"
    )
    assert lp.validate_base_url("http://127.0.0.1:9", allow_loopback_http=True)
    with pytest.raises(ValueError):
        lp.validate_base_url("http://example.com", allow_loopback_http=True)
