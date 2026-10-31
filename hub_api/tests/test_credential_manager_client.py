"""`services/credential_manager_client.py` origin pinning (gh-969).

# regression: gh-969 -- the caller's bearer must only ever reach an approved origin.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import certifi
import httpx
import pytest

from services import credential_manager_client as cmc
from services.errors import ApiError


@pytest.mark.parametrize(
    "url",
    [
        "https://credential-manager.waddlebot.svc:8095",
        "https://credmgr.example.com",
        "https://credmgr.example.com/",
        "https://credential-manager:8095",
    ],
)
def test_https_origins_are_accepted(url: str) -> None:
    out = cmc.validate_origin(url)
    assert out.startswith("https://") and not out.endswith("/")


@pytest.mark.parametrize(
    "host",
    [
        "credential-manager",
        "credential-manager.waddlebot.svc",
        "credential-manager.waddlebot.svc.cluster.local",
    ],
)
def test_plain_http_is_always_refused(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a previously configured mesh override cannot send the bearer over HTTP.

    HTTPS is mandatory on every credential-manager call, including cluster DNS.
    """
    monkeypatch.setenv("CREDENTIAL_MANAGER_PLAINTEXT_HOSTS", host)
    with pytest.raises(cmc.UntrustedOriginError):
        cmc.validate_origin(f"http://{host}:8095")


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example.com",  # plain http off-cluster
        "ftp://credential-manager",
        "https://user:pw@credmgr.example.com",
        "https://credmgr.example.com/api",
        "https://credmgr.example.com?x=1",
        "https://credmgr.example.com#f",
        "https://10.0.0.5",
        "http://127.0.0.1:8095",
        "http://localhost:8095",
        "https://[::1]",
        "not a url",
        "",
    ],
)
def test_untrusted_origins_are_rejected(url: str) -> None:
    with pytest.raises(cmc.UntrustedOriginError):
        cmc.validate_origin(url)


def test_allowed_hosts_list_narrows_the_origin() -> None:
    allowed = frozenset({"credmgr.example.com"})
    assert cmc.validate_origin("https://credmgr.example.com", allowed_hosts=allowed)
    with pytest.raises(cmc.UntrustedOriginError):
        cmc.validate_origin("https://other.example.com", allowed_hosts=allowed)


async def test_unset_url_is_a_loud_503_with_no_plaintext_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CREDENTIAL_MANAGER_URL", raising=False)
    with pytest.raises(ApiError) as exc:
        await cmc.test_bot_connection("twitch", bearer_token="SECRET-BEARER")
    assert exc.value.status_code == 503 and exc.value.code == cmc.UNAVAILABLE_CODE


async def test_bad_url_never_sends_the_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mis-set CREDENTIAL_MANAGER_URL is a loud 503 and NO request is made."""
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    monkeypatch.setenv("CREDENTIAL_MANAGER_URL", "http://attacker.example.com")  # plain http
    with pytest.raises(ApiError) as exc:
        await cmc.test_bot_connection("twitch", bearer_token="SECRET-BEARER")
    assert exc.value.status_code == 503 and exc.value.code == cmc.UNAVAILABLE_CODE
    assert seen == []


async def test_client_ignores_proxy_env_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    real = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        captured.update(kwargs)
        kwargs["transport"] = httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"data": {"platform": "twitch", "valid": True, "error": None}}
            )
        )
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    monkeypatch.setenv("CREDENTIAL_MANAGER_URL", "https://credential-manager.waddlebot.svc:8095")
    assert await cmc.test_bot_connection("twitch", bearer_token="t") == (True, None)
    assert captured["trust_env"] is False and captured["follow_redirects"] is False
    assert isinstance(captured["verify"], ssl.SSLContext)
    assert captured["verify"].check_hostname is True
    assert captured["verify"].verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize(
    "url",
    [
        "https://[broken",
        "https://credmgr.example.com:bad",
        "https://credmgr.example.com:99999",
        "https://credmgr.example.com:0",
    ],
)
async def test_malformed_origin_is_a_loud_503(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid authority syntax is rejected before constructing the HTTP transport.

    Configuration errors use the same unavailable response as unreachable endpoints.
    """
    monkeypatch.setenv("CREDENTIAL_MANAGER_URL", url)
    with pytest.raises(ApiError) as exc:
        await cmc.test_bot_connection("twitch", bearer_token="SECRET-BEARER")
    assert exc.value.status_code == 503


@pytest.mark.parametrize("valid", ["false", "true", 0, 1, None, [], {}])
async def test_non_boolean_verdict_is_refused(
    valid: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed verdicts must never become success through Python truthiness.

    A credential manager must explicitly return a JSON boolean for its decision.
    """
    real = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": {"valid": valid}})
        )
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setenv("CREDENTIAL_MANAGER_URL", "https://credential-manager:8095")
    with pytest.raises(ApiError) as exc:
        await cmc.test_bot_connection("twitch", bearer_token="SECRET-BEARER")
    assert exc.value.status_code == 503


@pytest.mark.parametrize("timeout_value", ["broken", "0", "-1", "nan", "inf"])
async def test_invalid_timeout_is_refused(
    timeout_value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject unbounded and malformed timeouts before a privileged network request.

    An operator configuration mistake must remain a controlled unavailable result.
    """
    monkeypatch.setenv("CREDENTIAL_MANAGER_URL", "https://credential-manager:8095")
    monkeypatch.setenv("CREDENTIAL_MANAGER_TIMEOUT_S", timeout_value)
    with pytest.raises(ApiError) as exc:
        await cmc.test_bot_connection("twitch", bearer_token="SECRET-BEARER")
    assert exc.value.status_code == 503


async def test_private_ca_file_is_loaded_for_the_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured public CA certificate reaches the real TLS context.

    This proves private trust is loaded without disabling hostname or chain checks.
    """
    cert = ssl.create_default_context(cafile=certifi.where()).get_ca_certs(binary_form=True)[0]
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text(ssl.DER_cert_to_PEM_cert(cert))
    monkeypatch.setenv("CREDENTIAL_MANAGER_CA_FILE", str(ca_file))
    monkeypatch.setenv("CREDENTIAL_MANAGER_URL", "https://credential-manager:8095")
    real = httpx.AsyncClient
    contexts: list[ssl.SSLContext] = []

    def factory(**kwargs: object) -> httpx.AsyncClient:
        assert isinstance(kwargs["verify"], ssl.SSLContext)
        contexts.append(kwargs["verify"])
        kwargs["transport"] = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": {"valid": False}})
        )
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await cmc.test_bot_connection("twitch", bearer_token="t") == (False, None)
    assert len(contexts) == 1
    assert contexts[0].get_ca_certs(binary_form=True) == [cert]
    assert contexts[0].check_hostname is True
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED


async def test_invalid_ca_file_is_a_loud_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing operator CA must never fall back to an unverified connection.

    Rejecting before transport creation keeps the administrator bearer local.
    """
    monkeypatch.setenv("CREDENTIAL_MANAGER_URL", "https://credential-manager:8095")
    monkeypatch.setenv("CREDENTIAL_MANAGER_CA_FILE", str(tmp_path / "missing-ca.crt"))
    with pytest.raises(ApiError) as exc:
        await cmc.test_bot_connection("twitch", bearer_token="t")
    assert exc.value.status_code == 503
