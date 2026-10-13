"""Failure-path coverage for `event_discord_sync_service`: rollback-and-reraise, backstop, main.

Real sqlite-backed DAL (`event_sync_db`) with a thin wrapper whose `commit()` raises, so the
write helpers' rollback + re-raise branches (data-integrity critical: a failed commit must
roll back and surface, never be swallowed) execute for real.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import event_discord_sync_service as svc
from tests.test_event_discord_sync_service import (
    _DISCORD_EVENT_ID,
    _FakeCredentialResolver,
    _FakeDiscordClient,
    _make_pairing,
    _targets,
)


class _CommitFailsDal:
    """Delegates everything to the real DAL but `commit()` raises; records `rollback()`."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.rollbacks = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner(*args, **kwargs)

    def commit(self) -> None:
        raise RuntimeError("commit exploded")

    def rollback(self) -> None:
        self.rollbacks += 1
        self._inner.rollback()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Flag ON by default."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "feature_enabled", stub)
    return stub


class TestWriteHelpersRollBackAndReraise:
    def test_update_sync_row_rolls_back_and_reraises(self, event_sync_db: Any) -> None:
        dal, community_id, _t, _g, event_id = event_sync_db
        pairing = _make_pairing(dal, community_id)
        row = svc._get_or_create_sync_row(
            dal, event_id=event_id, pairing_id=pairing.id, discord_guild_id="1"
        )
        bad = _CommitFailsDal(dal)
        with pytest.raises(RuntimeError, match="commit exploded"):
            svc._update_sync_row(bad, row.id, sync_status="synced", sync_error=None)
        assert bad.rollbacks == 1

    def test_persist_aggregate_rolls_back_and_reraises(self, event_sync_db: Any) -> None:
        dal, _c, _t, _g, event_id = event_sync_db
        bad = _CommitFailsDal(dal)
        with pytest.raises(RuntimeError, match="commit exploded"):
            svc._persist_calendar_event_aggregate(
                bad, event_id, discord_event_id=None, sync_status="synced", sync_error=None
            )
        assert bad.rollbacks == 1

    def test_get_or_create_rolls_back_on_insert_commit_failure(self, event_sync_db: Any) -> None:
        dal, community_id, _t, _g, event_id = event_sync_db
        pairing = _make_pairing(dal, community_id)
        bad = _CommitFailsDal(dal)
        with pytest.raises(RuntimeError, match="commit exploded"):
            svc._get_or_create_sync_row(
                bad, event_id=event_id, pairing_id=pairing.id, discord_guild_id="1"
            )
        assert bad.rollbacks == 1

    def test_get_or_create_is_idempotent(self, event_sync_db: Any) -> None:
        dal, community_id, _t, _g, event_id = event_sync_db
        pairing = _make_pairing(dal, community_id)
        a = svc._get_or_create_sync_row(
            dal, event_id=event_id, pairing_id=pairing.id, discord_guild_id="1"
        )
        b = svc._get_or_create_sync_row(
            dal, event_id=event_id, pairing_id=pairing.id, discord_guild_id="1"
        )
        assert a.id == b.id

    def test_update_sync_row_clear_flag_nulls_discord_event_id(self, event_sync_db: Any) -> None:
        dal, community_id, _t, _g, event_id = event_sync_db
        pairing = _make_pairing(dal, community_id)
        row = svc._get_or_create_sync_row(
            dal, event_id=event_id, pairing_id=pairing.id, discord_guild_id="1"
        )
        svc._update_sync_row(
            dal, row.id, sync_status="synced", sync_error=None, discord_event_id="77"
        )
        assert (
            dal(dal.calendar_event_discord_syncs.id == row.id).select().first().discord_event_id
            == "77"
        )
        svc._update_sync_row(
            dal, row.id, sync_status="pending", sync_error=None, clear_discord_event_id=True
        )
        after = dal(dal.calendar_event_discord_syncs.id == row.id).select().first()
        assert after.discord_event_id is None
        assert after.sync_status == "pending"


class TestSyncEventBackstop:
    async def test_persistence_failure_inside_backstop_never_escapes(
        self, event_sync_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, community_id, _t, _g, event_id = event_sync_db
        _make_pairing(dal, community_id)
        event_row = dal(dal.calendar_events.id == event_id).select().first()

        class _Exploding:
            platform = "discord"

            async def push(self, dal: Any, event_row: Any, *, action: Any) -> Any:
                raise ValueError("target bug")

        def _boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(svc, "_persist_calendar_event_aggregate", _boom)
        result = await svc.sync_event(dal, event_row, action="update", targets=[_Exploding()])
        assert result.sync_status == "sync_error"
        assert result.sync_error == "unexpected_error"

    async def test_cancel_failure_on_one_guild_marks_error_but_other_still_cancels(
        self, event_sync_db: Any
    ) -> None:
        dal, community_id, _t, _g, event_id = event_sync_db
        p1 = _make_pairing(dal, community_id, guild_id="111111111111111111")
        p2 = _make_pairing(dal, community_id, guild_id="222222222222222222")
        for p, eid in ((p1, "e1"), (p2, "e2")):
            row = svc._get_or_create_sync_row(
                dal, event_id=event_id, pairing_id=p.id, discord_guild_id=p.discord_guild_id
            )
            svc._update_sync_row(
                dal, row.id, sync_status="synced", sync_error=None, discord_event_id=eid
            )

        class _Client(_FakeDiscordClient):
            async def cancel_scheduled_event(self, *, guild_id: str, discord_event_id: str) -> None:
                if discord_event_id == "e1":
                    raise svc.DiscordEventSyncError("rate limited (429)")
                await super().cancel_scheduled_event(
                    guild_id=guild_id, discord_event_id=discord_event_id
                )

        client = _Client()
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        result = await svc.sync_event(dal, event_row, action="cancel", targets=_targets(client))
        assert result.sync_status == "sync_error"
        assert [c[1] for c in client.cancelled] == ["e2"]
        s1 = dal(dal.calendar_event_discord_syncs.discord_event_id == "e1").select().first()
        assert s1.sync_status == "sync_error"

    async def test_unexpected_per_guild_exception_is_contained_and_recorded(
        self, event_sync_db: Any
    ) -> None:
        dal, community_id, _t, _g, event_id = event_sync_db
        _make_pairing(dal, community_id)
        client = _FakeDiscordClient(raise_on_create=KeyError("bug"))
        event_row = dal(dal.calendar_events.id == event_id).select().first()
        result = await svc.sync_event(dal, event_row, action="create", targets=_targets(client))
        assert result.sync_status == "sync_error"
        assert result.sync_error == "unexpected_error"
        assert _DISCORD_EVENT_ID not in str(result)


class TestBatchBackoffAndMain:
    async def test_main_prints_denominators(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_dal = object()

        async def _install() -> Any:
            return SimpleNamespace(dal=fake_dal)

        async def _batch(dal: Any, **kw: Any) -> svc.ReconcileSummary:
            assert dal is fake_dal
            return svc.ReconcileSummary(events_examined=5, events_synced=4, events_failed=1)

        monkeypatch.setattr(svc, "_build_install_dal", _install)
        monkeypatch.setattr(svc, "run_event_sync_reconcile_batch", _batch)
        assert await svc.main() == 0
        out = capsys.readouterr().out
        assert "events_examined=5" in out
        assert "events_failed=1" in out

    async def test_batch_default_http_client_path_with_no_events(self, event_sync_db: Any) -> None:
        dal, _c, _t, _g, event_id = event_sync_db
        dal(dal.calendar_events.id == event_id).update(status="pending")
        dal.commit()
        summary = await svc.run_event_sync_reconcile_batch(
            dal, credential_resolver=_FakeCredentialResolver()
        )
        assert summary.events_examined == 0
        assert summary.events_synced == 0
