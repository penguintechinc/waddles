"""Enabled login/link provider gating (`waddles.login-<provider>` flags).

Discord/Twitch/Google are on by default; Slack (no connector bundle) is dark. The server is the
only source of truth: start + callback of both flows refuse a disabled provider with a typed 404,
and `GET /api/v1/auth/providers` lists only the enabled ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.auth import auth_bp
from blueprints.v1.identity import identity_bp
from services import login_providers, oauth_service
from tests.entitlement_fakes import FakeFlagGate, FakeLicenseGate, install_entitlement_client
from tests.test_link_oauth_state_binding import _cfg, _hdr, _user


class PerFlagGate(FakeFlagGate):
    """Flag gate answering per flag key; unknown keys are unresolvable (`None`)."""

    def __init__(self, values: Mapping[str, bool]) -> None:
        """Hold the per-key results."""
        super().__init__(None)
        self.values = dict(values)

    def is_enabled(
        self, flag_key: str, distinct_id: str, *, groups: Mapping[str, str] | None = None
    ) -> bool | None:
        self.calls.append(flag_key)
        return self.values.get(flag_key)


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(identity_bp)
    quart_app.register_blueprint(auth_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    quart_app.config["HUB_API_CONFIG"] = _cfg()
    return quart_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        oauth_service,
        "_get_platform_credentials",
        AsyncMock(return_value={"client_id": "cid", "client_secret": "sec"}),  # noqa: S106
    )


def _gate(monkeypatch: pytest.MonkeyPatch, values: Mapping[str, bool] | None = None) -> None:
    install_entitlement_client(
        monkeypatch, flag_gate=PerFlagGate(values or {}), license_gate=FakeLicenseGate()
    )


def test_defaults_on_only_for_connected_providers() -> None:
    assert login_providers.ALL_PROVIDERS == ("discord", "twitch", "google", "slack")
    assert login_providers.flag_key("slack") == "waddles.login-slack"


async def test_providers_endpoint_lists_only_enabled(
    app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch)
    resp = await app.test_client().get("/api/v1/auth/providers")
    body = await resp.get_json()
    assert resp.status_code == 200
    assert [p["platform"] for p in body["providers"]] == ["discord", "twitch", "google"]
    assert set(body["providers"][0]) == {"platform", "displayName"}


async def test_flag_off_hides_a_default_on_provider(
    app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch, {"waddles.login-twitch": False})
    body = await (await app.test_client().get("/api/v1/auth/providers")).get_json()
    assert [p["platform"] for p in body["providers"]] == ["discord", "google"]


async def test_flag_on_enables_slack_end_to_end(
    app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch, {"waddles.login-slack": True})
    client = app.test_client()
    body = await (await client.get("/api/v1/auth/providers")).get_json()
    assert "slack" in [p["platform"] for p in body["providers"]]
    resp = await client.get("/api/v1/auth/oauth/slack")
    assert resp.status_code == 200
    assert "slack.com" in (await resp.get_json())["authorizeUrl"]


async def test_env_baseline_enables_slack(app: Quart, monkeypatch: pytest.MonkeyPatch) -> None:
    _gate(monkeypatch)
    monkeypatch.setenv("FLAG_WADDLES_LOGIN_SLACK", "true")
    assert await login_providers.is_enabled("slack") is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/auth/oauth/slack"),
        ("get", "/api/v1/auth/oauth/slack/link"),
        ("post", "/api/v1/user/identities/link/slack"),
    ],
)
async def test_start_refused_for_disabled_provider(
    app: Quart, auth_db: Any, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    _gate(monkeypatch)
    uid = _user(auth_db, "u1")
    resp = await getattr(app.test_client(), method)(path, headers=_hdr(uid))
    assert resp.status_code == 404
    assert "provider_disabled" in (await resp.get_data(as_text=True)).lower()
    assert len(auth_db.dal(auth_db.dal.hub_oauth_states).select()) == 0


@pytest.mark.parametrize(
    ("path", "error_path"),
    [
        ("/api/v1/auth/oauth/slack/callback", "/login?error=provider_disabled"),
        ("/api/v1/auth/oauth/slack/link-callback", "/dashboard/settings?error=provider_disabled"),
    ],
)
async def test_callback_refused_for_disabled_provider(
    app: Quart, monkeypatch: pytest.MonkeyPatch, path: str, error_path: str
) -> None:
    _gate(monkeypatch)
    resp = await app.test_client().get(f"{path}?code=c&state=s")
    assert resp.status_code in (302, 303)
    assert resp.headers["Location"].endswith(error_path)


async def test_unknown_provider_still_refused(app: Quart, monkeypatch: pytest.MonkeyPatch) -> None:
    _gate(monkeypatch)
    resp = await app.test_client().get("/api/v1/auth/oauth/myspace")
    assert resp.status_code == 400


async def test_enabled_provider_start_unchanged(
    app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch)
    resp = await app.test_client().get("/api/v1/auth/oauth/discord")
    assert resp.status_code == 200
