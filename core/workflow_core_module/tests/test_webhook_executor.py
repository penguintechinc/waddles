"""`services/webhook_executor.py` -- outbound webhook execution (httpx), HMAC, retries, extraction.

Complements `tests/test_webhook_executor_expressions.py` (SafeExpressionEvaluator/
ExpressionTemplater, pre-existing) with HMAC signing, response extraction,
retry policy, and the full `WebhookExecutor.execute()`/`WebhookActionNode`
flow -- httpx's `AsyncClient` is patched per-test with a fake context
manager returning real `httpx.Response` objects (no network I/O).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services.webhook_executor import (
    HMACSignatureGenerator,
    ResponseExtractor,
    RetryPolicy,
    WebhookActionNode,
    WebhookExecutionError,
    WebhookExecutor,
    WebhookNonRetryableError,
    WebhookRetryableError,
    WebhookTimeoutError,
)


class _FakeAsyncClient:
    """Stand-in for `httpx.AsyncClient` -- `request()` returns queued responses/exceptions."""

    def __init__(self, responses: list[Any], **kwargs: Any) -> None:
        # Deliberately NOT a copy -- `execute()` constructs a new
        # `httpx.AsyncClient(...)` on every retry attempt, so the queue
        # must be a shared mutable reference across those instantiations
        # for `.pop(0)` to advance correctly attempt-to-attempt.
        self._responses = responses

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_client(responses: list[Any]):
    return patch(
        "services.webhook_executor.httpx.AsyncClient",
        lambda **kwargs: _FakeAsyncClient(responses, **kwargs),
    )


class TestHMACSignatureGenerator:
    def test_generate_and_verify(self) -> None:
        gen = HMACSignatureGenerator("secret")
        sig = gen.generate("payload")
        assert gen.verify("payload", sig) is True

    def test_verify_rejects_wrong_signature(self) -> None:
        gen = HMACSignatureGenerator("secret")
        assert gen.verify("payload", "deadbeef") is False

    def test_unsupported_algorithm_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported HMAC algorithm"):
            HMACSignatureGenerator("secret", algorithm="md5")

    def test_sha512_algorithm(self) -> None:
        gen = HMACSignatureGenerator("secret", algorithm="sha512")
        assert len(gen.generate("data")) == 128


class TestResponseExtractor:
    def test_extract_json_success(self) -> None:
        response = httpx.Response(200, json={"a": 1})
        assert ResponseExtractor.extract_json(response) == {"a": 1}

    def test_extract_json_invalid_raises(self) -> None:
        response = httpx.Response(200, text="not json")
        with pytest.raises(WebhookExecutionError, match="Invalid JSON"):
            ResponseExtractor.extract_json(response)

    def test_extract_variables_no_extractors(self) -> None:
        response = httpx.Response(200, json={"a": 1})
        assert ResponseExtractor.extract_variables(response, None) == {}

    def test_extract_variables_simple_key(self) -> None:
        response = httpx.Response(
            200, json={"status": "ok"}, headers={"content-type": "application/json"}
        )
        result = ResponseExtractor.extract_variables(response, {"s": "status"})
        assert result == {"s": "ok"}

    def test_extract_variables_nested_key(self) -> None:
        response = httpx.Response(
            200, json={"data": {"user": {"id": 42}}},
            headers={"content-type": "application/json"},
        )
        result = ResponseExtractor.extract_variables(response, {"uid": "data.user.id"})
        assert result == {"uid": 42}

    def test_extract_variables_array_access(self) -> None:
        response = httpx.Response(
            200, json={"items": [{"name": "first"}]},
            headers={"content-type": "application/json"},
        )
        result = ResponseExtractor.extract_variables(response, {"n": "items[0].name"})
        assert result == {"n": "first"}

    def test_extract_variables_non_json_content_type(self) -> None:
        response = httpx.Response(200, text="plain", headers={"content-type": "text/plain"})
        result = ResponseExtractor.extract_variables(response, {"x": "status"})
        assert result == {"x": None}

    def test_extract_variables_invalid_path_returns_none(self) -> None:
        response = httpx.Response(
            200, json={"a": 1}, headers={"content-type": "application/json"}
        )
        result = ResponseExtractor.extract_variables(response, {"missing": "b.c"})
        assert result == {"missing": None}

    def test_get_nested_value_none_data_raises(self) -> None:
        with pytest.raises(TypeError):
            ResponseExtractor._get_nested_value(None, "a")

    def test_get_nested_value_bad_index_raises(self) -> None:
        with pytest.raises(IndexError):
            ResponseExtractor._get_nested_value({"items": []}, "items[0]")


class TestRetryPolicy:
    def test_defaults(self) -> None:
        policy = RetryPolicy()
        assert 500 in policy.retryable_status_codes

    def test_is_retryable_timeout_error(self) -> None:
        policy = RetryPolicy()
        assert policy.is_retryable(WebhookTimeoutError("x")) is True

    def test_is_retryable_status_code(self) -> None:
        policy = RetryPolicy()
        assert policy.is_retryable(ValueError("x"), status_code=503) is True

    def test_is_retryable_request_error(self) -> None:
        policy = RetryPolicy()
        assert policy.is_retryable(httpx.ConnectError("refused")) is True

    def test_is_retryable_false_for_generic_error(self) -> None:
        policy = RetryPolicy()
        assert policy.is_retryable(ValueError("x")) is False

    def test_get_delay_exponential_backoff(self) -> None:
        policy = RetryPolicy(initial_delay=1.0, exponential_base=2.0, max_delay=100.0)
        assert policy.get_delay(0) == 1.0
        assert policy.get_delay(2) == 4.0

    def test_get_delay_capped_at_max(self) -> None:
        policy = RetryPolicy(initial_delay=1.0, exponential_base=10.0, max_delay=5.0)
        assert policy.get_delay(5) == 5.0


class TestWebhookExecutorExecute:
    @pytest.mark.asyncio
    async def test_unsupported_method_raises(self) -> None:
        executor = WebhookExecutor()
        with pytest.raises(WebhookExecutionError, match="Unsupported HTTP method"):
            await executor.execute("https://x", method="TRACE")

    @pytest.mark.asyncio
    async def test_success_with_body_and_hmac(self) -> None:
        executor = WebhookExecutor()
        response = httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})
        with _patch_client([response]):
            result = await executor.execute(
                "https://x", method="POST", body={"msg": "${name}"},
                context={"name": "penguin"}, hmac_secret="secret",
            )
        assert result["success"] is True
        assert result["status_code"] == 200

    @pytest.mark.asyncio
    async def test_get_request_success(self) -> None:
        executor = WebhookExecutor()
        response = httpx.Response(200, text="ok")
        with _patch_client([response]):
            result = await executor.execute("https://x", method="get")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_non_retryable_4xx_returns_failure_without_retry(self) -> None:
        executor = WebhookExecutor(retry_policy=RetryPolicy(max_retries=2))
        response = httpx.Response(404, text="not found")
        with _patch_client([response]):
            result = await executor.execute("https://x")
        assert result["success"] is False
        assert result["status_code"] == 404

    @pytest.mark.asyncio
    async def test_retryable_5xx_retries_then_succeeds(self) -> None:
        executor = WebhookExecutor(retry_policy=RetryPolicy(max_retries=2, initial_delay=0.001))
        responses = [httpx.Response(503, text="unavailable"), httpx.Response(200, text="ok")]
        with _patch_client(responses), patch("services.webhook_executor.asyncio.sleep", new=AsyncMock()):
            result = await executor.execute("https://x")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_retryable_5xx_exhausts_retries(self) -> None:
        executor = WebhookExecutor(retry_policy=RetryPolicy(max_retries=1, initial_delay=0.001))
        responses = [httpx.Response(500, text="err"), httpx.Response(500, text="err")]
        with _patch_client(responses), patch("services.webhook_executor.asyncio.sleep", new=AsyncMock()):
            result = await executor.execute("https://x")
        assert result["success"] is False
        assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_timeout_retries_then_exhausts(self) -> None:
        executor = WebhookExecutor(retry_policy=RetryPolicy(max_retries=1, initial_delay=0.001))
        responses = [httpx.TimeoutException("timeout"), httpx.TimeoutException("timeout")]
        with _patch_client(responses), patch("services.webhook_executor.asyncio.sleep", new=AsyncMock()):
            result = await executor.execute("https://x")
        assert result["success"] is False
        assert result["status_code"] is None

    @pytest.mark.asyncio
    async def test_request_error_retries_then_exhausts(self) -> None:
        executor = WebhookExecutor(retry_policy=RetryPolicy(max_retries=1, initial_delay=0.001))
        responses = [httpx.ConnectError("refused"), httpx.ConnectError("refused")]
        with _patch_client(responses), patch("services.webhook_executor.asyncio.sleep", new=AsyncMock()):
            result = await executor.execute("https://x")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_non_retryable_request_error_returns_failure(self) -> None:
        # NOTE: when `is_retryable()` says no, the `except httpx.RequestError`
        # branch has no `break`/`return` -- the `for attempt in range(...)`
        # loop still runs every attempt regardless (just without the sleep/
        # log), so this must supply one exception per attempt (max_retries+1)
        # to reach the final "all retries exhausted" failure result.
        executor = WebhookExecutor(retry_policy=RetryPolicy(max_retries=1, initial_delay=0.001))

        class _NonRetryableRequestError(httpx.RequestError):
            pass

        with (
            _patch_client([_NonRetryableRequestError("bad"), _NonRetryableRequestError("bad")]),
            patch.object(RetryPolicy, "is_retryable", return_value=False),
        ):
            result = await executor.execute("https://x")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_build_response_non_json_content_type(self) -> None:
        executor = WebhookExecutor()
        response = httpx.Response(200, text="plain text", headers={"content-type": "text/plain"})
        with _patch_client([response]):
            result = await executor.execute("https://x")
        assert result["response_body"] == "plain text"

    @pytest.mark.asyncio
    async def test_build_response_invalid_json_falls_back_to_text(self) -> None:
        executor = WebhookExecutor()
        response = httpx.Response(
            200, content=b"not valid json{{", headers={"content-type": "application/json"}
        )
        with _patch_client([response]):
            result = await executor.execute("https://x")
        assert result["response_body"] == "not valid json{{"

    @pytest.mark.asyncio
    async def test_extractors_applied_on_success(self) -> None:
        executor = WebhookExecutor()
        response = httpx.Response(
            200, json={"id": "abc"}, headers={"content-type": "application/json"}
        )
        with _patch_client([response]):
            result = await executor.execute("https://x", extractors={"the_id": "id"})
        assert result["extracted_variables"] == {"the_id": "abc"}


class TestWebhookActionNode:
    @pytest.mark.asyncio
    async def test_execute_success(self) -> None:
        node = WebhookActionNode(
            node_id="wh1", url="https://x", body={"a": "${b}"},
            retry_config={"max_retries": 0},
        )
        response = httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})
        with _patch_client([response]):
            result = await node.execute({"b": "1"})
        assert result["node_id"] == "wh1"
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_execute_swallows_unexpected_exception(self) -> None:
        node = WebhookActionNode(node_id="wh1", url="https://x")
        with patch.object(WebhookExecutor, "execute", AsyncMock(side_effect=RuntimeError("boom"))):
            result = await node.execute({})
        assert result["success"] is False
        assert result["error"] == "boom"
