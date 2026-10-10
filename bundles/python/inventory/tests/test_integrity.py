"""Integrity regression tests for the `inventory` bundle (fix/bundle-defects-wave).

Each class pins one defect found in the 2026-10-10 review: `give` destroying items when the
credit step fails, the lost-update race on the per-user directory, unbounded item names /
distinct items / quantities, the actor-vs-target identity mismatch, and the string-badge mod-gate
bypass. Everything runs the real `transform`/`dispatch` and the real `waddle_sdk` over the fake
WIT host in `test_app.py`.
"""

# F811: the `fake_host` pytest fixture is re-exported from `test_app.py` and re-bound by parameters.
# S105: `_SECRET` is a log-leak canary string, not a credential.
# ruff: noqa: F811, S105
from __future__ import annotations

import json
import sys
from typing import Any

import pytest
import wit_fake_db
from test_app import (  # noqa: F401 -- fake_host is a pytest fixture re-exported for this module
    _FakeHost,
    _grant,
    _run,
    _sample_envelope,
    _sample_event,
    _scoped,
    fake_host,
)

import app
from app import (
    MAX_ITEM_LEN,
    MAX_ITEMS_PER_USER,
    MAX_QUANTITY,
    _actor_pseudonym,
    _caller_role_signal,
    _dir_key,
    _lock_key,
    _pseudonym,
    dispatch,
    transform,
)

_ALICE = _pseudonym("alice")
_VIEWER = _pseudonym("viewer-1")
_BACKEND = wit_fake_db.WitDbError(wit_fake_db.Error_Backend("down"))


def _reply(host: _FakeHost) -> str:
    return str(json.loads(host.relay_calls[-1][1])["text"])


def _give(host: _FakeHost, item: str, target: str, *, actor: str = "viewer-1") -> Any:
    return _run(
        dispatch(
            _sample_envelope("twitch", "give", item=item, target=target, actor=actor),
            {},
            http_client=None,
        )
    )


def _list(host: _FakeHost, *, actor: str = "viewer-1") -> str:
    _run(dispatch(_sample_envelope("twitch", "list_self", actor=actor), {}, http_client=None))
    return _reply(host)


def _quantity(host: _FakeHost, pseudonym: str, item: str) -> int | None:
    raw = host.kv_store.get(_scoped(_dir_key(pseudonym)))
    if raw is None:
        return None
    row_id = json.loads(raw).get(item)
    return None if row_id is None else int(host.db.rows[row_id]["quantity"])


def _errors(host: _FakeHost) -> list[tuple[str, dict[str, Any]]]:
    return [(m, json.loads(f)) for lvl, m, f in host.log_calls if lvl == "ERROR" or lvl == 0]


# mod gate: string badge bypass


class TestModGateStringBadge:
    """`bool("false")` is True: a non-mod whose normalizer sent a string badge passed the gate."""

    @pytest.mark.parametrize("badge", ["false", "False", "0", "no", "", "true", "1", 0, 1])
    def test_caller_role_signal_never_trusts_a_non_boolean(self, badge: Any) -> None:
        assert _caller_role_signal({"is_mod": badge, "is_broadcaster": badge}) is False

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

    def test_string_false_mod_cannot_add_items_end_to_end(self, fake_host: _FakeHost) -> None:
        event = _sample_event("!inv add sword alice", is_mod="false", is_broadcaster="false")  # type: ignore[arg-type]
        out = _run(transform(event))
        assert out is not None
        assert out.payload["is_mod"] is False and out.payload["is_broadcaster"] is False
        envelope = _sample_envelope(
            "twitch", "add", item="sword", target="alice", is_mod=out.payload["is_mod"]
        )
        _run(dispatch(envelope, {}, http_client=None))
        assert _reply(fake_host) == app._PERMISSION_DENIED_MSG
        assert fake_host.db.rows == {} and fake_host.kv_calls == []

    def test_a_real_mod_still_passes(self, fake_host: _FakeHost) -> None:
        _run(_grant("add", "sword", "alice"))
        assert _quantity(fake_host, _ALICE, "sword") == 1


# give must never destroy an item


class TestGiveIsLossless:
    def _seed_giver(self, host: _FakeHost, quantity: int = 1) -> None:
        for _ in range(quantity):
            _run(_grant("add", "fish", "viewer-1"))

    def test_failed_credit_refunds_the_giver_and_surfaces_one_error(
        self, fake_host: _FakeHost
    ) -> None:
        self._seed_giver(fake_host, 1)
        fake_host.db.raise_on["insert"] = _BACKEND
        before = len(fake_host.relay_calls)
        with pytest.raises(RuntimeError, match="db_insert"):
            _give(fake_host, "fish", "alice")
        assert [json.loads(m)["text"] for _p, m in fake_host.relay_calls[before:]] == [
            app._UNAVAILABLE_MSG
        ], "exactly one chat reply -- the refund must not reply again"
        del fake_host.db.raise_on["insert"]
        assert _quantity(fake_host, _VIEWER, "fish") == 1, "the item is back with the giver"
        assert "fish x1" in _list(fake_host)
        assert _quantity(fake_host, _ALICE, "fish") is None

    def test_failed_credit_keeps_the_gives_remaining_stack_intact(
        self, fake_host: _FakeHost
    ) -> None:
        self._seed_giver(fake_host, 3)
        fake_host.db.raise_on["insert"] = _BACKEND
        with pytest.raises(RuntimeError):
            _give(fake_host, "fish", "alice")
        assert _quantity(fake_host, _VIEWER, "fish") == 3

    def test_a_failed_refund_is_logged_loudly_and_the_original_error_still_raises(
        self, fake_host: _FakeHost
    ) -> None:
        self._seed_giver(fake_host, 1)
        fake_host.db.raise_on["insert"] = _BACKEND
        real_update = fake_host.db.update
        calls = {"n": 0}

        def _update(*args: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:  # 1st = the debit, 2nd = the refund
                raise _BACKEND
            return real_update(*args)

        sys.modules["wit_world"].imports.db.update = _update
        with pytest.raises(RuntimeError, match="db_insert"):
            _give(fake_host, "fish", "alice")
        assert any(m == "inventory.give_refund_failed" for m, _f in _errors(fake_host))

    def test_refund_retries_a_version_conflict(self, fake_host: _FakeHost) -> None:
        self._seed_giver(fake_host, 1)
        fake_host.db.raise_on["insert"] = _BACKEND
        real_update = fake_host.db.update
        calls = {"n": 0}

        def _update(row_id: str, ver: int, cvs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:  # the refund's first attempt loses a race
                fake_host.db.versions[row_id] += 1
            return real_update(row_id, ver, cvs)

        sys.modules["wit_world"].imports.db.update = _update
        with pytest.raises(RuntimeError, match="db_insert"):
            _give(fake_host, "fish", "alice")
        assert _quantity(fake_host, _VIEWER, "fish") == 1

    def test_refund_gives_up_loudly_if_the_row_vanished(self, fake_host: _FakeHost) -> None:
        self._seed_giver(fake_host, 1)
        fake_host.db.raise_on["insert"] = _BACKEND
        real_get = fake_host.db.get
        calls = {"n": 0}

        def _get(row_id: str) -> Any:
            calls["n"] += 1
            if calls["n"] >= 2:  # 1st get = the debit; 2nd = the refund, and the row is gone
                raise wit_fake_db.WitDbError(wit_fake_db.Error_NotFound())
            return real_get(row_id)

        sys.modules["wit_world"].imports.db.get = _get
        with pytest.raises(RuntimeError):
            _give(fake_host, "fish", "alice")
        assert any(
            m == "inventory.give_refund_failed" and f["error"] == "row_missing"
            for m, f in _errors(fake_host)
        )

    def test_refund_gives_up_after_exhausting_conflict_retries(self, fake_host: _FakeHost) -> None:
        self._seed_giver(fake_host, 1)
        fake_host.db.raise_on["insert"] = _BACKEND
        real_update = fake_host.db.update
        calls = {"n": 0}

        def _update(row_id: str, ver: int, cvs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] >= 2:
                fake_host.db.versions[row_id] += 1
            return real_update(row_id, ver, cvs)

        sys.modules["wit_world"].imports.db.update = _update
        with pytest.raises(RuntimeError):
            _give(fake_host, "fish", "alice")
        assert any(
            m == "inventory.give_refund_failed" and f["error"] == "conflict_retries_exhausted"
            for m, f in _errors(fake_host)
        )

    def test_a_kv_outage_during_the_credit_also_refunds(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_giver(fake_host, 1)
        kv_mod = sys.modules["wit_world"].imports.kv
        real_set = kv_mod.set

        def _set(key: str, value: bytes, ttl: int) -> None:
            if key.endswith(_ALICE):
                raise OSError("kv down")
            real_set(key, value, ttl)

        monkeypatch.setattr(kv_mod, "set", _set)
        with pytest.raises(RuntimeError, match="kv_set"):
            _give(fake_host, "fish", "alice")
        assert _quantity(fake_host, _VIEWER, "fish") == 1

    def test_target_at_max_quantity_is_refused_and_the_giver_keeps_the_item(
        self, fake_host: _FakeHost
    ) -> None:
        self._seed_giver(fake_host, 1)
        _run(_grant("add", "fish", "alice"))
        row_id = json.loads(fake_host.kv_store[_scoped(_dir_key(_ALICE))])["fish"]
        fake_host.db.rows[row_id]["quantity"] = MAX_QUANTITY
        result = _give(fake_host, "fish", "alice")
        assert result.detail == "give:limit"
        assert "maximum quantity" in _reply(fake_host)
        assert _quantity(fake_host, _VIEWER, "fish") == 1
        assert _quantity(fake_host, _ALICE, "fish") == MAX_QUANTITY

    def test_target_at_max_distinct_items_is_refused_and_the_giver_keeps_the_item(
        self, fake_host: _FakeHost
    ) -> None:
        self._seed_giver(fake_host, 1)
        _fill_items(fake_host, "alice", MAX_ITEMS_PER_USER)
        result = _give(fake_host, "fish", "alice")
        assert result.detail == "give:limit"
        assert "maximum of 50 distinct items" in _reply(fake_host)
        assert _quantity(fake_host, _VIEWER, "fish") == 1

    def test_busy_target_is_refused_and_the_giver_keeps_the_item(
        self, fake_host: _FakeHost
    ) -> None:
        self._seed_giver(fake_host, 1)
        fake_host.kv_store[_scoped(_lock_key(_ALICE))] = b"1"
        result = _give(fake_host, "fish", "alice")
        assert result.detail == "give:busy"
        assert _reply(fake_host) == app._BUSY_MSG
        assert _quantity(fake_host, _VIEWER, "fish") == 1
        assert _quantity(fake_host, _ALICE, "fish") is None

    def test_successful_give_moves_exactly_one_unit_and_cleans_up_the_empty_row(
        self, fake_host: _FakeHost
    ) -> None:
        self._seed_giver(fake_host, 1)
        result = _give(fake_host, "fish", "alice")
        assert result.detail == "give"
        assert _quantity(fake_host, _ALICE, "fish") == 1
        assert _quantity(fake_host, _VIEWER, "fish") is None, "emptied row is removed"
        assert len(fake_host.db.rows) == 1

    def test_cleanup_happens_only_after_the_credit_succeeded(self, fake_host: _FakeHost) -> None:
        self._seed_giver(fake_host, 1)
        fake_host.db.raise_on["insert"] = _BACKEND
        with pytest.raises(RuntimeError):
            _give(fake_host, "fish", "alice")
        assert not any(op == "delete" for op, _a in fake_host.db.calls)

    def test_giving_what_you_do_not_have_changes_nothing(self, fake_host: _FakeHost) -> None:
        result = _give(fake_host, "fish", "alice")
        assert result.detail == "give:insufficient"
        assert fake_host.db.rows == {}

    @pytest.mark.parametrize("actor", ["Viewer-1", "@viewer-1", "VIEWER-1", " viewer-1 "])
    def test_giver_identity_is_normalized_like_a_typed_target(
        self, fake_host: _FakeHost, actor: str
    ) -> None:
        self._seed_giver(fake_host, 1)
        assert _give(fake_host, "fish", "alice", actor=actor).detail == "give"

    @pytest.mark.parametrize("target", ["Viewer-1", "@VIEWER-1", "viewer-1"])
    def test_self_give_is_caught_whatever_the_casing(
        self, fake_host: _FakeHost, target: str
    ) -> None:
        self._seed_giver(fake_host, 1)
        assert _give(fake_host, "fish", target, actor="VIEWER-1").detail == "give:self"
        assert _quantity(fake_host, _VIEWER, "fish") == 1


# directory lock: no lost updates


def _fill_items(host: _FakeHost, target: str, count: int) -> None:
    """Give `target` exactly `count` distinct one-unit items (mod grants, real code path)."""
    for i in range(count):
        _run(_grant("add", f"item{i:03d}", target))
    raw = host.kv_store[_scoped(_dir_key(_pseudonym(target)))]
    assert len(json.loads(raw)) == count


class TestDirectoryLock:
    def test_lock_is_taken_atomically_with_a_short_ttl_and_released(
        self, fake_host: _FakeHost
    ) -> None:
        _run(_grant("add", "sword", "alice"))
        locks = [c for c in fake_host.kv_calls if c[0] == "increment"]
        assert len(locks) == 1
        _op, key, delta, ttl = locks[0]
        assert key == _scoped(_lock_key(_ALICE)) and delta == 1
        assert 0 < ttl <= 30, "a crashed holder must not block a user for long"
        assert _scoped(_lock_key(_ALICE)) not in fake_host.kv_store, "released afterwards"

    def test_a_busy_lock_replies_busy_without_writing_and_retries_a_few_times(
        self, fake_host: _FakeHost
    ) -> None:
        fake_host.kv_store[_scoped(_lock_key(_ALICE))] = b"1"
        _run(_grant("add", "sword", "alice"))
        assert _reply(fake_host) == app._BUSY_MSG
        assert fake_host.db.rows == {}
        assert [c[0] for c in fake_host.kv_calls].count("increment") == app._LOCK_ATTEMPTS
        assert any(m == "inventory.dir_lock_busy" for _l, m, _f in fake_host.log_calls)
        assert fake_host.kv_store[_scoped(_lock_key(_ALICE))] != b"", "someone else's lock stays"

    def test_the_lock_frees_up_between_attempts(self, fake_host: _FakeHost) -> None:
        """Holder releases between our first and second attempt -> we get it and proceed."""
        key = _scoped(_lock_key(_ALICE))
        fake_host.kv_store[key] = b"1"
        kv_mod = sys.modules["wit_world"].imports.kv
        real_increment = kv_mod.increment

        def _increment(k: str, delta: int, ttl: int) -> int:
            result = real_increment(k, delta, ttl)
            if k == key and result == 2:  # the holder finishes right after our first attempt
                fake_host.kv_store.pop(key)
            return result

        kv_mod.increment = _increment
        _run(_grant("add", "sword", "alice"))
        assert _quantity(fake_host, _ALICE, "sword") == 1

    def test_a_creator_that_published_while_we_waited_is_adopted_not_overwritten(
        self, fake_host: _FakeHost
    ) -> None:
        """The exact lost-update race: both invocations saw an empty directory."""
        _run(_grant("add", "sword", "alice"))  # the "other creator" finishes first
        (row_id,) = fake_host.db.rows
        directory_key = _scoped(_dir_key(_ALICE))
        published = fake_host.kv_store[directory_key]
        reads = {"n": 0}

        class _Store(dict[str, bytes]):
            def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
                if key == directory_key:
                    reads["n"] += 1
                    if reads["n"] == 1:  # our unlocked first read predates their publish
                        return None
                return super().get(key, default)

        fake_host.kv_store = _Store(fake_host.kv_store)
        _run(_grant("add", "sword", "alice"))
        assert len(fake_host.db.rows) == 1, "no second row for the same (user, item)"
        assert fake_host.db.rows[row_id]["quantity"] == 2
        assert fake_host.kv_store[directory_key] == published

    def test_two_different_new_items_both_survive_in_the_directory(
        self, fake_host: _FakeHost
    ) -> None:
        """Documents the guarantee the lock buys: sequential-equivalent outcomes."""
        _run(_grant("add", "sword", "alice"))
        _run(_grant("add", "shield", "alice"))
        directory = json.loads(fake_host.kv_store[_scoped(_dir_key(_ALICE))])
        assert set(directory) == {"sword", "shield"}

    def test_lock_is_released_when_the_critical_section_fails(self, fake_host: _FakeHost) -> None:
        fake_host.db.raise_on["insert"] = _BACKEND
        with pytest.raises(RuntimeError, match="db_insert"):
            _run(_grant("add", "sword", "alice"))
        assert _scoped(_lock_key(_ALICE)) not in fake_host.kv_store

    def test_a_failed_lock_release_is_logged_not_swallowed(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kv_mod = sys.modules["wit_world"].imports.kv

        def _boom(key: str) -> None:
            raise OSError("kv down")

        monkeypatch.setattr(kv_mod, "delete", _boom)
        _run(_grant("add", "sword", "alice"))
        assert any(m == "inventory.lock_release_failed" for m, _f in _errors(fake_host))

    def test_a_lock_increment_failure_is_loud(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kv_mod = sys.modules["wit_world"].imports.kv

        def _boom(key: str, delta: int, ttl: int) -> int:
            raise OSError("kv down")

        monkeypatch.setattr(kv_mod, "increment", _boom)
        with pytest.raises(RuntimeError, match="kv_lock"):
            _run(_grant("add", "sword", "alice"))
        assert fake_host.db.rows == {}

    def test_cleanup_is_skipped_loudly_when_the_lock_is_busy(self, fake_host: _FakeHost) -> None:
        _run(_grant("add", "sword", "alice"))
        fake_host.kv_store[_scoped(_lock_key(_ALICE))] = b"1"
        _run(_grant("remove", "sword", "alice"))
        assert "now have 0" in _reply(fake_host)
        assert any(m == "inventory.cleanup_skipped" for _l, m, _f in fake_host.log_calls)
        # the zero row stays but is invisible to listings
        assert "has no items" in _list(fake_host, actor="alice")

    def test_quantity_changes_on_existing_rows_need_no_lock(self, fake_host: _FakeHost) -> None:
        _run(_grant("add", "sword", "alice"))
        before = [c[0] for c in fake_host.kv_calls].count("increment")
        _run(_grant("add", "sword", "alice"))
        assert [c[0] for c in fake_host.kv_calls].count("increment") == before

    def test_concurrent_last_unit_gives_cannot_both_succeed(self, fake_host: _FakeHost) -> None:
        """Both invocations read quantity 1; the version gate serializes the debit."""
        _run(_grant("add", "fish", "viewer-1"))
        (row_id,) = fake_host.db.rows
        real_update = fake_host.db.update
        raced = {"done": False}

        def _update(rid: str, ver: int, cvs: Any) -> Any:
            if not raced["done"]:
                raced["done"] = True
                real_update(rid, ver, [wit_fake_db.ColumnValue("quantity", wit_fake_db.wrap(0))])
            return real_update(rid, ver, cvs)

        sys.modules["wit_world"].imports.db.update = _update
        result = _give(fake_host, "fish", "alice")
        assert result.detail == "give:insufficient", "the retry saw the other give's debit"
        assert _quantity(fake_host, _ALICE, "fish") is None
        assert fake_host.db.rows[row_id]["quantity"] == 0


# bounds


class TestBounds:
    @pytest.mark.parametrize(
        "item", ["@everyone", "a/b", "x" * (MAX_ITEM_LEN + 1), "<b>", "it'em", "a:b", "#tag"]
    )
    def test_bad_item_names_are_refused_for_add_and_give(
        self, fake_host: _FakeHost, item: str
    ) -> None:
        _run(_grant("add", item, "alice"))
        assert _reply(fake_host) == app._BAD_ITEM_MSG
        assert fake_host.db.rows == {}
        _run(_grant("add", "fish", "viewer-1"))
        result = _give(fake_host, item, "alice")
        assert result.detail == "give:bad_item"
        assert _reply(fake_host) == app._BAD_ITEM_MSG

    @pytest.mark.parametrize("item", ["sword", "x" * MAX_ITEM_LEN, "café", "a.b-c_d", "7"])
    def test_good_item_names_are_accepted(self, fake_host: _FakeHost, item: str) -> None:
        _run(_grant("add", item, "alice"))
        assert _quantity(fake_host, _ALICE, item.lower()) == 1

    def test_distinct_item_cap(self, fake_host: _FakeHost) -> None:
        _fill_items(fake_host, "alice", MAX_ITEMS_PER_USER)
        _run(_grant("add", "one-too-many", "alice"))
        assert "maximum of 50 distinct items" in _reply(fake_host)
        assert _quantity(fake_host, _ALICE, "one-too-many") is None
        _run(_grant("add", "item000", "alice"))  # an item they already hold still stacks
        assert _quantity(fake_host, _ALICE, "item000") == 2

    def test_quantity_cap(self, fake_host: _FakeHost) -> None:
        _run(_grant("add", "sword", "alice"))
        row_id = json.loads(fake_host.kv_store[_scoped(_dir_key(_ALICE))])["sword"]
        fake_host.db.rows[row_id]["quantity"] = MAX_QUANTITY
        _run(_grant("add", "sword", "alice"))
        assert "maximum quantity" in _reply(fake_host)
        assert fake_host.db.rows[row_id]["quantity"] == MAX_QUANTITY

    def test_a_full_inventory_listing_stays_inside_the_host_op_budget(
        self, fake_host: _FakeHost
    ) -> None:
        _fill_items(fake_host, "alice", MAX_ITEMS_PER_USER)
        kv_before, db_before = len(fake_host.kv_calls), len(fake_host.db.calls)
        text = _list(fake_host, actor="alice")
        assert "and 25 more" in text
        assert len(fake_host.kv_calls) - kv_before <= 64
        assert len(fake_host.db.calls) - db_before <= 64

    @pytest.mark.parametrize("stored", [-1, MAX_QUANTITY + 1, "abc", None])
    def test_corrupt_stored_quantity_fails_loud_and_is_not_repaired(
        self, fake_host: _FakeHost, stored: Any
    ) -> None:
        _run(_grant("add", "sword", "alice"))
        (row,) = fake_host.db.rows.values()
        row["quantity"] = stored
        for action in (
            lambda: _run(_grant("add", "sword", "alice")),
            lambda: _run(_grant("remove", "sword", "alice")),
            lambda: _list(fake_host, actor="alice"),
        ):
            before = len(fake_host.relay_calls)
            with pytest.raises(RuntimeError, match="quantity_invalid"):
                action()
            assert [json.loads(m)["text"] for _p, m in fake_host.relay_calls[before:]] == [
                app._UNAVAILABLE_MSG
            ]
        assert row["quantity"] == stored


# one identity form


class TestIdentityNormalization:
    @pytest.mark.parametrize("actor", ["Alice", "ALICE", "@Alice", "  alice  "])
    def test_a_users_own_listing_matches_what_a_mod_granted_to_the_typed_name(
        self, fake_host: _FakeHost, actor: str
    ) -> None:
        _run(_grant("add", "sword", "alice"))
        assert "sword x1" in _list(fake_host, actor=actor)

    def test_actor_pseudonym_matches_target_pseudonym(self) -> None:
        assert _actor_pseudonym("Alice") == _ALICE
        assert _actor_pseudonym(None) == _pseudonym("anonymous") == _actor_pseudonym("")


# PII-free logging


class TestNewLogLinesArePiiFree:
    _SECRET = "sentinelpii9f3a"

    def test_new_log_lines_carry_no_user_input(self, fake_host: _FakeHost) -> None:
        secret = self._SECRET
        _run(_grant("add", secret, secret))
        fake_host.kv_store[_scoped(_lock_key(_pseudonym(secret)))] = b"1"
        _run(_grant("add", f"{secret}2", secret))  # busy
        fake_host.kv_store.pop(_scoped(_lock_key(_pseudonym(secret))))
        _run(_grant("remove", secret, secret))
        fake_host.db.raise_on["insert"] = _BACKEND
        with pytest.raises(RuntimeError):
            _run(_grant("add", f"{secret}3", secret))
        assert fake_host.log_calls, "denominator: the scenario must log"
        for _lvl, message, fields in fake_host.log_calls:
            assert secret not in message and secret not in fields, (message, fields)


class TestNoOrphanRows:
    """A row inserted but never indexed in the directory is unreachable -- it must be removed."""

    def _fail_directory_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kv_mod = sys.modules["wit_world"].imports.kv
        real_set = kv_mod.set

        def _set(key: str, value: bytes, ttl: int) -> None:
            if key.endswith(_ALICE):
                raise OSError("kv down")
            real_set(key, value, ttl)

        monkeypatch.setattr(kv_mod, "set", _set)

    def test_directory_write_failure_deletes_the_orphan_row(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fail_directory_write(monkeypatch)
        with pytest.raises(RuntimeError, match="kv_set"):
            _run(_grant("add", "sword", "alice"))
        assert fake_host.db.rows == {}
        assert _scoped(_lock_key(_ALICE)) not in fake_host.kv_store, "lock released too"

    def test_a_retry_after_the_failure_yields_exactly_one_row(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with monkeypatch.context() as patch:
            self._fail_directory_write(patch)
            with pytest.raises(RuntimeError):
                _run(_grant("add", "sword", "alice"))
        _run(_grant("add", "sword", "alice"))
        assert len(fake_host.db.rows) == 1 and _quantity(fake_host, _ALICE, "sword") == 1

    def test_a_failed_orphan_cleanup_is_logged_not_swallowed(
        self, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fail_directory_write(monkeypatch)
        fake_host.db.raise_on["delete"] = _BACKEND
        with pytest.raises(RuntimeError, match="kv_set"):
            _run(_grant("add", "sword", "alice"))
        assert any(m == "inventory.orphan_cleanup_failed" for m, _f in _errors(fake_host))
