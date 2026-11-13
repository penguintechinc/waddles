"""Blueprint tests for POST /api/v1/apps/approvals/bulk."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.bundle_approvals_bulk import BLUEPRINTS
from tests.conftest import make_token

_APP = "waddles.socials.music.default"
_MANIFEST = {
    "schema_version": 2,
    "app_id": _APP,
    "name": "Music Station",
    "version": "3.0.1",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "x:y",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        }
    },
}
URL = "/api/v1/apps/approvals/bulk"


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    """Quart app with one PUBLISHED version (3.0.1) and two UPLOADED ones (1.0.0, 1.0.1)."""
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id=_APP,
        version="3.0.1",
        artifact_digest="sha256:" + "a" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=_APP,
        version="3.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=_MANIFEST,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )
    for ver in ("1.0.0", "1.0.1"):
        await install_dal.app_version_uploads.async_insert(
            app_id=_APP,
            version=ver,
            tenant_id=1,
            artifact_kind="source",
            language="python",
            status="UPLOADED",
            manifest_json={**_MANIFEST, "version": ver},
            created_at=now,
            updated_at=now,
        )
    app = Quart(__name__)
    QuartSchema(app)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


def _hdr(scope: str = "platform:admin", **kw: str) -> dict[str, str]:
    """Bearer header for a numeric user 1."""
    return {"Authorization": f"Bearer {make_token(scope=scope, user_id='1', **kw)}"}


def _item(version: str, app_id: str = _APP) -> dict[str, str]:
    """One bulk item."""
    return {"appId": app_id, "version": version}


async def test_all_succeed_approve(app: Quart) -> None:
    """A valid approve yields approved + a clean summary."""
    r = await app.test_client().post(
        URL, headers=_hdr(), json={"action": "approve", "items": [_item("3.0.1")]}
    )
    assert r.status_code == 200
    body = await r.get_json()
    assert body["results"][0]["status"] == "approved"
    assert body["summary"] == {"total": 1, "succeeded": 1, "failed": 0}
    assert body["success"] is True


async def test_reject_all_succeed(app: Quart) -> None:
    """Reject moves non-terminal versions to rejected."""
    r = await app.test_client().post(
        URL,
        headers=_hdr(),
        json={"action": "reject", "items": [_item("1.0.0"), _item("1.0.1")], "reason": "nope"},
    )
    body = await r.get_json()
    assert [x["status"] for x in body["results"]] == ["rejected", "rejected"]
    assert body["summary"]["succeeded"] == 2


async def test_partial_failure_does_not_abort(app: Quart) -> None:
    """A missing version errors; the next item still succeeds; HTTP stays 200."""
    r = await app.test_client().post(
        URL,
        headers=_hdr(),
        json={"action": "approve", "items": [_item("9.9.9"), _item("3.0.1")]},
    )
    assert r.status_code == 200
    body = await r.get_json()
    assert body["results"][0]["status"] == "error"
    assert body["results"][0]["error"]["code"]
    assert body["results"][1]["status"] == "approved"
    assert body["summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    assert body["success"] is False


async def test_already_installed(app: Quart, monkeypatch: pytest.MonkeyPatch) -> None:
    """A uq_app_global_installs_current violation on a current install is already_installed."""
    from sqlalchemy.exc import IntegrityError

    from services import bundle_approval_service as svc

    client = app.test_client()
    first = await client.post(
        URL, headers=_hdr(), json={"action": "approve", "items": [_item("3.0.1")]}
    )
    assert (await first.get_json())["results"][0]["status"] == "approved"

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise IntegrityError("insert", {}, Exception("uq_app_global_installs_current"))

    monkeypatch.setattr(svc, "install_version_globally", _boom)
    r = await client.post(
        URL, headers=_hdr(), json={"action": "approve", "items": [_item("3.0.1")]}
    )
    assert r.status_code == 200
    body = await r.get_json()
    assert body["results"][0]["status"] == "already_installed"
    assert body["summary"]["failed"] == 0


async def test_integrity_error_without_current_install_is_error(
    app: Quart, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An IntegrityError with no current install row is a real error, not already_installed."""
    from sqlalchemy.exc import IntegrityError

    from services import bundle_approval_service as svc

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise IntegrityError("insert", {}, Exception("other"))

    monkeypatch.setattr(svc, "install_version_globally", _boom)
    r = await app.test_client().post(
        URL, headers=_hdr(), json={"action": "approve", "items": [_item("3.0.1")]}
    )
    body = await r.get_json()
    assert body["results"][0]["status"] == "error"
    assert body["results"][0]["error"]["code"] == "internal_error"


async def test_more_than_200_rejected(app: Quart) -> None:
    """201 items is a 400."""
    items = [_item(f"1.0.{i}") for i in range(201)]
    r = await app.test_client().post(
        URL, headers=_hdr(), json={"action": "approve", "items": items}
    )
    assert r.status_code == 400


async def test_empty_and_bad_action_400(app: Quart) -> None:
    """Empty items and an unknown action are both 400."""
    client = app.test_client()
    r = await client.post(URL, headers=_hdr(), json={"action": "approve", "items": []})
    assert r.status_code == 400
    r = await client.post(URL, headers=_hdr(), json={"action": "nuke", "items": [_item("1.0.0")]})
    assert r.status_code == 400


async def test_unauthorized_403(app: Quart) -> None:
    """Without platform:admin the call is refused."""
    r = await app.test_client().post(
        URL, headers=_hdr(scope=""), json={"action": "approve", "items": [_item("3.0.1")]}
    )
    assert r.status_code == 403


async def test_cross_tenant_scoping_matches_single(app: Quart) -> None:
    """Another tenant without platform:admin, or no token at all, is refused."""
    client = app.test_client()
    r = await client.post(
        URL,
        headers=_hdr(scope="vendor:onboard", tenant="other-tenant"),
        json={"action": "approve", "items": [_item("3.0.1")]},
    )
    assert r.status_code in (401, 403)
    r = await client.post(URL, json={"action": "approve", "items": [_item("3.0.1")]})
    assert r.status_code in (401, 403)
