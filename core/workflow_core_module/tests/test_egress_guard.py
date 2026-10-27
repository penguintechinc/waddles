"""Regression tests for the ACTION_WEBHOOK SSRF egress guard.

Release-audit finding: `execute_action_webhook` fetched an attacker-authorable
URL and reflected the response. These tests exercise the real guard code
(no mocking of `validate_egress_url`/`GuardedResolver`); only
`socket.getaddrinfo` is faked so no network access is required.
"""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import MagicMock

import pytest

from models.execution import ExecutionContext
from models.nodes import ActionWebhookConfig, HttpMethod
from services.egress_guard import (
    EgressBlockedError,
    GuardedResolver,
    is_blocked_address,
    validate_egress_url,
)
from services.node_executor import NodeExecutor

PUBLIC_IP = "93.184.216.34"


def _fake_getaddrinfo(*answers: str) -> Any:
    """Build a getaddrinfo stand-in; successive calls consume `answers` (last repeats)."""
    calls: list[str] = []

    def _impl(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        idx = min(len(calls), len(answers) - 1)
        calls.append(host)
        ip = answers[idx]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    _impl.calls = calls  # type: ignore[attr-defined]
    return _impl


def _context() -> ExecutionContext:
    return ExecutionContext(
        execution_id="exec-1",
        workflow_id="wf-1",
        workflow_version="1.0",
        session_id="sess-1",
        entity_id="community-1",
        user_id="user-1",
    )


def _webhook(url: str) -> ActionWebhookConfig:
    return ActionWebhookConfig(
        node_id="w1",
        label="W",
        position={"x": 0, "y": 0},
        url=url,
        method=HttpMethod.GET,
        retry_count=0,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/admin",  # RFC1918
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://127.0.0.1:8080/",  # loopback
        "http://127.1/",  # short-form loopback
        "http://2130706433/",  # decimal-encoded 127.0.0.1
        "http://[::1]/",  # IPv6 loopback
        "http://[fc00::1]/",  # unique-local
        "http://[fd00:ec2::254]/",  # AWS IPv6 metadata
        "http://[fe80::1]/",  # link-local
        "http://[::ffff:169.254.169.254]/",  # IPv4-mapped metadata
        "http://[64:ff9b::a00:1]/",  # NAT64 wrapping 10.0.0.1
        "http://100.64.0.1/",  # CGNAT
        "http://0.0.0.0/",  # unspecified
    ],
)
async def test_blocks_non_public_ip_literals(url: str) -> None:
    with pytest.raises(EgressBlockedError):
        await validate_egress_url(url)


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "gopher://example.com/", "ftp://example.com/", "//example.com/"]
)
async def test_blocks_non_http_schemes(url: str) -> None:
    with pytest.raises(EgressBlockedError, match="scheme"):
        await validate_egress_url(url)


async def test_blocks_url_userinfo() -> None:
    with pytest.raises(EgressBlockedError, match="credentials"):
        await validate_egress_url("http://user:pw@example.com/")


async def test_blocks_hostname_resolving_to_private(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.1.2.3"))
    with pytest.raises(EgressBlockedError, match="non-public"):
        await validate_egress_url("https://internal.example.com/")


async def test_blocks_when_any_resolved_address_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _mixed(host: Any, port: Any, *a: Any, **k: Any) -> Any:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _mixed)
    with pytest.raises(EgressBlockedError):
        await validate_egress_url("https://mixed.example.com/")


async def test_unresolvable_host_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    def _nxdomain(*a: Any, **k: Any) -> Any:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _nxdomain)
    with pytest.raises(EgressBlockedError, match="cannot resolve"):
        await validate_egress_url("https://nope.invalid/")


async def test_allows_normal_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(PUBLIC_IP))
    await validate_egress_url("https://example.com/hook?x=1")


async def test_allows_public_ip_literal() -> None:
    await validate_egress_url(f"http://{PUBLIC_IP}/")
    await validate_egress_url("https://[2606:4700:4700::1111]/")


def test_is_blocked_address_default_deny_classification() -> None:
    import ipaddress

    assert is_blocked_address(ipaddress.ip_address("169.254.169.254"))
    assert is_blocked_address(ipaddress.ip_address("::1"))
    assert is_blocked_address(ipaddress.ip_address("2002:0a00:0001::1"))  # 6to4 of 10.0.0.1
    assert not is_blocked_address(ipaddress.ip_address(PUBLIC_IP))


async def test_guarded_resolver_pins_validated_public_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(PUBLIC_IP))
    result = await GuardedResolver().resolve("example.com", 443)
    assert [r["host"] for r in result] == [PUBLIC_IP]
    assert result[0]["hostname"] == "example.com"


async def test_guarded_resolver_blocks_private(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    with pytest.raises(EgressBlockedError):
        await GuardedResolver().resolve("localhost", 80)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://127.0.0.1/",
        "http://[::1]/",
    ],
)
async def test_webhook_node_blocks_ssrf_targets_without_requesting(url: str) -> None:
    executor = NodeExecutor()
    await executor._ensure_webhook_session()
    executor._webhook_session.request = MagicMock(side_effect=AssertionError("request made!"))
    result = await executor.execute_action_webhook(_webhook(url), _context(), {})
    assert result.success is False
    assert result.error_type == "egress_blocked"
    assert "egress blocked" in result.error
    executor._webhook_session.request.assert_not_called()
    await executor.close()


async def test_webhook_node_blocks_templated_metadata_url() -> None:
    """Attacker-controlled template data must not smuggle in a blocked host."""
    executor = NodeExecutor()
    ctx = _context()
    ctx.set_variable("host", "169.254.169.254")
    result = await executor.execute_action_webhook(
        _webhook("http://{{host}}/latest/meta-data/"), ctx, {}
    )
    assert result.error_type == "egress_blocked"
    await executor.close()


async def test_webhook_node_blocks_dns_rebinding_at_connect_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block a DNS-rebinding answer swap between pre-flight and connect.

    Pre-flight sees a public IP; the connect-time lookup then answers
    127.0.0.1. The guarded connector must re-validate and refuse -- real
    aiohttp session, no mocking of the request path.
    """
    fake = _fake_getaddrinfo(PUBLIC_IP, "127.0.0.1")
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    executor = NodeExecutor()
    result = await executor.execute_action_webhook(
        _webhook("http://rebind.example.com/"), _context(), {}
    )
    assert result.success is False
    assert result.error_type == "egress_blocked"
    assert len(fake.calls) >= 2  # pre-flight + connect-time lookup
    await executor.close()


async def test_webhook_node_refuses_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Redirect:
        status = 302

        async def __aenter__(self) -> _Redirect:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(PUBLIC_IP))
    executor = NodeExecutor()
    await executor._ensure_webhook_session()
    captured: dict = {}

    def _request(**kwargs: Any) -> _Redirect:
        captured.update(kwargs)
        return _Redirect()

    executor._webhook_session.request = _request
    result = await executor.execute_action_webhook(_webhook("https://example.com/"), _context(), {})
    assert captured["allow_redirects"] is False
    assert result.error_type == "egress_blocked"
    assert "redirect" in result.error
    await executor.close()
