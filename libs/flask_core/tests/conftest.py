"""Shared fixtures for flask_core tests.

Provides an in-memory, async, redis.asyncio-compatible fake so
StreamPipeline's generic contract can be exercised without a live
Valkey/Redis server. Only the small Streams surface StreamPipeline
actually calls is implemented.
"""

import importlib.util
import itertools
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import redis as redis_module

# --- flask_core import shim (shared by workload_identity + stream_pipeline tests)
# flask_core/__init__.py eagerly imports the full service stack (pydal, quart,
# authlib, ...). The leaf modules under test depend only on stdlib (+ optional
# redis), so register a lightweight namespace-style `flask_core` package whose
# __path__ points at the real dir. Submodule imports then resolve directly
# without executing the heavy package __init__. A fully provisioned env is
# unaffected. Runs before any test imports flask_core.*.
import pathlib as _pathlib
import types as _types

_PKG_DIR = _pathlib.Path(__file__).resolve().parent.parent / "flask_core"
if "flask_core" not in sys.modules:
    _stub = _types.ModuleType("flask_core")
    _stub.__path__ = [str(_PKG_DIR)]
    sys.modules["flask_core"] = _stub
# --- end shim

# --- libs/ on sys.path (module-package tests, e.g. bot_module.features)
# Module packages (bot_module, and social/marketing/customer_module as they
# land) are siblings of flask_core under libs/, not inside it, and register
# their Feature contracts against flask_core.feature_registry -- so their
# tests need libs/ importable the same way flask_core itself is. Real
# package imports (bot_module has its own __init__.py), no stub needed.
_LIBS_DIR = _PKG_DIR.parent.parent
if str(_LIBS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIBS_DIR))
# --- end libs/ path



def _load_stream_pipeline_module():
    """Load stream_pipeline.py directly, bypassing flask_core/__init__.py.

    The package __init__ eagerly imports every sibling module (database,
    auth, cache, ...), pulling in pydal/sqlalchemy/flask-security-too that
    this leaf-module unit test has no business depending on. stream_pipeline
    itself only imports stdlib + optional `redis`, so it loads standalone.
    """
    module_name = "flask_core.stream_pipeline"
    if module_name in sys.modules:
        return sys.modules[module_name]
    src = Path(__file__).resolve().parent.parent / "flask_core" / "stream_pipeline.py"
    spec = importlib.util.spec_from_file_location(module_name, src)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponseError(redis_module.ResponseError):
    """Stand-in for redis.ResponseError so BUSYGROUP/no-such-key paths exercise real handling."""


class _FakeStream:
    """A single append-only stream plus its consumer groups."""

    def __init__(self) -> None:
        self.entries: List[Tuple[str, Dict[str, str]]] = []
        self.groups: Dict[str, Dict[str, Any]] = {}


class FakeAsyncRedis:
    """Minimal in-memory async Redis Streams client.

    Implements just enough of the redis.asyncio.Redis Streams surface
    (xadd, xreadgroup, xack, xgroup_create, xpending_range, xinfo_stream,
    xrange, xtrim, ping, close) for StreamPipeline's unit tests.
    """

    def __init__(self) -> None:
        self._streams: Dict[str, _FakeStream] = {}
        self._id_counter = itertools.count(1)

    def _stream(self, name: str) -> _FakeStream:
        return self._streams.setdefault(name, _FakeStream())

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def xadd(
        self,
        name: str,
        fields: Dict[str, str],
        maxlen: Optional[int] = None,
        approximate: bool = True,
    ) -> str:
        stream = self._stream(name)
        message_id = f"{next(self._id_counter)}-0"
        stream.entries.append((message_id, dict(fields)))
        if maxlen is not None and len(stream.entries) > maxlen:
            stream.entries = stream.entries[-maxlen:]
        return message_id

    async def xgroup_create(
        self, name: str, groupname: str, id: str = "0", mkstream: bool = False
    ) -> bool:
        stream = self._stream(name)
        if groupname in stream.groups:
            raise FakeResponseError("BUSYGROUP Consumer Group name already exists")
        stream.groups[groupname] = {"last_delivered": 0, "pending": {}}
        return True

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: Dict[str, str],
        count: Optional[int] = None,
        block: Optional[int] = None,
    ) -> List[Tuple[str, List[Tuple[str, Dict[str, str]]]]]:
        results: List[Tuple[str, List[Tuple[str, Dict[str, str]]]]] = []
        for name in streams:
            stream = self._stream(name)
            group = stream.groups[groupname]
            new_entries = stream.entries[group["last_delivered"]:]
            if count is not None:
                new_entries = new_entries[:count]
            if not new_entries:
                continue
            delivered: List[Tuple[str, Dict[str, str]]] = []
            for message_id, fields in new_entries:
                group["pending"][message_id] = {
                    "consumer": consumername,
                    "times_delivered": group["pending"].get(message_id, {}).get(
                        "times_delivered", 0
                    )
                    + 1,
                }
                delivered.append((message_id, fields))
            group["last_delivered"] += len(new_entries)
            results.append((name, delivered))
        return results

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        stream = self._stream(name)
        group = stream.groups.get(groupname, {"pending": {}})
        acked = 0
        for message_id in ids:
            if message_id in group.get("pending", {}):
                del group["pending"][message_id]
                acked += 1
        return acked

    async def xpending_range(
        self,
        name: str,
        groupname: str,
        min: str,
        max: str,
        count: int,
        consumername: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        stream = self._stream(name)
        group = stream.groups.get(groupname, {"pending": {}})
        result = []
        for message_id, info in list(group.get("pending", {}).items())[:count]:
            if consumername and info["consumer"] != consumername:
                continue
            result.append(
                {
                    "message_id": message_id,
                    "consumer": info["consumer"],
                    "time_since_delivered": 0,
                    "times_delivered": info["times_delivered"],
                }
            )
        return result

    async def xinfo_stream(self, name: str) -> Dict[str, Any]:
        if name not in self._streams or not self._streams[name].entries:
            raise FakeResponseError("ERR no such key")
        stream = self._streams[name]
        return {
            "length": len(stream.entries),
            "radix-tree-keys": 0,
            "radix-tree-nodes": 0,
            "groups": len(stream.groups),
            "last-generated-id": stream.entries[-1][0],
            "first-entry": stream.entries[0],
            "last-entry": stream.entries[-1],
        }

    async def xrange(
        self, name: str, min: str = "-", max: str = "+", count: Optional[int] = None
    ) -> List[Tuple[str, Dict[str, str]]]:
        stream = self._stream(name)
        entries = stream.entries
        if count is not None:
            entries = entries[:count]
        return list(entries)

    async def xtrim(self, name: str, maxlen: int, approximate: bool = True) -> int:
        stream = self._stream(name)
        removed = max(0, len(stream.entries) - maxlen)
        stream.entries = stream.entries[-maxlen:]
        return removed


@pytest.fixture
async def fake_redis() -> FakeAsyncRedis:
    """A fresh in-memory fake Redis client per test."""
    return FakeAsyncRedis()


@pytest.fixture
async def pipeline(fake_redis: FakeAsyncRedis):
    """A connected StreamPipeline wired to the in-memory fake, bypassing real Redis I/O."""
    StreamPipeline = _load_stream_pipeline_module().StreamPipeline

    p = StreamPipeline(redis_url="redis://fake", enabled=True)
    p._redis = fake_redis
    p._connected = True
    yield p
    p._connected = False


class JwtMetrics:
    """In-memory OTel reader bound to the REAL ``flask_core.jwt_hardening`` instruments.

    Lets a test count emitted verification metrics without a collector; an empty result is the
    caller's to assert against (a zero denominator is a failure, never a pass).
    """

    def __init__(self) -> None:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        from flask_core import jwt_hardening

        self.reader = InMemoryMetricReader()
        jwt_hardening.use_meter_provider(MeterProvider(metric_readers=[self.reader]))

    def _points(self, name: str) -> list[Any]:
        data = self.reader.get_metrics_data()
        points: list[Any] = []
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name:
                        points.extend(metric.data.data_points)
        return points

    def verifications(self) -> dict[tuple[str, str, str], int]:
        """``(verifier, alg, outcome) -> count`` for ``waddles_jwt_verifications_total``."""
        return {
            (p.attributes["verifier"], p.attributes["alg"], p.attributes["outcome"]): int(p.value)
            for p in self._points("waddles_jwt_verifications_total")
        }

    def duration_counts(self) -> dict[tuple[str, str], int]:
        """``(verifier, alg) -> observations`` for ``waddles_jwt_verification_seconds``."""
        return {
            (p.attributes["verifier"], p.attributes["alg"]): int(p.count)
            for p in self._points("waddles_jwt_verification_seconds")
        }


@pytest.fixture
def metrics_capture():
    """Rebind the JWT verification metrics to an in-memory reader for one test."""
    from flask_core import jwt_hardening

    capture = JwtMetrics()
    yield capture
    jwt_hardening.use_meter_provider(None)
