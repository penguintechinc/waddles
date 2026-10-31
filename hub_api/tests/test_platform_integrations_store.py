"""`services/platform_integrations_store.py` -- SQLite (dev) path + shared allowlist helper.

# regression: gh-969 -- `config_data` is returned through an ALLOWLIST (`account_label` only),
# never scrubbed raw JSON; mirrors `pi_public_config` in migration 0069 (real-PG proof lives in
# `alembic/tests/test_0069_pi_definer_functions.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from flask_core.database import AsyncDAL

from services import platform_integrations_store as store
from services.schema import bind_platform_tables

CANARY = "CANARY-CFG-gh969"


@pytest.fixture
def dal(tmp_path: Any) -> Any:
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'store.db'}", pool_size=1)
    bind_platform_tables(async_dal.dal, migrate=True)
    yield async_dal.dal
    async_dal.dal.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ({}, {}),
        ({"account_label": "Chan"}, {"account_label": "Chan"}),
        ({"account_label": "x" * 500}, {"account_label": "x" * 200}),
        ({"account_label": {"token": CANARY}}, {}),  # non-string label dropped
        ({"auth_code": CANARY, "cookie": CANARY, "pem": CANARY, "u": f"https://x/?t={CANARY}"}, {}),
        (
            {"account_label": "ok", "session_key": CANARY, "nested": {"client_secret": CANARY}},
            {"account_label": "ok"},
        ),
        ("not a dict", {}),
    ],
)
def test_public_config_is_an_allowlist(raw: Any, expected: Any) -> None:
    out = store._public_config(raw)
    assert out == expected
    assert CANARY not in str(out)


def test_sqlite_metadata_and_status_never_return_non_allowlisted_config(dal: Any) -> None:
    t = dal.platform_integrations
    now = datetime.now(UTC)
    t.insert(
        platform="twitch",
        integration_type="bot",
        access_token="TOK",
        client_secret="SEC",
        config_data={"auth_code": CANARY, "account_label": "bot"},
        is_active=True,
        is_encrypted=True,
        created_at=now,
        updated_at=now,
    )
    t.insert(
        platform="youtube",
        integration_type="community_oauth",
        community_id=5,
        access_token="TOK2",
        config_data={"callback": f"https://h/?token={CANARY}"},
        is_active=True,
        is_encrypted=True,
        created_at=now,
        updated_at=now,
    )
    dal.commit()
    meta = store.list_metadata(dal)
    assert {m.platform: m.config_data for m in meta} == {
        "twitch": {"account_label": "bot"},
        "youtube": {},
    }
    assert all(m.access_token_set for m in meta)
    status = store.community_status(dal, 5, None, tenant_id=store.Scope.SYSTEM)
    assert [s.config_data for s in status] == [{}]
    assert CANARY not in str(meta) + str(status)


def test_unsupported_engine_raises() -> None:
    class _Adapter:
        dbengine = "mysql"

    class _Dal:
        _adapter = _Adapter()

    with pytest.raises(store.UnsupportedEngineError):
        store.list_metadata(_Dal())


def test_none_is_never_a_tenant_scope_on_any_engine(dal: Any) -> None:
    """# regression: gh-969 -- omitting/None-ing the tenant fails loudly, even on SQLite."""
    calls = (
        lambda: store.community_status(dal, 1, None, tenant_id=None),  # type: ignore[arg-type]
        lambda: store.get_secret(dal, 1, "youtube", tenant_id=None),  # type: ignore[arg-type]
        lambda: store.disconnect(dal, community_id=1, platform="youtube", tenant_id=None),  # type: ignore[arg-type]
        lambda: store.community_status(dal, 1, None, tenant_id=True),
    )
    for call in calls:
        with pytest.raises(TypeError):
            call()
    with pytest.raises(TypeError):
        store.get_secret(dal, 1, "youtube")  # type: ignore[call-arg]  # omitted entirely
