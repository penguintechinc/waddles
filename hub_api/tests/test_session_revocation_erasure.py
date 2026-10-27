"""DSAR erasure (Art. 17) also cuts off the erased subject's already-issued access tokens.

The `hub_sessions` rows are deleted inside erasure's all-or-nothing transaction, so refresh is
impossible afterwards; without this the subject's tokens would keep working for their remaining
<= 15 minutes. Reuses the erasure suite's real-pydal fixtures (no mocking of the transaction).
"""

from __future__ import annotations

from typing import Any

import pytest
from flask_core import token_revocation as tr
from flask_core.auth import verify_jwt_token

from services import data_privacy_service as privacy
from services import session_service
from tests import test_data_privacy_erasure as erasure_suite
from tests.conftest import SECRET_KEY, make_user_token

# Re-exported so pytest registers the erasure suite's real-pydal fixtures in this module.
module_db = erasure_suite.module_db
erasure_db = erasure_suite.db
_user = erasure_suite._user


class TestErasureRevokesAccessTokens:
    async def test_every_token_issued_so_far_dies_with_the_account(self, erasure_db: Any) -> None:
        user_id = _user(erasure_db, name="goner")
        bystander = _user(erasure_db, name="bystander")
        tokens = [make_user_token(user_id=user_id) for _ in range(3)]
        kept = make_user_token(user_id=bystander)
        assert all(verify_jwt_token(t, SECRET_KEY) is not None for t in tokens)

        await privacy.anonymize_user_data(
            erasure_db, erasure_db.dal, user_id=user_id, email="goner@example.com"
        )

        assert all(verify_jwt_token(t, SECRET_KEY) is None for t in tokens)
        assert verify_jwt_token(kept, SECRET_KEY) is not None

    async def test_a_failed_erasure_revokes_nothing(
        self, erasure_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id = _user(erasure_db, name="survivor")
        token = make_user_token(user_id=user_id)

        def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("erasure failed")

        monkeypatch.setattr(privacy, "_sync_erase", boom)
        with pytest.raises(RuntimeError):
            await privacy.anonymize_user_data(
                erasure_db, erasure_db.dal, user_id=user_id, email="survivor@example.com"
            )
        assert verify_jwt_token(token, SECRET_KEY) is not None

    async def test_erasure_adds_no_second_commit(
        self, erasure_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The revocation is store-only: erasure stays ONE atomic database transaction."""
        user_id = _user(erasure_db, name="onecommit")
        commits: list[int] = []
        real_commit = erasure_db.dal.commit
        monkeypatch.setattr(erasure_db.dal, "commit", lambda: (commits.append(1), real_commit())[1])
        await privacy.anonymize_user_data(
            erasure_db, erasure_db.dal, user_id=user_id, email="onecommit@example.com"
        )
        assert len(commits) == 1


class TestRevokeUserAccessTokens:
    async def test_writes_the_cutoff_and_reports_success(self) -> None:
        assert await session_service.revoke_user_access_tokens(user_id=5, reason="test") is True
        assert tr.get_token_revocation().issue_floor("5") is not None

    async def test_a_dead_store_is_reported_not_raised(
        self, isolated_token_revocation: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def down(*_a: Any, **_k: Any) -> None:
            raise tr.RevocationStoreError("ConnectionError")

        monkeypatch.setattr(isolated_token_revocation._backend, "put_max", down)
        assert await session_service.revoke_user_access_tokens(user_id=5, reason="test") is False
