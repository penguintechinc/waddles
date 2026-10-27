"""Marketplace source auto-update detection (Task 8): REAL Postgres + a real local git remote.

A SKIP means docker is absent -- treat it as a gate FAILURE, never a pass.
"""

# ruff: noqa: F811  -- imported pytest fixtures (`pg_db`/`pg_dal`) are re-bound as test params

from __future__ import annotations

import base64
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from services import marketplace_source_scanner as sc
from services import marketplace_source_update as upd
from services.marketplace_publisher_attestation import fingerprint_publisher_key
from services.marketplace_source_service import (
    BuildMode,
    SourceRecord,
    SourceStatus,
    approve_update,
    create_source,
    disable_source,
    get_source,
    list_sources,
    update_status,
)
from tests.malicious_source_fixtures import add_bundle, commit_bare, git
from tests.test_seed_core_bundles_pg_integration import (  # noqa: F401  # fixtures
    pg_dal,
    pg_db,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

_PK = base64.b64encode(b"\x07" * 32).decode()
_FP = fingerprint_publisher_key(_PK)


@pytest.fixture(autouse=True)
def _transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(sc.TEST_TRANSPORT_ENV, "1")

    async def _ok(url: str, *, allowed_schemes: tuple[str, ...]) -> str:
        return url

    monkeypatch.setattr(upd, "validate_outbound_url", _ok)
    yield
    sc._test_remote = None


class Remote:
    """A local bare remote whose `main` can be advanced."""

    def __init__(self, tmp: Path) -> None:
        """Create the work repo, push it to a bare remote and point the scanner transport at it."""
        self.work = tmp / "work"
        self.work.mkdir()
        add_bundle(self.work, "python", "alpha")
        self.bare = tmp / "remote.git"
        self.first = commit_bare(self.work, self.bare)
        sc.enable_test_transport(f"file://{self.bare}")

    def advance(self) -> str:
        (self.work / "bump.txt").write_text(uuid.uuid4().hex)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "next")
        git(self.work, "push", "-q", str(self.bare), "main")
        return git(self.work, "rev-parse", "HEAD")


async def _active(dal: Any, pin: str) -> SourceRecord:
    rec = await create_source(
        dal,
        name="acme",
        git_url=f"https://github.com/acme/{uuid.uuid4().hex[:10]}",
        ref="main",
        build_mode=BuildMode.PREBUILT,
        publisher_key_fingerprint=_FP,
        publisher_public_key=_PK,
        pinned_commit_sha=pin,
    )
    return await update_status(dal, rec.public_id, SourceStatus.ACTIVE, scanned=True)


async def _audit_count(dal: Any, sid: uuid.UUID) -> int:
    async with dal.engine.connect() as conn:
        row = await conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action=:a AND target_id=:t"),
            {"a": upd.AUDIT_ACTION, "t": str(sid)},
        )
        return int(row.scalar_one())


async def test_new_commit_recorded_pending_pin_unchanged_notified_once(
    pg_dal: Any, tmp_path: Path
) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    new = remote.advance()
    assert await upd.check_source_for_update(pg_dal, src) is upd.UpdateOutcome.PENDING
    got = await get_source(pg_dal, src.public_id)
    assert got is not None
    assert (got.pinned_commit_sha, got.pending_commit_sha) == (remote.first, new)
    assert got.status is SourceStatus.NEEDS_APPROVAL
    assert await _audit_count(pg_dal, src.public_id) == 1
    # idempotent re-run: same pending commit -> no second notification
    assert await upd.check_source_for_update(pg_dal, got) is upd.UpdateOutcome.UNCHANGED
    assert await _audit_count(pg_dal, src.public_id) == 1
    from services.marketplace_source_service import source_app_ids

    assert await source_app_ids(pg_dal, src.public_id) == ()


async def test_unchanged_upstream_records_nothing(pg_dal: Any, tmp_path: Path) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    assert await upd.check_source_for_update(pg_dal, src) is upd.UpdateOutcome.UNCHANGED
    got = await get_source(pg_dal, src.public_id)
    assert got is not None and got.pending_commit_sha is None
    assert got.status is SourceStatus.ACTIVE
    assert await _audit_count(pg_dal, src.public_id) == 0


async def test_routine_rescan_status_write_preserves_pending(pg_dal: Any, tmp_path: Path) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    new = remote.advance()
    await upd.check_source_for_update(pg_dal, src)
    # what scan_source does after a successful rescan of the PINNED commit
    after = await update_status(pg_dal, src.public_id, SourceStatus.ACTIVE, scanned=True)
    assert after.pending_commit_sha == new
    assert after.status is SourceStatus.NEEDS_APPROVAL  # not flipped back to ACTIVE
    # an explicit clear is still possible
    cleared = await update_status(
        pg_dal, src.public_id, SourceStatus.ACTIVE, pending_commit_sha=None
    )
    assert cleared.pending_commit_sha is None and cleared.status is SourceStatus.ACTIVE


async def test_newer_commit_supersedes_pending_and_notifies_again(
    pg_dal: Any, tmp_path: Path
) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    remote.advance()
    await upd.check_source_for_update(pg_dal, src)
    newer = remote.advance()
    cur = await get_source(pg_dal, src.public_id)
    assert cur is not None
    assert await upd.check_source_for_update(pg_dal, cur) is upd.UpdateOutcome.PENDING
    got = await get_source(pg_dal, src.public_id)
    assert got is not None and got.pending_commit_sha == newer
    assert got.pinned_commit_sha == remote.first
    assert await _audit_count(pg_dal, src.public_id) == 2


async def test_disabled_source_skipped(pg_dal: Any, tmp_path: Path) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    remote.advance()
    disabled = await disable_source(pg_dal, src.public_id)
    assert await upd.check_source_for_update(pg_dal, disabled) is upd.UpdateOutcome.SKIPPED
    # even a stale (pre-disable) record cannot resurrect it: the atomic UPDATE refuses
    assert await upd.check_source_for_update(pg_dal, src) is upd.UpdateOutcome.UNCHANGED
    got = await get_source(pg_dal, src.public_id)
    assert got is not None and got.status is SourceStatus.DISABLED
    assert got.pending_commit_sha is None


async def test_after_approve_consumes_pending_rescan_records_nothing(
    pg_dal: Any, tmp_path: Path
) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    new = remote.advance()
    await upd.check_source_for_update(pg_dal, src)
    approved = await approve_update(pg_dal, src.public_id, expected_pending_sha=new)
    assert approved.pinned_commit_sha == new and approved.pending_commit_sha is None
    assert await upd.check_source_for_update(pg_dal, approved) is upd.UpdateOutcome.UNCHANGED
    got = await get_source(pg_dal, src.public_id)
    assert got is not None and got.pending_commit_sha is None
    assert got.status is SourceStatus.ACTIVE
    assert await _audit_count(pg_dal, src.public_id) == 1


async def test_rescan_all_counts_and_isolates_failures(pg_dal: Any, tmp_path: Path) -> None:
    remote = Remote(tmp_path)
    good = await _active(pg_dal, remote.first)
    remote.advance()
    bad = await _active(pg_dal, "1" * 40)  # remote has no such ref? same remote -> still resolves
    pending = await create_source(  # PENDING_SCAN: skipped
        pg_dal,
        name="p",
        git_url=f"https://github.com/acme/{uuid.uuid4().hex[:10]}",
        ref="main",
        build_mode=BuildMode.PREBUILT,
        publisher_key_fingerprint=_FP,
        publisher_public_key=_PK,
        pinned_commit_sha=remote.first,
    )
    summary = await upd.rescan_all(pg_dal)
    external = [s for s in await list_sources(pg_dal) if not s.is_builtin]
    assert summary.examined >= 2 and summary.errors == 0
    assert summary.pending >= 2 and summary.skipped >= 2  # builtin + PENDING_SCAN
    by_id = {s.public_id: s for s in external}
    assert by_id[good.public_id].status is SourceStatus.NEEDS_APPROVAL
    assert by_id[bad.public_id].pending_commit_sha is not None
    assert by_id[pending.public_id].status is SourceStatus.PENDING_SCAN


async def test_unresolvable_ref_is_error_not_raise(pg_dal: Any, tmp_path: Path) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    git(remote.work, "push", "-q", str(remote.bare), "main:other")
    async with pg_dal.engine.begin() as conn:
        await conn.execute(
            text("UPDATE marketplace_sources SET ref='nope' WHERE public_id=:p"),
            {"p": src.public_id},
        )
    cur = await get_source(pg_dal, src.public_id)
    assert cur is not None
    assert await upd.check_source_for_update(pg_dal, cur) is upd.UpdateOutcome.ERROR
    got = await get_source(pg_dal, src.public_id)
    assert got is not None and got.pending_commit_sha is None


async def test_no_proxy_fails_closed(
    pg_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    sc._test_remote = None
    monkeypatch.delenv(sc.PROXY_ENV, raising=False)
    with pytest.raises(sc.ScanRejectedError) as ei:
        await upd.resolve_latest_commit(src)
    assert ei.value.reason == "no_egress_proxy"


@pytest.mark.parametrize("ref", ["-x", "a b", "--upload-pack=x", "a;b", ""])
async def test_bad_ref_rejected(ref: str) -> None:
    from tests.malicious_source_fixtures import make_source

    src = make_source("a" * 40)
    object.__setattr__(src, "ref", ref)
    with pytest.raises(sc.ScanRejectedError):
        await upd.resolve_latest_commit(src)


def test_pick_sha_prefers_peeled_tag_then_branch() -> None:
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    out = f"{a}\trefs/heads/v1\n{b}\trefs/tags/v1\n{c}\trefs/tags/v1^{{}}\n"
    assert upd._pick_sha(out, "v1") == c
    assert upd._pick_sha(f"{a}\trefs/heads/v1\n", "v1") == a
    assert upd._pick_sha("", "v1") is None
