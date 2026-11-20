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

        auth = authorize_onboarding_scope(request, app_id="penguintechinc.waddles.music")
    assert auth.is_admin is True


async def test_vendor_scope_is_authorized(app: Quart) -> None:
    token = make_user_token(user_id=42, scope="vendor:onboard")
    async with app.test_request_context("/", headers={"Authorization": f"Bearer {token}"}):
        from quart import request

        auth = authorize_onboarding_scope(request, app_id="acmecorp.widgets.mybundle")
    assert auth.is_admin is False


async def test_no_scope_is_forbidden(app: Quart) -> None:
    token = make_token(scope="")
    async with app.test_request_context("/", headers={"Authorization": f"Bearer {token}"}):
        from quart import request

        with pytest.raises(ApiError) as exc:
            authorize_onboarding_scope(request, app_id="penguintechinc.waddles.music")
    assert exc.value.status_code == 403


async def test_missing_bearer_token_is_unauthorized(app: Quart) -> None:
    async with app.test_request_context("/"):
        from quart import request

        with pytest.raises(ApiError) as exc:
            authorize_onboarding_scope(request, app_id="penguintechinc.waddles.music")
    assert exc.value.status_code == 401


def test_valid_vendor_id_passes() -> None:
    enforce_vendor_namespace(app_id="acmecorp.widgets.mybundle")  # must not raise
    enforce_vendor_namespace(app_id="acme-corp.my_repo.discord-invite")  # must not raise


@pytest.mark.parametrize(
    "app_id",
    [
        pytest.param("penguintechinc.waddles.x", id="first-party-namespace"),
        pytest.param("penguintechinc.other.x", id="reserved-org-other-repo"),
        pytest.param("penguintechinc.waddles.discord-invite", id="first-party-hyphenated"),
        pytest.param("psychoboy.penguintwitchbot.roll", id="admin-only-port"),
        pytest.param("acmecorp.widgets", id="two-segments"),
        pytest.param("acmecorp.widgets.a.b", id="four-segments"),
        pytest.param("AcmeCorp.widgets.x", id="uppercase"),
        pytest.param("acmecorp..x", id="empty-segment"),
        pytest.param("acmecorp.widgets." + "x" * 120, id="over-127-chars"),
    ],
)
def test_vendor_path_rejects_reserved_and_malformed_ids(app_id: str) -> None:
    with pytest.raises(ApiError) as exc:
        enforce_vendor_namespace(app_id=app_id)
    assert exc.value.status_code == 403


def test_reserved_org_check_is_on_the_segment_not_a_string_prefix() -> None:
    """`penguintechinc-fan` is a different org from `penguintechinc` and is not reserved."""
    enforce_vendor_namespace(app_id="penguintechinc-fan.waddles.x")  # must not raise
