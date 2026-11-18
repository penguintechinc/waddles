"""`services/pool_provider.py`: app-wide provider construction, caching and the bypass wrapper."""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart

from services import community_tier_service as svc
from services import pool_provider as pp


@pytest.fixture
def app(admin_db: Any) -> Quart:
    quart_app = Quart(__name__)
    quart_app.config["dal"] = admin_db.dal
    quart_app.config["async_dal"] = admin_db
    return quart_app


def test_override_wins_and_is_returned_as_is(app: Quart) -> None:
    sentinel = object()
    app.config[pp.POOL_PROVIDER_CONFIG_KEY] = sentinel
    assert pp.get_pool_provider(app) is sentinel


def test_default_provider_is_license_server_backed_and_cached(
    app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LICENSE_SERVER_URL", "https://license.example.test/")
    first = pp.get_pool_provider(app)
    assert isinstance(first, pp.ServicePoolProvider)
    assert pp.get_pool_provider(app) is first
    assert first._inner._base_url == "https://license.example.test"  # noqa: SLF001


def test_base_url_defaults_to_public_license_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LICENSE_SERVER_URL", raising=False)
    assert pp._license_server_url() == "https://license.penguintech.io"  # noqa: SLF001


@pytest.mark.parametrize(
    ("host", "expected"),
    [("app.penguincloud.io", True), ("waddles.example.com", False), (None, False)],
)
def test_bypass_wrapper_uses_the_request_host_only(
    monkeypatch: pytest.MonkeyPatch, host: str | None, expected: bool
) -> None:
    monkeypatch.setattr(pp, "current_request_host", lambda: host)
    monkeypatch.setenv("LICENSE_BYPASS", "true")  # env can never grant bypass
    assert pp._request_is_bypass_domain() is expected  # noqa: SLF001


async def test_missing_license_key_raises_the_services_pool_unavailable(
    app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lib `PoolUnavailable` must be translated: the service only catches its own class."""
    from services.schema import bind_community_tier_tables

    bind_community_tier_tables(app.config["dal"], migrate=True)
    monkeypatch.setattr(pp, "current_request_host", lambda: "waddles.example.com")

    async def _no_key(*_: Any) -> None:
        return None

    monkeypatch.setattr("services.tenant_license_service.get_tenant_license_key", _no_key)
    provider = pp.get_pool_provider(app)
    with pytest.raises(svc.PoolUnavailable):
        await provider.get_pool(1, svc.TIER_CODE_PROFESSIONAL)


async def test_bypass_host_without_operator_row_is_an_unlicensed_pool(
    app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.schema import bind_community_tier_tables

    bind_community_tier_tables(app.config["dal"], migrate=True)
    monkeypatch.setattr(pp, "current_request_host", lambda: "app.penguincloud.io")
    info = await pp.get_pool_provider(app).get_pool(1, svc.TIER_CODE_ENTERPRISE)
    assert isinstance(info, svc.PoolInfo) and info.limit is None and info.source == "operator_set"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("app.penguincloud.io", True),
        ("APP.PenguinCloud.IO", True),  # case-folded
        ("app.penguincloud.io:8443", True),  # one port stripped
        ("app.penguincloud.io.", True),  # trailing dot
        ("app.penguincloud.io@evil.example", False),  # userinfo
        ("evil.example@app.penguincloud.io", False),
        ("app.penguincloud.io/x", False),
        ("app.penguincloud.io, evil.example", False),  # multiple values
        ("app.penguincloud.io:80:81", False),  # odd port form
        ("app.penguincloud.io evil", False),
        ("[::1]", False),
        ("app.penguincloud.io.evil.example", False),  # lookalike suffix
        ("", False),
        (None, False),
        ("a" * 400 + ".penguincloud.io", False),
    ],
)
def test_spoofed_and_odd_host_values_never_bypass(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
) -> None:
    monkeypatch.setattr(pp, "current_request_host", lambda: raw)
    assert pp.request_is_bypass_domain() is expected


@pytest.mark.parametrize(
    "url",
    [
        "http://license.example.test",
        "https://user:pw@license.example.test",
        "https://license.example.test/?x=1",
        "https://license.example.test/#f",
        "http://127.0.0.1:9",  # loopback http needs the dev opt-in
    ],
)
def test_unsafe_license_server_url_fails_loud(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("LICENSE_SERVER_URL", url)
    monkeypatch.delenv("LICENSE_SERVER_ALLOW_LOOPBACK_HTTP", raising=False)
    with pytest.raises(ValueError):
        pp._license_server_url()  # noqa: SLF001


def test_loopback_http_only_with_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LICENSE_SERVER_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LICENSE_SERVER_ALLOW_LOOPBACK_HTTP", "1")
    assert pp._license_server_url() == "http://127.0.0.1:9"  # noqa: SLF001
    monkeypatch.setenv("LICENSE_SERVER_URL", "http://license.example.test")
    with pytest.raises(ValueError):  # never a remote host, even with the opt-in
        pp._license_server_url()  # noqa: SLF001
