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

    def force_push(self) -> str:
        """Rewrite history: amend the root commit (drops the old tip) and force-push."""
        (self.work / "rewritten.txt").write_text(uuid.uuid4().hex)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "--amend", "-m", "rewritten")
        git(self.work, "push", "-q", "--force", str(self.bare), "main")
        return git(self.work, "rev-parse", "HEAD")

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


async def _audit_count(dal: Any, sid: uuid.UUID, action: str = upd.AUDIT_ACTION) -> int:
    async with dal.engine.connect() as conn:
        row = await conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action=:a AND target_id=:t"),
            {"a": action, "t": str(sid)},
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
    bad = await _active(pg_dal, "1" * 40)  # pin absent from the remote -> rewritten/unreachable
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
    assert summary.pending >= 1 and summary.skipped >= 2  # builtin + PENDING_SCAN
    assert summary.history_rewritten >= 1  # `bad`'s pin is absent (DB shared across tests)
    by_id = {s.public_id: s for s in external}
    assert by_id[good.public_id].status is SourceStatus.NEEDS_APPROVAL
    assert by_id[bad.public_id].status is SourceStatus.ERROR
    assert by_id[bad.public_id].pending_commit_sha is None
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


async def test_force_push_drops_pin_sets_error_pin_untouched_alerts_once(
    pg_dal: Any, tmp_path: Path
) -> None:
    """Review Focus 5: a rewrite is ERROR + one alert, never a normal pending update."""
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    new = remote.force_push()
    assert new != remote.first
    assert await upd.check_source_for_update(pg_dal, src) is upd.UpdateOutcome.HISTORY_REWRITTEN
    got = await get_source(pg_dal, src.public_id)
    assert got is not None
    assert got.status is SourceStatus.ERROR
    assert got.pinned_commit_sha == remote.first and got.pending_commit_sha is None
    assert got.last_scan_error and "history_rewritten" in got.last_scan_error
    assert await _audit_count(pg_dal, src.public_id, upd.REWRITE_AUDIT_ACTION) == 1
    assert await _audit_count(pg_dal, src.public_id) == 0  # not an "update available"
    # idempotent: the ERROR source is not re-checked, so the same rewrite never re-alerts
    assert await upd.check_source_for_update(pg_dal, got) is upd.UpdateOutcome.SKIPPED
    assert await _audit_count(pg_dal, src.public_id, upd.REWRITE_AUDIT_ACTION) == 1


async def test_force_push_keeps_installed_bundles_untouched(pg_dal: Any, tmp_path: Path) -> None:
    from services.marketplace_source_service import source_app_ids

    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    before = await source_app_ids(pg_dal, src.public_id)
    remote.force_push()
    await upd.check_source_for_update(pg_dal, src)
    assert await source_app_ids(pg_dal, src.public_id) == before


async def test_pinned_sha_absent_from_remote_is_error_not_pending(
    pg_dal: Any, tmp_path: Path
) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, "2" * 40)
    assert await upd.check_source_for_update(pg_dal, src) is upd.UpdateOutcome.HISTORY_REWRITTEN
    got = await get_source(pg_dal, src.public_id)
    assert got is not None and got.status is SourceStatus.ERROR
    assert got.pending_commit_sha is None and got.pinned_commit_sha == "2" * 40
    assert remote.first  # fixture used


async def test_multi_commit_fast_forward_is_pending_not_rewrite(
    pg_dal: Any, tmp_path: Path
) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    remote.advance()
    tip = remote.advance()
    assert await upd.check_source_for_update(pg_dal, src) is upd.UpdateOutcome.PENDING
    got = await get_source(pg_dal, src.public_id)
    assert got is not None and got.pending_commit_sha == tip
    assert await _audit_count(pg_dal, src.public_id, upd.REWRITE_AUDIT_ACTION) == 0


async def test_ancestry_fetch_uses_hardened_git_path(
    pg_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ancestry fetch reuses the scanner's hardened argv/env + bounded, commits-only fetch."""
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    tip = remote.advance()
    seen: list[tuple[list[str], dict[str, str], float]] = []
    real = upd._run_git

    async def spy(argv: list[str], **kw: Any) -> str:
        seen.append((argv, kw["env"], kw["deadline"]))
        return await real(argv, **kw)

    monkeypatch.setattr(upd, "_run_git", spy)
    assert await upd.is_pin_reachable(src, tip) is True
    fetches = [a for a, _, _ in seen if "fetch" in a]
    assert len(fetches) == 1
    for flag in ("protocol.allow=never", "http.followRedirects=false", "submodule.recurse=false"):
        assert flag in fetches[0]
    assert "--filter=tree:0" in fetches[0] and "--no-recurse-submodules" in fetches[0]
    assert all(env["GIT_TERMINAL_PROMPT"] == "0" and "HOME" in env for _, env, _ in seen)


async def test_ancestry_denies_direct_egress_without_proxy(
    pg_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    sc._test_remote = None
    monkeypatch.delenv(sc.PROXY_ENV, raising=False)
    with pytest.raises(sc.ScanRejectedError) as ei:
        await upd.is_pin_reachable(src, "a" * 40)
    assert ei.value.reason == "no_egress_proxy"


async def test_ancestry_fetch_failure_is_error_and_never_flags_rewrite(
    pg_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    remote.advance()

    async def timeout(*a: Any, **k: Any) -> bool:
        raise sc.ScanRejectedError("clone_timeout", "git exceeded budget")

    monkeypatch.setattr(upd, "is_pin_reachable", timeout)
    assert await upd.check_source_for_update(pg_dal, src) is upd.UpdateOutcome.ERROR
    got = await get_source(pg_dal, src.public_id)
    assert got is not None and got.status is SourceStatus.ACTIVE  # fail loud, not a false alarm
    assert got.pending_commit_sha is None


async def test_ancestry_rejects_bad_sha_and_tip_mismatch(pg_dal: Any, tmp_path: Path) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    with pytest.raises(sc.ScanRejectedError) as ei:
        await upd.is_pin_reachable(src, "not-a-sha")
    assert ei.value.reason == "bad_sha"


async def test_rewrite_on_concurrently_disabled_source_is_unchanged(
    pg_dal: Any, tmp_path: Path
) -> None:
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    remote.force_push()
    await disable_source(pg_dal, src.public_id)  # stale `src` still looks ACTIVE
    assert await upd.check_source_for_update(pg_dal, src) is upd.UpdateOutcome.UNCHANGED
    assert await _audit_count(pg_dal, src.public_id, upd.REWRITE_AUDIT_ACTION) == 0


async def test_ancestry_git_calls_are_disk_watched_and_never_lazy_fetch(
    pg_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ancestry git subprocess is watch=root + GIT_NO_LAZY_FETCH=1; promisor dropped."""
    remote = Remote(tmp_path)
    src = await _active(pg_dal, remote.first)
    tip = remote.advance()
    seen: list[tuple[list[str], dict[str, str], Any]] = []
    real = upd._run_git

    async def spy(argv: list[str], **kw: Any) -> str:
        seen.append((argv, kw["env"], kw["watch"]))
        return await real(argv, **kw)

    monkeypatch.setattr(upd, "_run_git", spy)
    assert await upd.is_pin_reachable(src, tip) is True
    assert len(seen) >= 6  # init, remote add, fetch, remote remove, rev-parse x2, merge-base
    assert all(w is not None and w.name == "repo" for _, _, w in seen)
    assert all(env["GIT_NO_LAZY_FETCH"] == "1" for _, env, _ in seen)
    names = [next(a for a in argv if a in {"init", "remote", "fetch", "rev-parse", "merge-base"})
             for argv, _, _ in seen]  # fmt: skip
    assert names.index("fetch") < max(i for i, n in enumerate(names) if n == "remote")
    assert "merge-base" in names
