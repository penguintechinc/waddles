"""Tests for `waddle_sdk.platform_table` (allowlisted platform-table access)."""

from __future__ import annotations

from uuid import uuid4

import pytest

import waddle_sdk.platform_table as pt
from waddle_sdk.testing import (
    FakePlatformTableHost,
    _Error_Backend,
    _Error_Unavailable,
    _FakeWitError,
    install_fake_platform_table_host,
)

ALICE = str(uuid4())
ADMIN = str(uuid4())
T = "community_members"
W = f"platform.table.write:{T}.role"
R = f"platform.table.read:{T}.role"


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("coroutine unexpectedly suspended")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakePlatformTableHost:
    return install_fake_platform_table_host(
        monkeypatch,
        rows={
            ALICE: {"role": "member", "is_active": "true"},
            ADMIN: {"role": "community-admin"},
        },
        grants={W, R},
    )


def test_read_and_write_round_trip(host: FakePlatformTableHost) -> None:
    assert _run(pt.read(T, ALICE, ["role"])) == {"role": "member"}
    _run(pt.write(T, ALICE, {"role": "vip"}))
    assert _run(pt.read(T, ALICE, ["role"])) == {"role": "vip"}
    assert [c[0] for c in host.calls] == ["read", "write", "read"]


def test_unapproved_column_is_denied(host: FakePlatformTableHost) -> None:
    host.grants.discard(W)
    with pytest.raises(pt.DeniedError) as exc:
        _run(pt.write(T, ALICE, {"role": "vip"}))
    assert exc.value.code == "not_granted"
    # read grant exists for role but NOT is_active
    with pytest.raises(pt.NotAllowlistedError):
        _run(pt.write(T, ALICE, {"is_active": "false"}))
    with pytest.raises(pt.DeniedError):
        _run(pt.read(T, ALICE, ["role", "is_active"]))


def test_off_allowlist_column_and_table(host: FakePlatformTableHost) -> None:
    for column in ("display_name", "user_uuid"):
        with pytest.raises(pt.NotAllowlistedError):
            _run(pt.read(T, ALICE, [column]))
    with pytest.raises(pt.NotAllowlistedError):
        _run(pt.read("hub_users", ALICE, ["email"]))


def test_unknown_user_and_protected_row_are_not_a_member(host: FakePlatformTableHost) -> None:
    with pytest.raises(pt.NotAMemberError):
        _run(pt.read(T, str(uuid4()), ["role"]))
    with pytest.raises(pt.NotAMemberError):
        _run(pt.write(T, ADMIN, {"role": "member"}))
    assert host.rows[ADMIN]["role"] == "community-admin"


def test_invalid_role_value_is_rejected_by_host(host: FakePlatformTableHost) -> None:
    with pytest.raises(pt.InvalidArgError):
        _run(pt.write(T, ALICE, {"role": "community-admin"}))
    assert host.rows[ALICE]["role"] == "member"


@pytest.mark.parametrize(
    ("table", "user", "columns"),
    [
        ("", ALICE, ["role"]),
        ("x" * 65, ALICE, ["role"]),
        (T, "not-a-uuid", ["role"]),
        (T, ALICE, []),
        (T, ALICE, ["role", "role"]),
        (T, ALICE, [f"c{i}" for i in range(9)]),
        (T, ALICE, [""]),
    ],
)
def test_client_side_validation_makes_no_host_call(
    host: FakePlatformTableHost, table: str, user: str, columns: list[str]
) -> None:
    with pytest.raises(pt.InvalidArgError):
        _run(pt.read(table, user, columns))
    assert host.calls == []


def test_write_rejects_non_string_values(host: FakePlatformTableHost) -> None:
    with pytest.raises(pt.InvalidArgError):
        _run(pt.write(T, ALICE, {"role": 5}))  # type: ignore[dict-item]


def test_unavailable_and_backend_are_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_platform_table_host(monkeypatch, rows={ALICE: {"role": "member"}}, grants={R})
    for case, exc_type in (
        (_Error_Unavailable("off"), pt.UnavailableError),
        (_Error_Backend("x"), pt.BackendError),
    ):

        def boom(*_a: object, _case=case) -> None:
            raise _FakeWitError(_case)

        import sys

        sys.modules["wit_world"].imports.platform_table.read = boom
        with pytest.raises(exc_type):
            _run(pt.read(T, ALICE, ["role"]))


def test_read_with_unexpected_columns_is_a_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    install_fake_platform_table_host(monkeypatch, rows={ALICE: {"role": "member"}}, grants={R})
    sys.modules["wit_world"].imports.platform_table.read = lambda *_a: []
    with pytest.raises(pt.BackendError):
        _run(pt.read(T, ALICE, ["role"]))
