"""Tests for `authorize_onboarding_scope()`/`enforce_vendor_namespace()`."""

from __future__ import annotations

import pytest
from quart import Quart

from services.errors import ApiError
from services.vendor_bundle_authz import authorize_onboarding_scope, enforce_vendor_namespace
from tests.conftest import make_token, make_user_token


@pytest.fixture
def app() -> Quart:
    return Quart(__name__)


async def test_admin_scope_is_authorized(app: Quart) -> None:
    token = make_token(scope="platform:admin")
    async with app.test_request_context("/", headers={"Authorization": f"Bearer {token}"}):
        from quart import request

        auth = authorize_onboarding_scope(request, app_id="waddles.socials.music.default")
    assert auth.is_admin is True


async def test_vendor_scope_is_authorized(app: Quart) -> None:
    token = make_user_token(user_id=42, scope="vendor:onboard")
    async with app.test_request_context("/", headers={"Authorization": f"Bearer {token}"}):
        from quart import request

        auth = authorize_onboarding_scope(request, app_id="waddles.integrations.vendor-42.mybundle")
    assert auth.is_admin is False


async def test_no_scope_is_forbidden(app: Quart) -> None:
    token = make_token(scope="")
    async with app.test_request_context("/", headers={"Authorization": f"Bearer {token}"}):
        from quart import request

        with pytest.raises(ApiError) as exc:
            authorize_onboarding_scope(request, app_id="waddles.socials.music.default")
    assert exc.value.status_code == 403


async def test_missing_bearer_token_is_unauthorized(app: Quart) -> None:
    async with app.test_request_context("/"):
        from quart import request

        with pytest.raises(ApiError) as exc:
            authorize_onboarding_scope(request, app_id="waddles.socials.music.default")
    assert exc.value.status_code == 401


def test_vendor_may_onboard_their_own_namespace() -> None:
    enforce_vendor_namespace(
        app_id="waddles.integrations.vendor-42.mybundle", caller_id=42
    )  # must not raise


def test_vendor_cannot_onboard_core_namespace() -> None:
    with pytest.raises(ApiError) as exc:
        enforce_vendor_namespace(app_id="waddles.core.socials.music", caller_id=42)
    assert exc.value.status_code == 403


def test_vendor_cannot_onboard_another_vendors_namespace() -> None:
    with pytest.raises(ApiError) as exc:
        enforce_vendor_namespace(app_id="waddles.integrations.vendor-99.other", caller_id=42)
    assert exc.value.status_code == 403


def test_vendor_cannot_onboard_an_unrelated_namespace() -> None:
    with pytest.raises(ApiError) as exc:
        enforce_vendor_namespace(app_id="waddles.socials.music.default", caller_id=42)
    assert exc.value.status_code == 403
