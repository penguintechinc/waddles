"""Log-redaction regression tests for hub_api's credential / storage / crawler paths.

SECURITY (HIGH, PII/secret in logs; consumer side of #766). These sites logged the
raw exception (`"...%s", exc`, `f"...{e}"`, `extra={"error": str(exc)}`) or a full
`url=%s`:

- `services/platform_integrations_crypto.py` -- decrypt failure (key/ciphertext),
- `services/storage_service.py` -- object key + botocore message,
- `services/bot_ai_knowledge.py` -- user-supplied crawl URLs (query-string secrets)
  and the SSRF-guard / httpx exception text (which embeds the URL).

Each test plants a sentinel where a real failure puts data and asserts it never
appears in captured log output (message, `extra`, rendered traceback).

Fail-first proof (executed): with the pre-fix service modules restored, every test
below that plants a sentinel fails because the sentinel appears in the log text.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from botocore.exceptions import ClientError

from services import bot_ai_knowledge, platform_integrations_crypto, storage_service
from services.url_guard import SSRFError

SENTINEL = "SENTINEL-secret-5d1e"

# Fixed test-only AES key, not a real credential.
_KEY = "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"  # gitleaks:allow


def _all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Everything this service's loggers emitted: message, rendered traceback, `extra`.

    `httpx`/`httpcore` library loggers are excluded -- httpx itself logs the request URL
    at INFO, independent of the code under test (tracked as a separate follow-up).
    """
    formatter = logging.Formatter()
    return "\n".join(
        formatter.format(r) + repr(r.__dict__)
        for r in caplog.records
        if not r.name.startswith(("httpx", "httpcore", "botocore", "boto3", "urllib3"))
    )


class TestPlatformIntegrationsDecrypt:
    """`decrypt_if_needed` must not log key/ciphertext-bearing exception text."""

    def test_real_tampered_ciphertext(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)
        blob = "AAAA" + SENTINEL.replace("-", "")  # valid base64 charset, fails the GCM tag
        caplog.set_level(logging.DEBUG)

        assert platform_integrations_crypto.decrypt_if_needed(blob, is_encrypted=True) == blob

        text = _all_log_text(caplog)
        assert blob not in text and _KEY not in text
        assert "Failed to decrypt platform_integrations credential type=" in text

    def test_exception_message_with_secret_is_never_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def explode(_value: str) -> str:
            raise platform_integrations_crypto.PlatformCredentialCryptoError(
                f"key={SENTINEL} ciphertext={SENTINEL}"
            )

        monkeypatch.setattr(platform_integrations_crypto, "decrypt_value", explode)
        caplog.set_level(logging.DEBUG)

        assert platform_integrations_crypto.decrypt_if_needed("blob", is_encrypted=True) == "blob"

        assert SENTINEL not in _all_log_text(caplog)
        assert "PlatformCredentialCryptoError" in _all_log_text(caplog)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"detail {SENTINEL}"}}, "GetObject")


class _NoSuchKeyError(Exception):
    """Stand-in for the typed `client.exceptions.NoSuchKey` class."""


class TestStorageService:
    """`read_bundle_sidecar` / `delete_object` log type/code/category, never message or URL key."""

    async def test_delete_object_failure_does_not_log_key_or_message(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("S3_PUBLIC_BASE_URL", "https://cdn.example/assets")
        client = MagicMock()
        client.delete_object.side_effect = _client_error("AccessDenied")
        monkeypatch.setattr(storage_service, "_client", lambda: client)
        caplog.set_level(logging.DEBUG)

        # Caller-supplied URL: the path after the base becomes the S3 key and can carry a secret.
        await storage_service.delete_object(f"https://cdn.example/assets/avatars/{SENTINEL}.png")

        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "Failed to delete storage object type=botocore.exceptions.ClientError" in text
        assert "code=AccessDenied" in text and "category=storage_client_error" in text
        assert "key_prefix=avatars" in text

    async def test_delete_object_unrecognised_prefix_is_a_constant(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("S3_PUBLIC_BASE_URL", "https://cdn.example/assets")
        client = MagicMock()
        client.delete_object.side_effect = _client_error("AccessDenied")
        monkeypatch.setattr(storage_service, "_client", lambda: client)
        caplog.set_level(logging.DEBUG)

        await storage_service.delete_object(f"https://cdn.example/assets/{SENTINEL.upper()}/x")

        assert SENTINEL.upper() not in _all_log_text(caplog)
        assert "key_prefix=<unrecognized>" in _all_log_text(caplog)

    @pytest.mark.parametrize("code", ["AccessDenied", "InternalError"])
    async def test_read_bundle_sidecar_error_does_not_log_message(
        self, code: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = SimpleNamespace(
            exceptions=SimpleNamespace(NoSuchKey=_NoSuchKeyError),
            get_object=MagicMock(side_effect=_client_error(code)),
        )
        monkeypatch.setattr(storage_service, "_client", lambda: client)
        caplog.set_level(logging.DEBUG)

        with pytest.raises(ClientError):
            await storage_service.read_bundle_sidecar("waddles.x.y", "1.0.0", "abc123")

        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "bundle sidecar read failed type=botocore.exceptions.ClientError" in text
        assert f"code={code}" in text

    async def test_read_bundle_sidecar_generic_404_debug_log_does_not_log_message(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = SimpleNamespace(
            exceptions=SimpleNamespace(NoSuchKey=_NoSuchKeyError),
            get_object=MagicMock(side_effect=_client_error("NoSuchKey")),
        )
        monkeypatch.setattr(storage_service, "_client", lambda: client)
        caplog.set_level(logging.DEBUG)

        assert await storage_service.read_bundle_sidecar("waddles.x.y", "1.0.0", "abc123") is None

        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "generic ClientError 404/NoSuchKey" in text and "code=NoSuchKey" in text


SECRET_URL = f"https://docs.example.com/guide?access_token={SENTINEL}"


class TestKnowledgeCrawlerLogs:
    """Crawl logs carry the host only, and never the SSRF-guard / httpx exception text."""

    async def test_real_ssrf_guard_block_logs_host_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No mocking: a loopback literal is rejected by the real `validate_url`."""
        caplog.set_level(logging.DEBUG)

        pages = await bot_ai_knowledge._fetch_sitemap_pages(
            f"http://127.0.0.1/docs?access_token={SENTINEL}"
        )

        assert pages == []
        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "Sitemap fetch blocked by SSRF guard type=" in text
        assert "host=127.0.0.1" in text

    async def test_sitemap_ssrf_error_message_is_not_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def blocked(_client: Any, url: str, **_kwargs: Any) -> httpx.Response:
            raise SSRFError(f"URL has no host: {url!r}")

        monkeypatch.setattr(bot_ai_knowledge, "guarded_get", blocked)
        caplog.set_level(logging.DEBUG)

        assert await bot_ai_knowledge._fetch_sitemap_pages(SECRET_URL) == []

        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "host=docs.example.com" in text

    async def test_sitemap_http_error_message_is_not_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def failing(_client: Any, url: str, **_kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError(f"all connection attempts failed for {url}")

        monkeypatch.setattr(bot_ai_knowledge, "guarded_get", failing)
        caplog.set_level(logging.DEBUG)

        assert await bot_ai_knowledge._fetch_sitemap_pages(SECRET_URL) == []

        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "Could not fetch sitemap type=httpx.ConnectError" in text
        assert "category=network_connect" in text

    async def test_sitemap_non_ok_status_logs_host_only(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def not_found(_client: Any, url: str, **_kwargs: Any) -> httpx.Response:
            return httpx.Response(404, request=httpx.Request("GET", url))

        monkeypatch.setattr(bot_ai_knowledge, "guarded_get", not_found)
        caplog.set_level(logging.DEBUG)

        assert await bot_ai_knowledge._fetch_sitemap_pages(SECRET_URL) == []

        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "host=docs.example.com status=404" in text

    async def test_sitemap_page_fetch_errors_log_host_only(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        sitemap = (
            "<urlset>"
            f"<loc>https://a.example.com/p?token={SENTINEL}</loc>"
            f"<loc>https://b.example.com/p?token={SENTINEL}</loc>"
            "</urlset>"
        )

        async def mixed(_client: Any, url: str, **_kwargs: Any) -> httpx.Response:
            if url.endswith("/sitemap.xml"):
                return httpx.Response(200, text=sitemap, request=httpx.Request("GET", url))
            if "a.example.com" in url:
                raise SSRFError(f"blocked {url!r}")
            raise httpx.ReadTimeout(f"timed out fetching {url}")

        monkeypatch.setattr(bot_ai_knowledge, "guarded_get", mixed)
        caplog.set_level(logging.DEBUG)

        assert await bot_ai_knowledge._fetch_sitemap_pages("https://docs.example.com") == []

        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "Page fetch blocked by SSRF guard type=" in text and "host=a.example.com" in text
        assert "Failed to fetch knowledge page type=httpx.ReadTimeout" in text
        assert "host=b.example.com" in text and "category=network_timeout" in text

    async def test_github_fetch_logs_host_only(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def blocked(_client: Any, url: str, **_kwargs: Any) -> httpx.Response:
            raise SSRFError(f"blocked {url!r}")

        monkeypatch.setattr(bot_ai_knowledge, "guarded_get", blocked)
        caplog.set_level(logging.DEBUG)

        # `branch` is user data spliced into the contents URL's query string.
        pages = await bot_ai_knowledge._fetch_github_markdown(
            "https://github.com/o/r", f"main&access_token={SENTINEL}", "docs", None
        )

        assert pages == []
        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "GitHub fetch blocked by SSRF guard type=" in text
        assert "host=api.github.com" in text

    async def test_github_non_ok_status_logs_host_only(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def server_error(_client: Any, url: str, **_kwargs: Any) -> httpx.Response:
            return httpx.Response(500, request=httpx.Request("GET", url))

        monkeypatch.setattr(bot_ai_knowledge, "guarded_get", server_error)
        caplog.set_level(logging.DEBUG)

        await bot_ai_knowledge._fetch_github_markdown(
            "https://github.com/o/r", f"main&access_token={SENTINEL}", "docs", None
        )

        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "GitHub API non-OK response host=api.github.com status=500" in text

    async def test_github_file_fetch_block_logs_host_only(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        listing = [
            {
                "type": "file",
                "name": "a.md",
                "url": f"https://api.github.com/f?token={SENTINEL}",
                "html_url": "https://github.com/o/r/a.md",
            }
        ]
        calls = {"n": 0}

        async def second_call_blocked(_client: Any, url: str, **_kwargs: Any) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json=listing, request=httpx.Request("GET", url))
            raise SSRFError(f"blocked {url!r}")

        monkeypatch.setattr(bot_ai_knowledge, "guarded_get", second_call_blocked)
        caplog.set_level(logging.DEBUG)

        await bot_ai_knowledge._fetch_github_markdown(
            "https://github.com/o/r", "main", "docs", None
        )

        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "GitHub file fetch blocked by SSRF guard type=" in text
        assert "host=api.github.com" in text

    async def test_chunk_index_error_does_not_log_exception_or_url(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        source = SimpleNamespace(
            source_type="generic_url",
            source_url="https://docs.example.com",
            branch=None,
            docs_path=None,
            encrypted_token=None,
        )
        dal = MagicMock()
        dal.ai_knowledge_sources.__getitem__.return_value = source

        async def one_page(_url: str) -> list[bot_ai_knowledge._Page]:
            return [
                bot_ai_knowledge._Page(
                    url=f"https://docs.example.com/p?token={SENTINEL}",
                    title="t",
                    content="some documentation text " * 10,
                )
            ]

        async def failing_embedding(_text: str) -> list[float]:
            raise RuntimeError(f"embedding backend echoed: {SENTINEL}")

        monkeypatch.setattr(bot_ai_knowledge, "_fetch_sitemap_pages", one_page)
        monkeypatch.setattr(bot_ai_knowledge, "_generate_embedding", failing_embedding)
        caplog.set_level(logging.DEBUG)

        await bot_ai_knowledge.index_source(dal, 7)

        text = _all_log_text(caplog)
        assert SENTINEL not in text
        assert "Chunk index error type=builtins.RuntimeError" in text
        assert "source_id=7" in text and "host=docs.example.com" in text
