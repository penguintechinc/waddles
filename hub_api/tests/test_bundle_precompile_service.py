"""Unit tests for the bundle AOT precompile queue service + its export/ack CLI.

The SQL itself (ON CONFLICT, SKIP LOCKED lease) is exercised against real Postgres in
`alembic/tests/test_0100_bundle_precompile_requests.py`; here the DAL calls are faked so the
service's own logic (normalization, enqueue-once signalling, ack routing, CLI gates) is covered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli import precompile_queue
from services import bundle_precompile_service as svc

DIGEST = "ab" * 32


class _Row:
    def __init__(self, **kw: Any) -> None:
        """Attribute bag mirroring a penguin_dal Row."""
        self.__dict__.update(kw)


class _Rows(list):  # type: ignore[type-arg]
    def first(self) -> Any:
        return self[0] if self else None


class FakeQueue:
    """In-memory stand-in honouring the queue's UNIQUE(artifact_digest) semantics."""

    def __init__(self) -> None:
        """Start empty: no pending rows, no signed versions."""
        self.pending: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.signed: dict[tuple[str, str, str], _Row] = {}

    async def write(self, _dal: Any, sql: str, params: dict[str, Any] | None = None) -> _Rows:
        params = params or {}
        self.writes.append((sql, params))
        if "INSERT INTO bundle_precompile_requests" in sql:
            if params["digest"] in self.pending:
                return _Rows()
            self.pending[params["digest"]] = {
                **params,
                "id": len(self.pending) + 1,
                "last_error": None,
                "attempts": 0,
                "claimed_at": None,
                "stale_reclaims": 0,
            }
            return _Rows([_Row(id=len(self.pending))])
        if "SET attempts = GREATEST" in sql:
            hits = []
            for p in self.pending.values():
                if (
                    p["attempts"] < params["max_attempts"]
                    and p["claimed_at"] is not None
                    and p.get("lease_expired")
                    and p["stale_reclaims"] + 1 > params["max_stale_reclaims"]
                ):
                    p["attempts"] = max(p["attempts"], params["max_attempts"])
                    p["stale_reclaims"] += 1
                    p["last_error"] = (
                        f"lease expired {p['stale_reclaims']} times without ack "
                        "(runner crash/timeout)"
                    )
                    hits.append(
                        _Row(
                            app_id=p["app_id"],
                            version=p["version"],
                            attempts=p["attempts"],
                            stale_reclaims=p["stale_reclaims"],
                        )
                    )
            return _Rows(hits)
        if "UPDATE bundle_precompile_requests r" in sql:
            picked = [
                p
                for p in self.pending.values()
                if p["attempts"] < params["max_attempts"]
                and (p["claimed_at"] is None or p.get("lease_expired"))
            ][: params["limit"]]
            rows = []
            for p in picked:
                rows.append(
                    _Row(
                        artifact_digest=p["digest"],
                        app_id=p["app_id"],
                        version=p["version"],
                        prev_claimed_at=p["claimed_at"],
                    )
                )
                if p["claimed_at"] is not None:
                    p["stale_reclaims"] += 1
                p["claimed_at"] = "now"
                p["lease_expired"] = False
            return _Rows(rows)
        if sql.strip().startswith("DELETE"):
            self.pending.pop(params["digest"], None)
            return _Rows()
        if "SET last_error = :error" in sql:
            p = self.pending[params["digest"]]
            p["last_error"] = params["error"]
            p["attempts"] += 1
            p["stale_reclaims"] = 0
            return _Rows([_Row(app_id=p["app_id"], version=p["version"], attempts=p["attempts"])])
        if "WHERE artifact_digest = :digest AND attempts >= :max_attempts" in sql:
            hit = self.pending.get(params["digest"])
            if hit is None or hit["attempts"] < params["max_attempts"]:
                return _Rows()
            p = hit
            p.update(attempts=0, stale_reclaims=0, claimed_at=None, last_error=None)
            return _Rows([_Row(id=p["id"])])
        if "WHERE id = :id AND attempts >= :max_attempts" in sql:
            for p in self.pending.values():
                if p["id"] == params["id"] and p["attempts"] >= params["max_attempts"]:
                    p.update(attempts=0, stale_reclaims=0, claimed_at=None, last_error=None)
                    return _Rows([_Row(id=p["id"])])
            return _Rows()
        raise AssertionError(f"unexpected SQL {sql}")

    async def read(self, _dal: Any, sql: str, params: dict[str, Any] | None = None) -> _Rows:
        if "FROM app_active_versions" in sql:
            return _Rows(list(self.signed.values()))
        if "COUNT(*) AS n" in sql:
            assert params is not None
            n = sum(1 for p in self.pending.values() if p["attempts"] >= params["max_attempts"])
            return _Rows([_Row(n=n)])
        if "ILIKE" in sql:
            assert params is not None
            frag = params["error_like"]
            out = []
            for p in self.pending.values():
                if p["attempts"] < params["max_attempts"]:
                    continue
                if params["app_id"] is not None and p["app_id"] != params["app_id"]:
                    continue
                needle = (frag or "").strip("%").lower()
                if frag is not None and needle not in (p["last_error"] or "").lower():
                    continue
                out.append(
                    _Row(
                        id=p["id"], app_id=p["app_id"], version=p["version"], attempts=p["attempts"]
                    )
                )
            return _Rows(out[: params["fetch_limit"]])
        assert params is not None
        row = self.signed.get((params["app_id"], params["version"], params["digest"]))
        return _Rows([row] if row else [])


@pytest.fixture()
def queue(monkeypatch: pytest.MonkeyPatch) -> FakeQueue:
    q = FakeQueue()
    monkeypatch.setattr(svc, "raw_sql_write", q.write)
    monkeypatch.setattr(svc, "raw_sql_rows", q.read)
    return q


def _signed(q: FakeQueue, app_id: str = "waddles.test", version: str = "1.0.0") -> None:
    q.signed[(app_id, version, DIGEST)] = _Row(
        app_id=app_id,
        version=version,
        artifact_digest=DIGEST,
        component_key=f"bundles/{app_id}/{version}/{DIGEST}.wasm",
        sidecar_key=f"bundles/{app_id}/{version}/{DIGEST}.json",
    )


async def test_publish_hook_enqueues_once_per_digest(queue: FakeQueue) -> None:
    first = await svc.enqueue_precompile(
        None, app_id="waddles.test", version="1.0.0", digest=f"sha256:{DIGEST.upper()}"
    )
    second = await svc.enqueue_precompile(
        None, app_id="waddles.test", version="1.0.0", digest=DIGEST
    )
    assert (first, second) == (True, False)
    assert list(queue.pending) == [DIGEST]


@pytest.mark.parametrize("bad", ["", "abc", "../" + "a" * 61, "g" * 64])
async def test_enqueue_rejects_malformed_digests(queue: FakeQueue, bad: str) -> None:
    with pytest.raises(ValueError):
        await svc.enqueue_precompile(None, app_id="a", version="1", digest=bad)
    assert queue.writes == []


async def test_claim_resolves_keys_and_skips_unsigned(queue: FakeQueue) -> None:
    await svc.enqueue_precompile(None, app_id="waddles.test", version="1.0.0", digest=DIGEST)
    assert await svc.claim_pending(None, limit=10) == []  # not signed yet -> retried later
    _signed(queue)
    queue.pending[DIGEST]["lease_expired"] = True  # the first lease ran out
    items = await svc.claim_pending(None, limit=10)
    assert [i.as_json()["digest"] for i in items] == [DIGEST]
    assert items[0].component_key.endswith(".wasm")


async def test_claim_rejects_nonpositive_bounds(queue: FakeQueue) -> None:
    with pytest.raises(ValueError):
        await svc.claim_pending(None, limit=0)


async def test_ack_deletes_done_and_records_failures(queue: FakeQueue) -> None:
    other = "cd" * 32
    await svc.enqueue_precompile(None, app_id="a", version="1", digest=DIGEST)
    await svc.enqueue_precompile(None, app_id="b", version="1", digest=other)
    summary = await svc.ack_report(
        None,
        {
            "results": [
                {"digest": DIGEST, "status": "compiled"},
                {"digest": other, "status": "failed", "error": "x" * 900},
            ]
        },
    )
    assert (summary.examined, summary.acked, summary.failed) == (2, 1, 1)
    assert list(queue.pending) == [other]
    assert len(queue.pending[other]["last_error"]) == 500


@pytest.mark.parametrize(
    "report",
    [{}, {"results": [1]}, {"results": [{"digest": DIGEST, "status": "weird"}]}],
)
async def test_ack_rejects_malformed_reports(queue: FakeQueue, report: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await svc.ack_report(None, report)


async def test_enqueue_all_active_queues_every_active_signed_version_once(
    queue: FakeQueue,
) -> None:
    """The per-release enqueue Job only INSERTs queue rows; a re-run queues nothing new."""
    _signed(queue)
    other = "cd" * 32
    queue.signed[("waddles.other", "2.0.0", other)] = _Row(
        app_id="waddles.other",
        version="2.0.0",
        artifact_digest=other,
        component_key="bundles/o.wasm",
        sidecar_key="bundles/o.json",
    )
    first = await svc.enqueue_all_active(None)
    assert (first.examined, first.queued, first.already_pending) == (2, 2, 0)
    assert set(queue.pending) == {DIGEST, other}
    again = await svc.enqueue_all_active(None)
    assert (again.examined, again.queued, again.already_pending) == (2, 0, 2)
    # Never compiles or claims: only INSERT statements were issued.
    assert all("INSERT INTO bundle_precompile_requests" in sql for sql, _ in queue.writes)


async def test_enqueue_all_active_with_nothing_active_reports_zero(queue: FakeQueue) -> None:
    summary = await svc.enqueue_all_active(None)
    assert (summary.examined, summary.queued) == (0, 0)


def test_cli_enqueue_all_active_command_runs_with_minimal_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`enqueue-all-active` builds its DAL from DATABASE_URL alone and closes it."""
    import asyncio

    seen: list[str] = []

    class _Dal:
        async def close(self) -> None:
            seen.append("closed")

    async def _build(url: str, pool_size: int) -> _Dal:
        seen.append(url)
        return _Dal()

    async def _enqueue(_dal: Any, revive_dead_letters: bool = False) -> svc.EnqueueSummary:
        seen.append(f"enqueued revive={revive_dead_letters}")
        return svc.EnqueueSummary(examined=3, queued=1, already_pending=2)

    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://r:p@h:5432/d")
    monkeypatch.setattr(precompile_queue, "build_install_dal", _build)
    monkeypatch.setattr(svc, "enqueue_all_active", _enqueue)
    rc = asyncio.run(
        precompile_queue._run(precompile_queue._parser().parse_args(["enqueue-all-active"]))
    )
    assert rc == 0
    assert seen == ["postgresql://r:p@h:5432/d", "enqueued revive=False", "closed"]


@pytest.mark.parametrize(
    "flag", [["export", "--out", "m", "--all-active"], ["ack", "--report", "r", "--backfill"]]
)
def test_cli_compile_in_helm_hook_modes_are_gone(flag: list[str]) -> None:
    """Regression (alpha 43868192527): the in-hook full backfill compile path no longer exists."""
    with pytest.raises(SystemExit):
        precompile_queue._parser().parse_args(flag)


def test_cli_denominator_mismatch_is_an_error(tmp_path: Path) -> None:
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps([{"digest": DIGEST}]))
    with pytest.raises(ValueError):
        precompile_queue._check_denominator({"examined": 0}, manifest)
    precompile_queue._check_denominator({"examined": 1}, manifest)


async def test_cli_queue_ack_exit_code_reflects_failures(queue: FakeQueue) -> None:
    await svc.enqueue_precompile(None, app_id="a", version="1", digest=DIGEST)
    rc = await precompile_queue._ack(
        object(), {"results": [{"digest": DIGEST, "status": "failed"}]}
    )
    assert rc == 1
    rc = await precompile_queue._ack(
        object(), {"results": [{"digest": DIGEST, "status": "skipped"}]}
    )
    assert rc == 0


def test_cli_config_needs_only_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (alpha 43797026024): export/ack must not require SECRET_KEY / HubAPIConfig."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://role:pw@db:5432/waddlebot")
    assert precompile_queue.database_url_from_env() == "postgresql://role:pw@db:5432/waddlebot"


@pytest.mark.parametrize("env", [{}, {"DATABASE_URL": ""}, {"DATABASE_URL": "   "}])
def test_cli_config_fails_closed_without_database_url(env: dict[str, str]) -> None:
    """A missing/blank DATABASE_URL raises -- never a guessed default DSN."""
    with pytest.raises(precompile_queue.PrecompileConfigError):
        precompile_queue.database_url_from_env(env)


def test_cli_run_uses_minimal_config_not_hub_api_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`export` builds its DAL from DATABASE_URL alone; SECRET_KEY unset must not matter."""
    import asyncio

    seen: list[str] = []

    class _Dal:
        async def close(self) -> None:
            seen.append("closed")

    async def _build(url: str, pool_size: int) -> _Dal:
        seen.append(url)
        return _Dal()

    async def _claim(_dal: Any, limit: int, **_kw: Any) -> list[Any]:
        return []

    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://r:p@h:5432/d")
    monkeypatch.setattr(precompile_queue, "build_install_dal", _build)
    monkeypatch.setattr(svc, "claim_pending", _claim)
    out = tmp_path / "m.json"
    rc = asyncio.run(
        precompile_queue._run(precompile_queue._parser().parse_args(["export", "--out", str(out)]))
    )
    assert rc == 0 and json.loads(out.read_text()) == []
    assert seen == ["postgresql://r:p@h:5432/d", "closed"]
