"""Integrity regression tests for the `loyalty` bundle (fix/bundle-defects-wave).

Each class pins one defect found in the 2026-10-10 review of the ledger: lost first-time grants
under concurrency, unbounded/overflowing amounts, misleading clamped replies, junk rows from
`sub`, the actor-vs-target identity mismatch, and the string-badge mod-gate bypass. Everything
runs the real `transform`/`dispatch` and the real `waddle_sdk` over the fake WIT host in
`test_app.py`.
"""

# F811: the `fake_host` pytest fixture is re-exported from `test_app.py` and re-bound by parameters.
# S105: `_SECRET` is a log-leak canary string, not a credential.
# ruff: noqa: F811, S105
from __future__ import annotations

import json
from typing import Any

import pytest
import test_app
from test_app import _FakeHost, _go, _run, _sample_event, _scoped, fake_host  # noqa: F401
from waddle_sdk.community_kv import TENANT_WIDE_SENTINEL  # noqa: F401 -- documents the scope

import app
from app import (
    MAX_ADJUST_AMOUNT,
    MAX_BALANCE,
    _actor_pseudonym,
    _caller_role_signal,
    _claim_key,
    _index_key,
    _pseudonym,
    dispatch,
    transform,
)

_PS_ALICE = _pseudonym("alice")


def _reply(host: _FakeHost) -> str:
    return test_app._reply_text(host)


def _balances(host: _FakeHost) -> list[int]:
    return [int(r["balance"]) for r in host.db.rows.values()]


# mod gate: string badge bypass


class TestModGateStringBadge:
    """`bool("false")` is True: a non-mod whose normalizer sent a string badge passed the gate."""

    @pytest.mark.parametrize("badge", ["false", "False", "0", "no", "", "true", "1", 0, 1])
    def test_caller_role_signal_never_trusts_a_non_boolean(self, badge: Any) -> None:
        signal = _caller_role_signal({"is_mod": badge, "is_broadcaster": badge})
        assert signal is False

    @pytest.mark.parametrize(
        "payload",
        [
            {"is_mod": "false", "is_broadcaster": False},
            {"is_mod": False, "is_broadcaster": "false"},
            {"is_mod": "0"},
            {"is_broadcaster": "no"},
        ],
    )
    def test_mixed_string_and_false_badges_are_denied(self, payload: dict[str, Any]) -> None:
        assert _caller_role_signal(payload) is False

    def test_string_false_mod_is_rejected_end_to_end_and_writes_nothing(
        self, fake_host: _FakeHost
    ) -> None:
        event = _sample_event("!points add 500 alice", is_mod="false", is_broadcaster="false")  # type: ignore[arg-type]
        out = _run(transform(event))
        assert out is not None
        assert out.payload["is_mod"] is False and out.payload["is_broadcaster"] is False
        envelope = test_app._sample_envelope(
            "twitch", "add", target="alice", amount=500, is_mod=out.payload["is_mod"]
        )
        _run(dispatch(envelope, {}, http_client=None))
        assert _reply(fake_host) == app._PERMISSION_DENIED_MSG
        assert fake_host.db.rows == {} and fake_host.kv_calls == []

    def test_a_real_mod_still_passes(self, fake_host: _FakeHost) -> None:
        _go("add", target="alice", amount=5, role=True)
        assert _balances(fake_host) == [5]


# first-time row creation race


class TestFirstGrantRace:
    """Two concurrent first-time grants used to insert two rows; one user's points vanished."""

    def test_claim_loser_never_inserts_a_duplicate_row(self, fake_host: _FakeHost) -> None:
        # Another invocation already holds the creation claim and has not published its index.
        fake_host.kv_store[_scoped(_claim_key(_PS_ALICE))] = b"1"
        _go("add", target="alice", amount=7, role=True)
        assert fake_host.db.rows == {}, "loser must not create a second row"
        assert _reply(fake_host) == app._BUSY_MSG
        assert any(m == "loyalty.row_creation_busy" for _l, m, _f in fake_host.log_calls), (
            "busy is surfaced, not silent"
        )

    def test_claim_loser_applies_to_the_winners_row_once_the_index_appears(
        self, fake_host: _FakeHost
    ) -> None:
        """Winner publishes its row between the loser's claim and a re-check: points add up."""
        fake_host.kv_store[_scoped(_claim_key(_PS_ALICE))] = b"1"
        winner_row = fake_host.db.insert(
            [
                test_app.wit_fake_db.ColumnValue(
                    "actor_hash", test_app.wit_fake_db.wrap(_PS_ALICE)
                ),
                test_app.wit_fake_db.ColumnValue("balance", test_app.wit_fake_db.wrap(10)),
            ]
        )
        reads = {"n": 0}
        index_key = _scoped(_index_key(_PS_ALICE))

        class _Store(dict[str, bytes]):
            def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
                if key == index_key:
                    reads["n"] += 1
                    if reads["n"] == 3:  # the winner publishes during the loser's re-checks
                        self[key] = winner_row.row_id.encode()
                return super().get(key, default)

        store = _Store(fake_host.kv_store)
        fake_host.kv_store = store
        # the fake kv closures capture `host.kv_store` attribute lookups, so rebinding works
        _go("add", target="alice", amount=7, role=True)
        assert _balances(fake_host) == [17]
        assert len(fake_host.db.rows) == 1

    def test_winner_creates_exactly_one_row_and_publishes_the_index(
        self, fake_host: _FakeHost
    ) -> None:
        _go("add", target="alice", amount=7, role=True)
        assert len(fake_host.db.rows) == 1
        assert fake_host.kv_store[_scoped(_index_key(_PS_ALICE))].decode() in fake_host.db.rows
        _go("add", target="alice", amount=3, role=True)
        assert _balances(fake_host) == [10], "second grant takes the update path"

    def test_claim_is_taken_atomically_with_a_short_ttl(self, fake_host: _FakeHost) -> None:
        _go("add", target="alice", amount=7, role=True)
        claims = [c for c in fake_host.kv_calls if c[0] == "increment"]
        assert len(claims) == 1
        _op, key, delta, ttl = claims[0]
        assert key == _scoped(_claim_key(_PS_ALICE)) and delta == 1
        assert 0 < ttl <= 60, "a crashed creator must not wedge a user for long"

    def test_failed_insert_releases_the_claim_so_the_retry_works(
        self, fake_host: _FakeHost
    ) -> None:
        fake_host.db.raise_on["insert"] = test_app.wit_fake_db.WitDbError(
            test_app.wit_fake_db.Error_Backend("down")
        )
        with pytest.raises(RuntimeError):
            _go("add", target="alice", amount=7, role=True)
        assert _scoped(_claim_key(_PS_ALICE)) not in fake_host.kv_store
        del fake_host.db.raise_on["insert"]
        _go("add", target="alice", amount=7, role=True)
        assert _balances(fake_host) == [7]

    def test_failed_index_write_releases_the_claim(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        kv_mod = sys.modules["wit_world"].imports.kv
        real_set = kv_mod.set

        def _boom(key: str, value: bytes, ttl: int) -> None:
            if "loyalty.rowid" in key:
                raise OSError("kv down")
            real_set(key, value, ttl)

        monkeypatch.setattr(kv_mod, "set", _boom)
        with pytest.raises(RuntimeError):
            _go("add", target="alice", amount=7, role=True)
        assert _scoped(_claim_key(_PS_ALICE)) not in fake_host.kv_store

    def test_a_failed_claim_release_is_logged_not_swallowed(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        kv_mod = sys.modules["wit_world"].imports.kv

        def _boom(key: str) -> None:
            raise OSError("kv down")

        monkeypatch.setattr(kv_mod, "delete", _boom)
        fake_host.db.raise_on["insert"] = test_app.wit_fake_db.WitDbError(
            test_app.wit_fake_db.Error_Backend("down")
        )
        with pytest.raises(RuntimeError):
            _go("add", target="alice", amount=7, role=True)
        assert any(m == "loyalty.claim_release_failed" for _l, m, _f in fake_host.log_calls)

    def test_claim_increment_failure_is_loud(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        kv_mod = sys.modules["wit_world"].imports.kv

        def _boom(key: str, delta: int, ttl: int) -> int:
            raise OSError("kv down")

        monkeypatch.setattr(kv_mod, "increment", _boom)
        with pytest.raises(RuntimeError, match="kv_claim"):
            _go("add", target="alice", amount=7, role=True)
        assert fake_host.db.rows == {}


# bounds + never negative


class TestBoundsAndClamping:
    @pytest.mark.parametrize(
        "text",
        [
            "!points add 1000000001 alice",
            "!points add 99999999999999999999999999 alice",
            "!points add +5 alice",
            "!points add 1_000 alice",
            "!points add 0 alice",
            "!points add -5 alice",
            "!points add ٣ alice",
        ],
    )
    def test_out_of_bounds_or_odd_amounts_are_usage_errors_that_never_reach_dispatch(
        self, fake_host: _FakeHost, text: str
    ) -> None:
        out = _run(transform(_sample_event(text, is_mod=True)))
        assert out is not None and out.payload["command"] == "usage"

    def test_the_maximum_single_adjustment_is_accepted(self, fake_host: _FakeHost) -> None:
        out = _run(transform(_sample_event(f"!points add {MAX_ADJUST_AMOUNT} alice", is_mod=True)))
        assert out is not None and out.payload["amount"] == MAX_ADJUST_AMOUNT

    @pytest.mark.parametrize("amount", [0, -5, MAX_ADJUST_AMOUNT + 1, True, "5", 5.5])
    def test_dispatch_rejects_a_malformed_amount_payload_without_touching_state(
        self, fake_host: _FakeHost, amount: Any
    ) -> None:
        with pytest.raises(ValueError, match="malformed"):
            _go("add", target="alice", amount=amount, role=True)
        assert fake_host.db.rows == {} and fake_host.kv_calls == []

    def test_negative_add_payload_cannot_act_as_a_sub(self, fake_host: _FakeHost) -> None:
        _go("add", target="alice", amount=50, role=True)
        with pytest.raises(ValueError, match="malformed"):
            _go("add", target="alice", amount=-50, role=True)
        assert _balances(fake_host) == [50]

    def test_balance_saturates_at_the_cap_and_the_reply_says_so(self, fake_host: _FakeHost) -> None:
        _go("add", target="alice", amount=5, role=True)
        (row,) = fake_host.db.rows.values()
        row["balance"] = MAX_BALANCE - 3
        _go("add", target="alice", amount=10, role=True)
        assert _balances(fake_host) == [MAX_BALANCE]
        assert "Added 3 of 10 points" in _reply(fake_host)
        assert "balance cap reached" in _reply(fake_host)

    def test_a_clamped_sub_reports_the_amount_actually_removed(self, fake_host: _FakeHost) -> None:
        _go("add", target="alice", amount=5, role=True)
        _go("sub", target="alice", amount=999, role=True)
        assert _balances(fake_host) == [0]
        assert "Removed 5 of 999 points" in _reply(fake_host)
        assert "can't go below 0" in _reply(fake_host)

    def test_an_exact_sub_reports_the_full_amount(self, fake_host: _FakeHost) -> None:
        _go("add", target="alice", amount=5, role=True)
        _go("sub", target="alice", amount=5, role=True)
        assert _reply(fake_host) == "Removed 5 points from alice. New balance: 0."

    def test_balance_never_goes_negative_across_a_mixed_sequence(
        self, fake_host: _FakeHost
    ) -> None:
        for verb, amount in [("add", 10), ("sub", 25), ("sub", 1), ("add", 3), ("sub", 4)]:
            _go(verb, target="alice", amount=amount, role=True)
            assert all(b >= 0 for b in _balances(fake_host))
        assert _balances(fake_host) == [0]

    def test_first_ever_grant_is_capped_too(self, fake_host: _FakeHost) -> None:
        _go("add", target="alice", amount=MAX_ADJUST_AMOUNT, role=True)
        assert _balances(fake_host) == [MAX_ADJUST_AMOUNT]
        assert MAX_ADJUST_AMOUNT < MAX_BALANCE < 2**53, "stays exact for JSON consumers"

    @pytest.mark.parametrize("stored", [-1, MAX_BALANCE + 1, "abc", None])
    def test_corrupt_stored_balance_fails_loud_on_every_read_path(
        self, fake_host: _FakeHost, stored: Any
    ) -> None:
        _go("add", target="alice", amount=5, role=True)
        (row,) = fake_host.db.rows.values()
        row["balance"] = stored
        for command, kwargs in [
            ("balance_other", {"target": "alice"}),
            ("add", {"target": "alice", "amount": 1, "role": True}),
            ("leaderboard", {}),
        ]:
            before = len(fake_host.relay_calls)
            with pytest.raises(RuntimeError, match="balance_invalid"):
                _go(command, **kwargs)
            texts = [json.loads(m)["text"] for _p, m in fake_host.relay_calls[before:]]
            assert texts == [app._UNAVAILABLE_MSG]
        assert row["balance"] == stored, "corruption is never silently 'repaired'"


class TestSubOnMissingUser:
    def test_sub_creates_no_row_claim_or_index(self, fake_host: _FakeHost) -> None:
        _go("sub", target="ghost", amount=10, role=True)
        assert _reply(fake_host) == "ghost has 0 points; nothing to remove."
        assert fake_host.db.rows == {}
        assert not any(c[0] in ("increment", "set") for c in fake_host.kv_calls)

    def test_leaderboard_is_not_polluted_by_sub_typos(self, fake_host: _FakeHost) -> None:
        _go("add", target="alice", amount=9, role=True)
        _go("sub", target="alcie", amount=1, role=True)
        _go("leaderboard")
        assert _reply(fake_host).count("player-") == 1


# one identity form


class TestIdentityNormalization:
    @pytest.mark.parametrize("actor", ["Alice", "ALICE", "@Alice", "  alice  "])
    def test_a_users_own_balance_matches_what_a_mod_granted_to_the_typed_name(
        self, fake_host: _FakeHost, actor: str
    ) -> None:
        _go("add", target="alice", amount=12, role=True)
        _go("balance_self", actor=actor)
        assert "has 12 points" in _reply(fake_host)

    def test_a_mod_typing_any_casing_hits_the_same_row(self, fake_host: _FakeHost) -> None:
        _go("add", target="Alice", amount=1, role=True)
        _go("add", target="@ALICE", amount=1, role=True)
        _go("add", target="alice", amount=1, role=True)
        assert _balances(fake_host) == [3] and len(fake_host.db.rows) == 1

    def test_actor_pseudonym_matches_target_pseudonym(self) -> None:
        assert _actor_pseudonym("Alice") == _PS_ALICE
        assert _actor_pseudonym(None) == _pseudonym("anonymous") == _actor_pseudonym("")


# PII-free logging for the new log lines


class TestNewLogLinesArePiiFree:
    _SECRET = "sentinelpii9f3a"

    def test_busy_and_release_lines_carry_no_user_input(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_host.kv_store[_scoped(_claim_key(_pseudonym(self._SECRET)))] = b"1"
        _go("add", target=self._SECRET, amount=5, role=True, actor=self._SECRET)
        _go("sub", target=self._SECRET, amount=5, role=True, actor=self._SECRET)
        out = _run(transform(_sample_event(f"!points add 99999999999 {self._SECRET}", is_mod=True)))
        assert out is not None
        assert fake_host.log_calls, "denominator: the scenario must log"
        for _lvl, message, fields in fake_host.log_calls:
            assert self._SECRET not in message and self._SECRET not in fields
            assert message in test_app._ALLOWED_LOG_FIELDS, message
            assert set(json.loads(fields)) <= test_app._ALLOWED_LOG_FIELDS[message]


class TestNoOrphanRows:
    """An inserted row whose index write failed is unreachable -- it must not keep the points."""

    def _fail_index_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        kv_mod = sys.modules["wit_world"].imports.kv
        real_set = kv_mod.set

        def _set(key: str, value: bytes, ttl: int) -> None:
            if "loyalty.rowid" in key:
                raise OSError("kv down")
            real_set(key, value, ttl)

        monkeypatch.setattr(kv_mod, "set", _set)

    def test_index_write_failure_deletes_the_orphan_row(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fail_index_write(monkeypatch)
        with pytest.raises(RuntimeError, match="kv_set"):
            _go("add", target="alice", amount=7, role=True)
        assert fake_host.db.rows == {}, "the unindexed row must not survive"

    def test_a_retry_after_the_failure_yields_exactly_one_row(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with monkeypatch.context() as patch:
            self._fail_index_write(patch)
            with pytest.raises(RuntimeError):
                _go("add", target="alice", amount=7, role=True)
        _go("add", target="alice", amount=7, role=True)
        assert _balances(fake_host) == [7]
        _go("leaderboard")
        assert _reply(fake_host).count("player-") == 1

    def test_a_failed_orphan_cleanup_is_logged_not_swallowed(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fail_index_write(monkeypatch)
        fake_host.db.raise_on["delete"] = test_app.wit_fake_db.WitDbError(
            test_app.wit_fake_db.Error_Backend("down")
        )
        with pytest.raises(RuntimeError, match="kv_set"):
            _go("add", target="alice", amount=7, role=True)
        assert any(m == "loyalty.orphan_cleanup_failed" for _l, m, _f in fake_host.log_calls)
