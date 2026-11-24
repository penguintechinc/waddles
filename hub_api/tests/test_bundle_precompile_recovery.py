"""Recovery-path tests for the bundle precompile queue (dead letters, stale leases, requeue).

Regression: alpha precompile queue incident 2026-11-16..19 -- (a) a deterministic S3 403 exhausted
all attempts on 130 rows and (b) 50 rows dead-lettered with an empty `last_error` because every
claim burned an attempt even when the runner died before acking. Reuses the in-memory queue fake
from `test_bundle_precompile_service`.
"""

# ruff: noqa: F811
from __future__ import annotations

from typing import Any

import pytest

from cli import precompile_queue
from services import bundle_precompile_service as svc
from tests.test_bundle_precompile_service import (  # noqa: F401  # queue is a fixture
    DIGEST,
    FakeQueue,
    _signed,
    queue,
)


def _dead(
    q: FakeQueue,
    digest: str,
    app_id: str,
    error: str | None,
    attempts: int = 5,
    stale_reclaims: int = 0,
) -> None:
    """Seed a dead-lettered row directly (attempts at the cap)."""
    q.pending[digest] = {
        "digest": digest,
        "app_id": app_id,
        "version": "1.0.0",
        "id": len(q.pending) + 1,
        "last_error": error,
        "attempts": attempts,
        "stale_reclaims": stale_reclaims,
        "claimed_at": "old",
    }


async def test_stale_reclaim_under_cap_is_reoffered_without_burning_attempts(
    queue: FakeQueue,
) -> None:
    """Up to MAX_STALE_RECLAIMS expired leases are re-offered; attempts never moves."""
    await svc.enqueue_precompile(None, app_id="a", version="1.0.0", digest=DIGEST)
    _signed(queue, app_id="a")
    assert [i.digest for i in await svc.claim_pending(None, limit=10)] == [DIGEST]  # first claim
    for expected in range(1, svc.DEFAULT_MAX_STALE_RECLAIMS + 1):
        queue.pending[DIGEST]["lease_expired"] = True  # runner died, no ack
        items = await svc.claim_pending(None, limit=10)
        assert [i.digest for i in items] == [DIGEST]
        assert queue.pending[DIGEST]["stale_reclaims"] == expected
    assert queue.pending[DIGEST]["attempts"] == 0
    assert await svc.record_dead_letters(None) == 0


async def test_stale_reclaim_over_cap_dead_letters_with_error_and_warning(
    queue: FakeQueue, caplog: pytest.LogCaptureFixture
) -> None:
    """The reclaim past the cap dead-letters (poison pill): not offered, error set, WARN logged."""
    await svc.enqueue_precompile(None, app_id="app.x", version="1.0.0", digest=DIGEST)
    _signed(queue, app_id="app.x")
    await svc.claim_pending(None, limit=10)
    for _ in range(svc.DEFAULT_MAX_STALE_RECLAIMS):
        queue.pending[DIGEST]["lease_expired"] = True
        await svc.claim_pending(None, limit=10)
    queue.pending[DIGEST]["lease_expired"] = True
    with caplog.at_level("WARNING"):
        items = await svc.claim_pending(None, limit=10)
    assert items == []
    n = svc.DEFAULT_MAX_STALE_RECLAIMS + 1
    row = queue.pending[DIGEST]
    assert row["last_error"] == f"lease expired {n} times without ack (runner crash/timeout)"
    assert row["attempts"] >= svc.DEFAULT_MAX_ATTEMPTS
    assert svc._DEAD_LETTERS_SEEN == 1  # the gauge counts it
    warn = [r for r in caplog.records if "lease expired too many times" in r.getMessage()]
    assert len(warn) == 1
    extra = warn[0].__dict__
    assert (extra.get("app_id"), extra.get("version"), extra.get("stale_reclaims")) == (
        "app.x",
        "1.0.0",
        n,
    )
    queue.pending[DIGEST]["lease_expired"] = True
    assert await svc.claim_pending(None, limit=10) == []  # stays dead


async def test_poison_item_does_not_starve_other_items(queue: FakeQueue) -> None:
    """After a poison row dead-letters, the rest of the queue is still claimed."""
    other = "cd" * 32
    await svc.enqueue_precompile(None, app_id="a", version="1.0.0", digest=DIGEST)
    await svc.enqueue_precompile(None, app_id="b", version="1.0.0", digest=other)
    _signed(queue, app_id="a")
    queue.signed[("b", "1.0.0", other)] = queue.signed[("a", "1.0.0", DIGEST)].__class__(
        app_id="b",
        version="1.0.0",
        artifact_digest=other,
        component_key="c",
        sidecar_key="s",
    )
    queue.pending[DIGEST].update(
        claimed_at="old", lease_expired=True, stale_reclaims=svc.DEFAULT_MAX_STALE_RECLAIMS
    )
    items = await svc.claim_pending(None, limit=10)
    assert [i.digest for i in items] == [other]
    assert queue.pending[DIGEST]["attempts"] >= svc.DEFAULT_MAX_ATTEMPTS


async def test_failed_ack_resets_stale_reclaims(queue: FakeQueue) -> None:
    """A genuine failed ack proves the runner finished the item, so the stale counter restarts."""
    await svc.enqueue_precompile(None, app_id="a", version="1.0.0", digest=DIGEST)
    queue.pending[DIGEST]["stale_reclaims"] = 2
    await svc.ack_report(None, {"results": [{"digest": DIGEST, "status": "failed", "error": "e"}]})
    assert queue.pending[DIGEST]["stale_reclaims"] == 0


async def test_claim_counts_stale_reclaims(
    queue: FakeQueue, caplog: pytest.LogCaptureFixture
) -> None:
    """Only a re-offered (previously claimed) row is a stale reclaim; first claims are not."""
    await svc.enqueue_precompile(None, app_id="a", version="1.0.0", digest=DIGEST)
    _signed(queue, app_id="a")
    with caplog.at_level("INFO"):
        await svc.claim_pending(None, limit=10)
        queue.pending[DIGEST]["lease_expired"] = True
        await svc.claim_pending(None, limit=10)
    stale = [
        getattr(r, "stale_reclaims", None) for r in caplog.records if "stale_reclaims" in r.__dict__
    ]
    assert stale == [0, 1]


async def test_failed_ack_consumes_attempts_and_dead_letters_with_warning(
    queue: FakeQueue, caplog: pytest.LogCaptureFixture
) -> None:
    """Genuine failures count; the max-th one logs a WARNING with app/version/attempts only."""
    await svc.enqueue_precompile(None, app_id="app.x", version="1.0.0", digest=DIGEST)
    failed = {"results": [{"digest": DIGEST, "status": "failed", "error": "secret-body"}]}
    with caplog.at_level("WARNING"):
        for _ in range(svc.DEFAULT_MAX_ATTEMPTS):
            await svc.ack_report(None, failed)
    assert queue.pending[DIGEST]["attempts"] == svc.DEFAULT_MAX_ATTEMPTS
    dead = [r for r in caplog.records if "dead letter)" in r.getMessage()]
    assert len(dead) == 1
    assert (
        getattr(dead[0], "app_id", None),
        getattr(dead[0], "version", None),
        getattr(dead[0], "attempts", None),
    ) == (
        "app.x",
        "1.0.0",
        svc.DEFAULT_MAX_ATTEMPTS,
    )
    assert "secret-body" not in repr(dead[0].__dict__)
    assert svc._DEAD_LETTERS_SEEN == 1


async def test_dead_letter_not_claimed_and_success_deletes_row(queue: FakeQueue) -> None:
    """A capped row is never leased; a successful ack deletes the row (no stale last_error)."""
    await svc.enqueue_precompile(None, app_id="a", version="1.0.0", digest=DIGEST)
    _signed(queue, app_id="a")
    await svc.ack_report(None, {"results": [{"digest": DIGEST, "status": "failed", "error": "e"}]})
    queue.pending[DIGEST]["lease_expired"] = True
    assert len(await svc.claim_pending(None, limit=10)) == 1  # retry after a genuine failure
    await svc.ack_report(None, {"results": [{"digest": DIGEST, "status": "compiled"}]})
    assert DIGEST not in queue.pending
    _dead(queue, DIGEST, "a", "boom")
    assert await svc.claim_pending(None, limit=10) == []


async def test_dead_letter_count_failure_never_fails_the_step(
    queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken count query is logged and returns -1; the queue step itself still succeeds."""

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(svc, "raw_sql_rows", _boom)
    assert await svc.record_dead_letters(None) == -1


async def test_reconcile_revives_dead_letters_for_active_versions_only(queue: FakeQueue) -> None:
    """enqueue_all_active(revive_dead_letters=True) resets capped active rows, clears last_error."""
    _signed(queue)
    other = "cd" * 32
    _dead(queue, DIGEST, "waddles.test", "S3 PUT 403", stale_reclaims=4)
    _dead(queue, other, "waddles.inactive", "boom")  # not active: must stay dead
    off = await svc.enqueue_all_active(None)  # default: insert-only, no revive
    assert (off.queued, off.revived, off.already_pending) == (0, 0, 1)
    assert queue.pending[DIGEST]["attempts"] == 5
    on = await svc.enqueue_all_active(None, revive_dead_letters=True)
    assert (on.examined, on.queued, on.revived, on.already_pending) == (1, 0, 1, 0)
    assert queue.pending[DIGEST]["attempts"] == 0
    assert queue.pending[DIGEST]["stale_reclaims"] == 0
    assert queue.pending[DIGEST]["last_error"] is None
    assert queue.pending[DIGEST]["claimed_at"] is None
    assert queue.pending[other]["attempts"] == 5


async def test_reconcile_revive_leaves_healthy_pending_rows_alone(queue: FakeQueue) -> None:
    """Rows still under the cap are not reset (their attempts/last_error are preserved)."""
    _signed(queue)
    _dead(queue, DIGEST, "waddles.test", "transient", attempts=2)
    summary = await svc.enqueue_all_active(None, revive_dead_letters=True)
    assert (summary.revived, summary.already_pending) == (0, 1)
    assert queue.pending[DIGEST]["attempts"] == 2
    assert queue.pending[DIGEST]["last_error"] == "transient"


async def test_requeue_failed_dry_run_counts_and_writes_nothing(queue: FakeQueue) -> None:
    """Default dry-run reports the match count and issues no UPDATE."""
    _dead(queue, DIGEST, "a", "403")
    before = len(queue.writes)
    summary = await svc.requeue_failed(None)
    assert (summary.matched, summary.requeued, summary.dry_run) == (1, 0, True)
    assert len(queue.writes) == before
    assert queue.pending[DIGEST]["attempts"] == 5


async def test_requeue_failed_filters_and_resets(queue: FakeQueue) -> None:
    """app_id and error_like narrow the set; requeue clears attempts, lease and last_error."""
    d2, d3 = "cd" * 32, "ef" * 32
    _dead(queue, DIGEST, "app.a", "S3 PUT returned 403 Forbidden", stale_reclaims=4)
    _dead(queue, d2, "app.b", "S3 PUT returned 403 Forbidden")
    _dead(queue, d3, "app.a", "wasm trap")
    assert (await svc.requeue_failed(None, app_id="app.a")).matched == 2
    assert (await svc.requeue_failed(None, error_like="forbidden")).matched == 2
    both = await svc.requeue_failed(None, app_id="app.a", error_like="403", dry_run=False)
    assert (both.matched, both.requeued, both.dry_run) == (1, 1, False)
    assert queue.pending[DIGEST]["attempts"] == 0
    assert queue.pending[DIGEST]["stale_reclaims"] == 0
    assert queue.pending[DIGEST]["last_error"] is None
    assert queue.pending[DIGEST]["claimed_at"] is None
    assert queue.pending[d2]["attempts"] == 5 and queue.pending[d3]["attempts"] == 5
    assert (await svc.requeue_failed(None, error_like="nomatch")).matched == 0


async def test_requeue_failed_cap_fails_loudly_before_writing(queue: FakeQueue) -> None:
    """More matches than the cap raises and writes nothing (no partial requeue)."""
    for i in range(3):
        _dead(queue, f"{i:064x}", "app.a", "403")
    with pytest.raises(svc.RequeueCapExceededError):
        await svc.requeue_failed(None, cap=2, dry_run=False)
    assert all(p["attempts"] == 5 for p in queue.pending.values())
    assert (await svc.requeue_failed(None, cap=3, dry_run=False)).requeued == 3
    with pytest.raises(ValueError):
        await svc.requeue_failed(None, cap=0)


def test_like_pattern_escapes_wildcards() -> None:
    """Operator text is a literal substring: %, _ and backslash are escaped."""
    assert svc._like_pattern(None) is None
    assert svc._like_pattern("50%_x\\y") == "%50\\%\\_x\\\\y%"


@pytest.mark.parametrize(
    ("argv", "apply"),
    [
        (["requeue-failed"], False),
        (["requeue-failed", "--dry-run"], False),
        (["requeue-failed", "--yes", "--dry-run"], False),
        (["requeue-failed", "--yes"], True),
    ],
)
async def test_cli_requeue_failed_requires_yes(
    queue: FakeQueue, argv: list[str], apply: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only --yes (without --dry-run) writes; otherwise the count is printed."""
    _dead(queue, DIGEST, "a", "403")
    args = precompile_queue._parser().parse_args([*argv, "--error-like", "403"])
    rc = await precompile_queue._requeue_failed(
        object(),
        app_id=args.app_id,
        error_like=args.error_like,
        cap=args.cap,
        apply=args.yes and not args.dry_run,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert (queue.pending[DIGEST]["attempts"] == 0) is apply
    assert ("requeued 1 of 1" in out) is apply
    assert ("dry-run: 1" in out) is (not apply)


async def test_cli_requeue_failed_cap_exit_code(
    queue: FakeQueue, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exceeding --max exits 2 with a REFUSED message and changes nothing."""
    for i in range(3):
        _dead(queue, f"{i:064x}", "a", "403")
    rc = await precompile_queue._requeue_failed(
        object(), app_id=None, error_like=None, cap=2, apply=True
    )
    assert rc == 2
    assert "REFUSED" in capsys.readouterr().err
    assert all(p["attempts"] == 5 for p in queue.pending.values())


def test_cli_enqueue_all_active_revive_flag_parses() -> None:
    """The Helm Job's `--revive-dead-letters` reaches enqueue-all-active."""
    parser = precompile_queue._parser()
    assert parser.parse_args(["enqueue-all-active"]).revive_dead_letters is False
    assert parser.parse_args(["enqueue-all-active", "--revive-dead-letters"]).revive_dead_letters
