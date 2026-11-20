"""Tests for `authorize_onboarding_scope()`/`enforce_vendor_namespace()`."""

from __future__ import annotations

from types import SimpleNamespace

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


class _FakeUploads:
    """Stand-in `install_dal.app_version_uploads` (field refs are compared to plain values)."""

    app_id = "app_id"
    requested_by = "requested_by"


class _FakeDal:
    """Minimal DAL double: `dal(query).select(...)` returns the preset upload rows."""

    def __init__(self, owners: list[int | None]) -> None:
        self.app_version_uploads = _FakeUploads()
        self._rows = [SimpleNamespace(requested_by=o) for o in owners]

    def __call__(self, _query: object) -> _FakeDal:
        return self

    async def select(self, *_fields: object) -> list[SimpleNamespace]:
        return self._rows


_NOBODY = _FakeDal([])


async def test_valid_new_vendor_id_passes() -> None:
    await enforce_vendor_namespace(_NOBODY, app_id="acmecorp.widgets.mybundle", caller_id=42)
    await enforce_vendor_namespace(_NOBODY, app_id="acme-corp.my_repo.discord-invite", caller_id=42)


async def test_vendors_own_new_version_passes() -> None:
    dal = _FakeDal([42, 42])
    await enforce_vendor_namespace(dal, app_id="acmecorp.widgets.mybundle", caller_id=42)


async def test_vendor_b_cannot_publish_an_app_id_owned_by_vendor_a() -> None:
    dal = _FakeDal([42])
    with pytest.raises(ApiError) as exc:
        await enforce_vendor_namespace(dal, app_id="acmecorp.widgets.mybundle", caller_id=99)
    assert exc.value.status_code == 403


async def test_system_seeded_app_id_cannot_be_taken_over_by_a_vendor() -> None:
    dal = _FakeDal([None])  # requested_by NULL = system actor / seeder
    with pytest.raises(ApiError) as exc:
        await enforce_vendor_namespace(dal, app_id="acmecorp.widgets.seeded", caller_id=42)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "app_id",
    [
        pytest.param("penguintechinc.waddles.x", id="first-party-namespace"),
        pytest.param("penguintechinc.other.x", id="reserved-org-other-repo"),
        pytest.param("penguintechinc.waddles.discord-invite", id="first-party-hyphenated"),
        pytest.param("waddles.core.x", id="legacy-waddles-org"),
        pytest.param("psychoboy.penguintwitchbot.roll", id="platform-hosted-port"),
        pytest.param("psychoboy.anything.x", id="platform-hosted-org-any-repo"),
        pytest.param("acmecorp.widgets", id="two-segments"),
        pytest.param("acmecorp.widgets.a.b", id="four-segments"),
        pytest.param("AcmeCorp.widgets.x", id="uppercase"),
        pytest.param("acmecorp..x", id="empty-segment"),
        pytest.param("acmecorp.widgets." + "x" * 120, id="over-127-chars"),
    ],
)
async def test_vendor_path_rejects_reserved_and_malformed_ids(app_id: str) -> None:
    with pytest.raises(ApiError) as exc:
        await enforce_vendor_namespace(_NOBODY, app_id=app_id, caller_id=42)
    assert exc.value.status_code == 403


async def test_reserved_org_check_is_on_the_segment_not_a_string_prefix() -> None:
    """`penguintechinc-fan` is a different org from `penguintechinc` and is not reserved."""
    await enforce_vendor_namespace(_NOBODY, app_id="penguintechinc-fan.waddles.x", caller_id=42)
