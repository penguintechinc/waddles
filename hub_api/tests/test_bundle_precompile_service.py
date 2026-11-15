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
            self.pending[params["digest"]] = {**params, "last_error": None}
            return _Rows([_Row(id=len(self.pending))])
        if "UPDATE bundle_precompile_requests r" in sql:
            picked = list(self.pending.values())[: params["limit"]]
            return _Rows(
                [
                    _Row(artifact_digest=p["digest"], app_id=p["app_id"], version=p["version"])
                    for p in picked
                ]
            )
        if sql.strip().startswith("DELETE"):
            self.pending.pop(params["digest"], None)
            return _Rows()
        if "SET last_error" in sql:
            self.pending[params["digest"]]["last_error"] = params["error"]
            return _Rows()
        raise AssertionError(f"unexpected SQL {sql}")

    async def read(self, _dal: Any, sql: str, params: dict[str, Any] | None = None) -> _Rows:
        if "FROM app_active_versions" in sql:
            return _Rows(list(self.signed.values()))
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


async def test_backfill_export_lists_every_active_signed_version(
    queue: FakeQueue, tmp_path: Path
) -> None:
    _signed(queue)
    out = tmp_path / "m.json"
    assert await precompile_queue._export(None, out, all_active=True, limit=1) == 0
    assert json.loads(out.read_text())[0]["app_id"] == "waddles.test"


async def test_cli_ack_backfill_fails_on_any_failure(tmp_path: Path) -> None:
    ok = {"examined": 1, "compiled": 1, "skipped": 0, "failed": 0, "results": []}
    bad = {**ok, "failed": 1}
    assert await precompile_queue._ack(None, ok) == 0
    assert await precompile_queue._ack(None, bad) == 1


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
