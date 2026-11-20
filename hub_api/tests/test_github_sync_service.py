"""`services/github_sync_service.py` -- direct unit coverage beyond the blueprint tests.

`tests/test_v1_github_sync_blueprint.py` monkeypatches `_github_request`
entirely and only calls two of this module's public functions
(`create_repo_connection`/`sync_ticket_to_github`) through HTTP -- this
file covers the rest directly against a real `AsyncDAL` (`automation_db`,
from `conftest.py`): `_github_request()`'s own header/URL construction
(via `httpx.MockTransport`, mirroring `test_workflow_service.py`'s
pattern), `_encryption_key()`'s error branches, `delete_repo_connection()`'s
role check, `sync_ticket_to_github()`'s failure/malformed-response paths,
`handle_github_webhook()`'s silent-ignore branches, and
`retry_failed_syncs()` (no blueprint route calls it at all -- see its own
docstring).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

import services.github_sync_service as svc
from services.errors import ApiError

ENCRYPTION_KEY = "11" * 32


@pytest.fixture(autouse=True)
def _encryption_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SYNC_ENCRYPTION_KEY", ENCRYPTION_KEY)


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class TestEncryptionKey:
    def test_missing_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_SYNC_ENCRYPTION_KEY", raising=False)
        with pytest.raises(ApiError, match="not set"):
            svc.encrypt_token("secret")

    def test_non_hex_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_SYNC_ENCRYPTION_KEY", "not-hex-at-all-zz")
        with pytest.raises(ApiError, match="64-character hex"):
            svc.encrypt_token("secret")

    def test_wrong_length_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_SYNC_ENCRYPTION_KEY", "aa")  # valid hex, wrong length
        with pytest.raises(ApiError, match="64-character hex"):
            svc.encrypt_token("secret")


class TestVerifyWebhookSignature:
    def test_missing_prefix_is_false(self) -> None:
        assert svc.verify_webhook_signature(b"body", "deadbeef", "secret") is False

    def test_missing_signature_is_false(self) -> None:
        assert svc.verify_webhook_signature(b"body", None, "secret") is False

    def test_correct_signature_is_true(self) -> None:
        import hmac

        body = b'{"a": 1}'
        sig = "sha256=" + hmac.new(b"secret", body, "sha256").hexdigest()
        assert svc.verify_webhook_signature(body, sig, "secret") is True

    def test_wrong_secret_is_false(self) -> None:
        import hmac

        body = b'{"a": 1}'
        sig = "sha256=" + hmac.new(b"wrong-secret", body, "sha256").hexdigest()
        assert svc.verify_webhook_signature(body, sig, "secret") is False


class TestGithubRequest:
    async def test_sends_expected_headers_method_and_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            return httpx.Response(201, json={"number": 7})

        _patch_transport(monkeypatch, handler)
        ok, status, data = await svc._github_request(  # noqa: SLF001
            "POST", "/repos/owner/repo/issues", "ghp_token", json_body={"title": "x"}
        )
        assert ok is True
        assert status == 201
        assert data == {"number": 7}
        assert captured["method"] == "POST"
        assert captured["url"] == "https://api.github.com/repos/owner/repo/issues"
        headers = captured["headers"]
        assert isinstance(headers, dict)
        assert headers["authorization"] == "Bearer ghp_token"
        assert headers["x-github-api-version"] == "2022-11-28"

    async def test_non_json_response_body_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        _patch_transport(monkeypatch, handler)
        ok, status, data = await svc._github_request("GET", "/repos/owner/repo", "tok")  # noqa: SLF001
        assert ok is True
        assert data is None

    async def test_4xx_is_not_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        _patch_transport(monkeypatch, handler)
        ok, status, data = await svc._github_request("GET", "/repos/owner/missing", "tok")  # noqa: SLF001
        assert ok is False
        assert status == 404


class TestCreateRepoConnectionValidation:
    async def test_invalid_auth_type_raises(self, automation_db: Any) -> None:
        with pytest.raises(ApiError, match="auth_type"):
            await svc.create_repo_connection(
                automation_db,
                automation_db.dal,
                community_id=1,
                repo_owner="owner",
                repo_name="repo",
                auth_type="bearer-token",
                token="x",
            )

    async def test_missing_token_raises(self, automation_db: Any) -> None:
        with pytest.raises(ApiError, match="token is required"):
            await svc.create_repo_connection(
                automation_db,
                automation_db.dal,
                community_id=1,
                repo_owner="owner",
                repo_name="repo",
                auth_type="pat",
                token="",
            )

    async def test_invalid_sync_mode_raises(self, automation_db: Any) -> None:
        with pytest.raises(ApiError, match="sync_mode"):
            await svc.create_repo_connection(
                automation_db,
                automation_db.dal,
                community_id=1,
                repo_owner="owner",
                repo_name="repo",
                auth_type="pat",
                token="ghp_x",
                sync_mode="not-a-real-mode",
            )


class TestBindIdempotentWithSupportBinder:
    def test_binding_after_support_binder_reuses_support_tables(self, automation_db: Any) -> None:
        """Regression: no `table already defined: support_tickets` at startup.

        `bind_support_token_tables()` ran first on the same DAL.
        """
        from services.schema import bind_support_token_tables

        dal = automation_db.dal
        bind_support_token_tables(dal)
        before = {f for f in dal.support_tickets.fields}
        svc.bind_github_sync_tables(dal)  # must not raise
        svc.bind_github_sync_tables(dal)  # and stays idempotent
        assert {f for f in dal.support_tickets.fields} == before  # richer def reused
        assert "github_repo_connections" in dal.tables

    def test_binding_before_support_binder_still_works(self, automation_db: Any) -> None:
        from services.schema import bind_support_token_tables

        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        bind_support_token_tables(dal)  # support binder guards per table
        assert "support_tickets" in dal.tables


class TestGetRepoConnectionsDecryptFailure:
    async def test_corrupted_token_masks_as_stars(self, automation_db: Any) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        dal.communities.insert(id=1, name="c1")
        dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="not-valid-base64-ciphertext!!",
            webhook_secret="s",
            is_active=True,
        )
        dal.commit()
        connections = await svc.get_repo_connections(automation_db, dal, community_id=1)
        assert connections[0].token_masked == "****"


class TestDeleteRepoConnectionRoleCheck:
    async def test_member_role_is_forbidden(self, automation_db: Any) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        dal.communities.insert(id=1, name="c1")
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="y",
            is_active=True,
        )
        dal.community_members.insert(community_id=1, user_id="7", role="member", is_active=True)
        dal.commit()
        with pytest.raises(ApiError) as exc_info:
            await svc.delete_repo_connection(
                automation_db, dal, user_id=7, community_id=1, connection_id=conn_id
            )
        assert exc_info.value.status_code == 403


class TestSyncCommentToGithub:
    async def test_no_active_sync_record_is_noop(self, automation_db: Any) -> None:
        # No ticket_github_sync row for this ticket at all -- must not raise.
        await svc.sync_comment_to_github(
            automation_db, automation_db.dal, ticket_id=999, comment_text="hi", author_name="alice"
        )

    async def test_posts_comment_and_logs_success(
        self, automation_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token=svc.encrypt_token("tok"),
            webhook_secret="s",
            is_active=True,
        )
        dal.ticket_github_sync.insert(
            ticket_id=42,
            github_repo_connection_id=conn_id,
            github_issue_number=7,
            sync_status="synced",
        )
        dal.commit()

        captured: dict[str, Any] = {}

        async def fake_request(method: str, path: str, token: str, *, json_body: Any = None) -> Any:
            captured["path"] = path
            captured["body"] = json_body
            return True, 201, {"id": 1}

        monkeypatch.setattr(svc, "_github_request", fake_request)

        await svc.sync_comment_to_github(
            automation_db, dal, ticket_id=42, comment_text="Thanks!", author_name="alice"
        )
        assert captured["path"] == "/repos/owner/repo/issues/7/comments"
        assert "Thanks!" in captured["body"]["body"]

    async def test_undecryptable_token_is_noop(self, automation_db: Any) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="not-decryptable",
            webhook_secret="s",
            is_active=True,
        )
        dal.ticket_github_sync.insert(
            ticket_id=42,
            github_repo_connection_id=conn_id,
            github_issue_number=7,
            sync_status="synced",
        )
        dal.commit()
        # Must not raise -- matches Node's silent-return on decrypt failure.
        await svc.sync_comment_to_github(
            automation_db, dal, ticket_id=42, comment_text="hi", author_name="alice"
        )


class TestSyncTicketToGithubFailurePaths:
    async def test_ticket_not_found_raises(self, automation_db: Any) -> None:
        with pytest.raises(ApiError, match="Ticket not found"):
            await svc.sync_ticket_to_github(
                automation_db, automation_db.dal, ticket_id=999, repo_connection_id=1
            )

    async def test_connection_not_found_raises(self, automation_db: Any) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        ticket_id = dal.support_tickets.insert(
            subject="s", description="d", status="open", priority="normal"
        )
        dal.commit()
        with pytest.raises(ApiError, match="Repo connection not found"):
            await svc.sync_ticket_to_github(
                automation_db, dal, ticket_id=ticket_id, repo_connection_id=999
            )

    async def test_github_api_failure_records_failed_sync(
        self, automation_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        ticket_id = dal.support_tickets.insert(
            subject="s", description="d", status="open", priority="normal"
        )
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token=svc.encrypt_token("tok"),
            webhook_secret="y",
            is_active=True,
        )
        dal.commit()

        async def failing_request(
            method: str, path: str, token: str, *, json_body: Any = None
        ) -> Any:
            return False, 422, {"message": "Validation Failed"}

        monkeypatch.setattr(svc, "_github_request", failing_request)

        with pytest.raises(ApiError, match="GitHub API error 422"):
            await svc.sync_ticket_to_github(
                automation_db, dal, ticket_id=ticket_id, repo_connection_id=conn_id
            )

        rows = await automation_db.select_async(dal(dal.ticket_github_sync.ticket_id == ticket_id))
        assert rows.first().sync_status == "failed"

    async def test_malformed_github_response_raises(
        self, automation_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        ticket_id = dal.support_tickets.insert(
            subject="s", description="d", status="open", priority="normal"
        )
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token=svc.encrypt_token("tok"),
            webhook_secret="y",
            is_active=True,
        )
        dal.commit()

        async def ok_but_no_number(
            method: str, path: str, token: str, *, json_body: Any = None
        ) -> Any:
            return True, 201, {"node_id": "abc"}  # missing "number"

        monkeypatch.setattr(svc, "_github_request", ok_but_no_number)

        with pytest.raises(ApiError, match="no issue number"):
            await svc.sync_ticket_to_github(
                automation_db, dal, ticket_id=ticket_id, repo_connection_id=conn_id
            )


class TestHandleGithubWebhookSilentIgnores:
    async def test_unknown_repo_connection_is_silently_ignored(self, automation_db: Any) -> None:
        # No connection seeded at all -- must not raise.
        await svc.handle_github_webhook(
            automation_db,
            automation_db.dal,
            repo_owner="nobody",
            repo_name="nothing",
            event="issues",
            payload={},
            raw_body=b"{}",
            signature="sha256=doesnotmatter",
        )

    async def test_missing_issue_number_is_silently_ignored(self, automation_db: Any) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="s",
            is_active=True,
        )
        dal.commit()
        import hmac

        payload = b"{}"
        sig = "sha256=" + hmac.new(b"s", payload, "sha256").hexdigest()
        await svc.handle_github_webhook(
            automation_db,
            dal,
            repo_owner="owner",
            repo_name="repo",
            event="issues",
            payload={},
            raw_body=payload,
            signature=sig,
        )

    async def test_untracked_issue_is_silently_ignored(self, automation_db: Any) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="s",
            is_active=True,
        )
        dal.commit()
        import hmac
        import json

        payload_dict = {"issue": {"number": 99}, "action": "closed"}
        payload = json.dumps(payload_dict).encode()
        sig = "sha256=" + hmac.new(b"s", payload, "sha256").hexdigest()
        await svc.handle_github_webhook(
            automation_db,
            dal,
            repo_owner="owner",
            repo_name="repo",
            event="issues",
            payload=payload_dict,
            raw_body=payload,
            signature=sig,
        )

    async def test_echo_comment_from_waddlebot_is_not_reinserted(self, automation_db: Any) -> None:
        """`(via WaddleBot)` marker in the comment body -- avoid echo-looping our own posts."""
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="s",
            is_active=True,
        )
        ticket_id = dal.support_tickets.insert(
            subject="s", description="d", status="open", priority="normal"
        )
        dal.ticket_github_sync.insert(
            ticket_id=ticket_id,
            github_repo_connection_id=conn_id,
            github_issue_number=5,
            sync_status="synced",
        )
        dal.commit()

        import hmac
        import json

        payload_dict = {
            "issue": {"number": 5},
            "action": "created",
            "comment": {"body": "Hi (via WaddleBot)", "user": {"login": "bot"}},
        }
        payload = json.dumps(payload_dict).encode()
        sig = "sha256=" + hmac.new(b"s", payload, "sha256").hexdigest()
        await svc.handle_github_webhook(
            automation_db,
            dal,
            repo_owner="owner",
            repo_name="repo",
            event="issue_comment",
            payload=payload_dict,
            raw_body=payload,
            signature=sig,
        )
        rows = await automation_db.select_async(
            dal(dal.support_ticket_comments.ticket_id == ticket_id)
        )
        assert not rows


class TestProcessInboundIssueCommentFailure:
    async def test_insert_failure_writes_failure_log_not_raise(
        self, automation_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)

        async def failing_insert(table: Any, **fields: Any) -> Any:
            raise RuntimeError("db exploded")

        monkeypatch.setattr(automation_db, "insert_async", failing_insert)

        # Must not raise -- matches Node's catch-log-continue.
        await svc.process_inbound_issue_comment(
            automation_db, dal, sync_id=1, ticket_id=1, comment_body="hi", author="alice"
        )


class TestProcessInboundIssueCloseBranches:
    async def test_missing_sync_record_is_noop(self, automation_db: Any) -> None:
        await svc.process_inbound_issue_close(
            automation_db, automation_db.dal, sync_id=999, ticket_id=1
        )

    async def test_auto_close_disabled_does_not_close_ticket(self, automation_db: Any) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="s",
            auto_close_on_github_close=False,
            is_active=True,
        )
        ticket_id = dal.support_tickets.insert(
            subject="s", description="d", status="open", priority="normal"
        )
        sync_id = dal.ticket_github_sync.insert(
            ticket_id=ticket_id,
            github_repo_connection_id=conn_id,
            github_issue_number=1,
            sync_status="synced",
        )
        dal.commit()
        await svc.process_inbound_issue_close(
            automation_db, dal, sync_id=sync_id, ticket_id=ticket_id
        )
        rows = await automation_db.select_async(dal(dal.support_tickets.id == ticket_id))
        assert rows.first().status == "open"

    async def test_update_failure_writes_failure_log_not_raise(
        self, automation_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token="x",
            webhook_secret="s",
            auto_close_on_github_close=True,
            is_active=True,
        )
        ticket_id = dal.support_tickets.insert(
            subject="s", description="d", status="open", priority="normal"
        )
        sync_id = dal.ticket_github_sync.insert(
            ticket_id=ticket_id,
            github_repo_connection_id=conn_id,
            github_issue_number=1,
            sync_status="synced",
        )
        dal.commit()

        async def failing_update(query: Any, **fields: Any) -> Any:
            raise RuntimeError("db exploded")

        monkeypatch.setattr(automation_db, "update_async", failing_update)

        await svc.process_inbound_issue_close(
            automation_db, dal, sync_id=sync_id, ticket_id=ticket_id
        )


class TestRetryFailedSyncs:
    async def test_retries_succeed_and_clear_failed_status(
        self, automation_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        ticket_id = dal.support_tickets.insert(
            subject="s", description="d", status="open", priority="normal"
        )
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token=svc.encrypt_token("tok"),
            webhook_secret="s",
            is_active=True,
        )
        dal.ticket_github_sync.insert(
            ticket_id=ticket_id,
            github_repo_connection_id=conn_id,
            github_issue_number=0,
            sync_status="failed",
            retry_count=0,
            created_at=datetime.now(UTC) - timedelta(hours=1),  # well past the 1-minute backoff
        )
        dal.commit()

        async def succeeding_request(
            method: str, path: str, token: str, *, json_body: Any = None
        ) -> Any:
            return True, 201, {"number": 1, "node_id": "n1"}

        monkeypatch.setattr(svc, "_github_request", succeeding_request)

        result = await svc.retry_failed_syncs(automation_db, dal)
        assert result.retried == 1
        assert result.succeeded == 1
        assert result.failed == 0

    async def test_backoff_not_yet_elapsed_is_skipped(self, automation_db: Any) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        ticket_id = dal.support_tickets.insert(
            subject="s", description="d", status="open", priority="normal"
        )
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token=svc.encrypt_token("tok"),
            webhook_secret="s",
            is_active=True,
        )
        dal.ticket_github_sync.insert(
            ticket_id=ticket_id,
            github_repo_connection_id=conn_id,
            github_issue_number=0,
            sync_status="failed",
            retry_count=0,
            created_at=datetime.now(UTC),  # just failed -- backoff not elapsed yet
        )
        dal.commit()

        result = await svc.retry_failed_syncs(automation_db, dal)
        assert result.retried == 1
        assert result.succeeded == 0
        assert result.failed == 0  # skipped, not attempted

    async def test_repeated_failure_increments_retry_count(
        self, automation_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal = automation_db.dal
        svc.bind_github_sync_tables(dal)
        ticket_id = dal.support_tickets.insert(
            subject="s", description="d", status="open", priority="normal"
        )
        conn_id = dal.github_repo_connections.insert(
            community_id=1,
            repo_owner="owner",
            repo_name="repo",
            auth_type="pat",
            encrypted_token=svc.encrypt_token("tok"),
            webhook_secret="s",
            is_active=True,
        )
        sync_id = dal.ticket_github_sync.insert(
            ticket_id=ticket_id,
            github_repo_connection_id=conn_id,
            github_issue_number=0,
            sync_status="failed",
            retry_count=0,
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
        dal.commit()

        async def failing_request(
            method: str, path: str, token: str, *, json_body: Any = None
        ) -> Any:
            return False, 500, {"message": "boom"}

        monkeypatch.setattr(svc, "_github_request", failing_request)

        result = await svc.retry_failed_syncs(automation_db, dal)
        assert result.retried == 1
        assert result.succeeded == 0
        assert result.failed == 1

        rows = await automation_db.select_async(dal(dal.ticket_github_sync.id == sync_id))
        assert rows.first().retry_count == 1
