"""Blueprint tests for the marketplace-sources admin CRUD (flag, tier, ack, DTO, built-in rules).

The persistence layer has its own real-Postgres suite (`test_marketplace_source_service.py`);
here the service functions are swapped for an in-memory store so the gates and DTO shape are
what is under test.
"""

from __future__ import annotations

import base64
import dataclasses
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1 import marketplace_sources as bp
from services import url_guard
from services.errors import conflict, not_found
from services.marketplace_publisher_attestation import fingerprint_publisher_key
from services.marketplace_sandbox_warning import WARNING_HASH
from services.marketplace_source_ingest import IngestSummary
from services.marketplace_source_scanner import ScanRejectedError, ScanResult
from services.marketplace_source_service import BuildMode, SourceRecord, SourceStatus
from tests.conftest import make_user_token

SHA = "a" * 40
SHA2 = "b" * 40
_PK = base64.b64encode(b"\x07" * 32).decode()
URL = "https://github.com/acme/bundles"
NUMERIC_LEAK_KEYS = {"id", "created_by", "sandbox_ack_by", "createdBy", "sandboxAckBy"}


def _record(**kw: Any) -> SourceRecord:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "public_id": uuid.uuid4(), "name": "acme", "is_builtin": False, "git_url": URL,
        "ref": "main", "pinned_commit_sha": SHA, "pending_commit_sha": None,
        "build_mode": BuildMode.PREBUILT, "publisher_key_fingerprint": None,
        "publisher_public_key": None, "sandbox_ack_by": None, "sandbox_ack_at": None,
        "sandbox_ack_text_hash": None, "sandbox_ack_commit_sha": None,
        "status": SourceStatus.PENDING_SCAN, "last_scanned_at": None, "last_scan_error": None,
        "created_by": 7, "created_at": now, "updated_at": now,
    }  # fmt: skip
    base.update(kw)
    return SourceRecord(**base)


class Store:
    """In-memory stand-in for the `marketplace_source_service` functions the blueprint uses."""

    def __init__(self) -> None:
        """Start empty."""
        self.rows: dict[uuid.UUID, SourceRecord] = {}
        self.created_kwargs: dict[str, Any] = {}
        self.has_versions = False
        self.deactivated: list[uuid.UUID] = []
        self.launched: list[tuple[SourceRecord, dict[str, Any]]] = []
        self.prebuilt_launched: list[tuple[SourceRecord, dict[str, Any]]] = []

    async def mark_pending_scan(self, dal: Any, source: SourceRecord) -> SourceRecord:
        self.rows[source.public_id] = dataclasses.replace(source, status=SourceStatus.PENDING_SCAN)
        return self.rows[source.public_id]

    def launch_sandbox_scan(self, dal: Any, source: SourceRecord, **kw: Any) -> None:
        self.launched.append((source, kw))

    def launch_prebuilt_scan(self, dal: Any, source: SourceRecord, **kw: Any) -> None:
        self.prebuilt_launched.append((source, kw))

    async def create_source(self, dal: Any, **kw: Any) -> SourceRecord:
        self.created_kwargs = kw
        ack = kw.get("sandbox_ack")
        rec = _record(
            name=kw["name"], git_url=kw["git_url"], ref=kw["ref"], build_mode=kw["build_mode"],
            pinned_commit_sha=kw["pinned_commit_sha"], created_by=kw["created_by"],
            publisher_key_fingerprint=kw["publisher_key_fingerprint"],
            publisher_public_key=kw["publisher_public_key"],
            sandbox_ack_by=ack.by if ack else None, sandbox_ack_at=ack.at if ack else None,
        )  # fmt: skip
        self.rows[rec.public_id] = rec
        return rec

    async def get_source(self, dal: Any, pid: uuid.UUID) -> SourceRecord | None:
        return self.rows.get(pid)

    async def list_sources(self, dal: Any, **kw: Any) -> tuple[SourceRecord, ...]:
        return tuple(self.rows.values())

    async def delete_source(self, dal: Any, pid: uuid.UUID, **kw: Any) -> None:
        if self.rows[pid].is_builtin:
            raise conflict("builtin")
        if self.has_versions:
            raise conflict("still has bundle versions")
        del self.rows[pid]

    async def disable_and_deactivate_source(
        self, dal: Any, pid: uuid.UUID, *, actor_id: int | None = None
    ) -> tuple[SourceRecord, tuple[str, ...]]:
        import dataclasses

        self.rows[pid] = dataclasses.replace(self.rows[pid], status=SourceStatus.DISABLED)
        self.deactivated.append(pid)
        return self.rows[pid], ("app.one",)

    async def approve_update(
        self,
        dal: Any,
        pid: uuid.UUID,
        *,
        expected_pending_sha: str,
        sandbox_ack: Any = None,
        actor_id: int | None = None,
    ) -> SourceRecord:
        import dataclasses

        r = self.rows.get(pid)
        if r is None:
            raise not_found("x")
        if r.pending_commit_sha is None or r.pending_commit_sha != expected_pending_sha:
            raise conflict("no pending update")
        extra: dict[str, Any] = {}
        if sandbox_ack is not None:
            extra = {
                "sandbox_ack_by": sandbox_ack.by, "sandbox_ack_at": sandbox_ack.at,
                "sandbox_ack_text_hash": sandbox_ack.text_hash,
                "sandbox_ack_commit_sha": expected_pending_sha,
            }  # fmt: skip
        self.rows[pid] = dataclasses.replace(
            r, pinned_commit_sha=r.pending_commit_sha, pending_commit_sha=None,
            status=SourceStatus.ACTIVE, **extra,
        )  # fmt: skip
        return self.rows[pid]


class Env:
    """Mutable test knobs: flag + tier."""

    flag = True
    tier = "enterprise"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    s = Store()
    for name in ("create_source", "get_source", "list_sources", "delete_source",
                 "disable_and_deactivate_source", "approve_update"):  # fmt: skip
        monkeypatch.setattr(bp.svc, name, getattr(s, name))
    monkeypatch.setattr(bp, "mark_pending_scan", s.mark_pending_scan)
    monkeypatch.setattr(bp, "launch_sandbox_scan", s.launch_sandbox_scan)
    monkeypatch.setattr(bp, "launch_prebuilt_scan", s.launch_prebuilt_scan)
    return s


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    e = Env()
    e.flag, e.tier = True, "enterprise"

    async def _flag(key: str, **kw: Any) -> bool:
        assert key == "waddles.marketplace-sources"
        return e.flag

    async def _tier(**kw: Any) -> str:
        return e.tier

    monkeypatch.setattr(bp, "feature_enabled", _flag)
    monkeypatch.setattr(bp, "get_tier", _tier)
    monkeypatch.setattr(url_guard, "_resolve_and_check_sync", lambda host: None)  # no real DNS
    return e


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any, store: Store, env: Env) -> Quart:
    app = Quart(__name__)
    QuartSchema(app)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    for b in bp.BLUEPRINTS:
        app.register_blueprint(b)
    return app


def _h(scope: str = "platform:admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=1, scope=scope)}"}


def _body(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "acme", "git_url": URL, "ref": "main", "build_mode": "prebuilt",
        "commit_sha": SHA, "publisher_public_key": _PK,
    }  # fmt: skip
    base.update(kw)
    return base


def _sandbox(**kw: Any) -> dict[str, Any]:
    b = _body(build_mode="sandbox", sandbox_trust_ack=WARNING_HASH, **kw)
    b.pop("publisher_public_key")
    return b


async def test_add_prebuilt_happy_path_dto_shape(app: Quart, store: Store, env: Env) -> None:
    env.tier = "professional"
    r = await app.test_client().post("/api/v1/marketplace/sources", json=_body(), headers=_h())
    assert r.status_code == 201
    src = (await r.get_json())["source"]
    assert set(src) == {
        "publicId", "name", "isBuiltin", "gitUrl", "ref", "pinnedCommitSha", "pendingCommitSha",
        "buildMode", "publisherKeyFingerprint", "sandboxAcknowledged", "sandboxAckAt", "status",
        "lastScannedAt", "lastScanError", "createdAt", "updatedAt",
    }  # fmt: skip
    assert not NUMERIC_LEAK_KEYS & set(src)
    assert "publisher_public_key" not in src and _PK not in str(src)
    assert src["buildMode"] == "prebuilt" and src["isBuiltin"] is False
    assert src["publisherKeyFingerprint"] == fingerprint_publisher_key(_PK)
    assert store.created_kwargs["pinned_commit_sha"] == SHA
    assert store.created_kwargs["created_by"] == 1


@pytest.mark.parametrize(
    ("tier", "mode", "status"),
    [
        ("free", "prebuilt", 402),
        ("professional", "prebuilt", 201),
        ("enterprise", "prebuilt", 201),
        ("free", "sandbox", 402),
        ("professional", "sandbox", 402),
        ("enterprise", "sandbox", 201),
    ],
)
async def test_tier_matrix(app: Quart, env: Env, tier: str, mode: str, status: int) -> None:
    env.tier = tier
    body = _body() if mode == "prebuilt" else _sandbox()
    r = await app.test_client().post("/api/v1/marketplace/sources", json=body, headers=_h())
    assert r.status_code == status
    if status == 402:
        err = (await r.get_json())["error"]
        assert err["code"] == "UPGRADE_REQUIRED"
        assert err["details"]["requiredTier"] == (
            "enterprise" if mode == "sandbox" else "professional"
        )


async def test_sandbox_add_stores_ack(app: Quart, store: Store) -> None:
    r = await app.test_client().post("/api/v1/marketplace/sources", json=_sandbox(), headers=_h())
    assert r.status_code == 201
    ack = store.created_kwargs["sandbox_ack"]
    assert (ack.by, ack.text_hash, ack.commit_sha) == (1, WARNING_HASH, SHA)
    assert (await r.get_json())["source"]["sandboxAcknowledged"] is True


@pytest.mark.parametrize("ack", [None, "", "0" * 64, "stale-hash"])
async def test_sandbox_without_valid_ack_rejected_422(app: Quart, store: Store, ack: Any) -> None:
    body = _sandbox()
    body["sandbox_trust_ack"] = ack
    r = await app.test_client().post("/api/v1/marketplace/sources", json=body, headers=_h())
    assert r.status_code == 422
    assert store.rows == {}


async def test_is_builtin_not_settable(app: Quart, store: Store) -> None:
    r = await app.test_client().post(
        "/api/v1/marketplace/sources", json=_body(is_builtin=True), headers=_h()
    )
    assert r.status_code == 400
    assert store.rows == {}


async def test_flag_off_disables_every_endpoint(app: Quart, env: Env) -> None:
    env.flag = False
    c, pid = app.test_client(), uuid.uuid4()
    base = "/api/v1/marketplace/sources"
    calls = [
        await c.post(base, json=_body(), headers=_h()),
        await c.get(base, headers=_h()),
        await c.get(f"{base}/{pid}", headers=_h()),
        await c.delete(f"{base}/{pid}", headers=_h()),
        await c.post(f"{base}/{pid}/rescan", headers=_h()),
        await c.post(f"{base}/{pid}/approve-update", headers=_h()),
    ]
    assert [r.status_code for r in calls] == [404] * 6


@pytest.mark.parametrize("scope", ["", "marketplace:admin", "bundles:read"])
async def test_non_platform_admin_403(app: Quart, scope: str) -> None:
    c = app.test_client()
    r = await c.post("/api/v1/marketplace/sources", json=_body(), headers=_h(scope))
    assert r.status_code == 403
    assert (await c.get("/api/v1/marketplace/sources", headers=_h(scope))).status_code == 403


async def test_unauthenticated_rejected(app: Quart) -> None:
    r = await app.test_client().get("/api/v1/marketplace/sources")
    assert r.status_code in (401, 403)


@pytest.mark.parametrize(
    "url",
    ["http://github.com/a/b", "git://github.com/a/b", "ssh://git@github.com/a/b",
     "https://user:pw@github.com/a/b", "file:///etc", "github.com/a/b",
     "https://127.0.0.1/a/b", "https://169.254.169.254/a", "https://localhost/a"],
)  # fmt: skip
async def test_invalid_or_ssrf_git_url_rejected(app: Quart, store: Store, url: str) -> None:
    r = await app.test_client().post(
        "/api/v1/marketplace/sources", json=_body(git_url=url), headers=_h()
    )
    assert r.status_code == 400
    assert store.rows == {}


async def test_bad_commit_sha_and_missing_key_rejected(app: Quart, store: Store) -> None:
    c = app.test_client()
    r = await c.post("/api/v1/marketplace/sources", json=_body(commit_sha="main"), headers=_h())
    assert r.status_code == 400
    body = _body()
    del body["publisher_public_key"]
    r = await c.post("/api/v1/marketplace/sources", json=body, headers=_h())
    assert r.status_code == 400  # prebuilt requires a publisher key


async def test_list_and_get_use_dto(app: Quart, store: Store) -> None:
    rec = _record()
    store.rows[rec.public_id] = rec
    c = app.test_client()
    listed = await (await c.get("/api/v1/marketplace/sources", headers=_h())).get_json()
    assert [s["publicId"] for s in listed["sources"]] == [str(rec.public_id)]
    got = await c.get(f"/api/v1/marketplace/sources/{rec.public_id}", headers=_h())
    assert got.status_code == 200
    assert not NUMERIC_LEAK_KEYS & set((await got.get_json())["source"])
    assert (
        await c.get(f"/api/v1/marketplace/sources/{uuid.uuid4()}", headers=_h())
    ).status_code == 404
    assert (await c.get("/api/v1/marketplace/sources/not-a-uuid", headers=_h())).status_code == 404


async def test_delete_external_and_disable_when_versions_exist(app: Quart, store: Store) -> None:
    a, b = _record(), _record()
    store.rows |= {a.public_id: a, b.public_id: b}
    c = app.test_client()
    r = await c.delete(f"/api/v1/marketplace/sources/{a.public_id}", headers=_h())
    assert (r.status_code, (await r.get_json())["disposition"]) == (200, "deleted")
    store.has_versions = True
    r = await c.delete(f"/api/v1/marketplace/sources/{b.public_id}", headers=_h())
    assert (await r.get_json())["disposition"] == "disabled"
    assert store.rows[b.public_id].status == SourceStatus.DISABLED


async def test_builtin_cannot_be_deleted(app: Quart, store: Store) -> None:
    rec = _record(is_builtin=True, build_mode=BuildMode.BUILTIN_IMAGE, git_url=None, ref=None)
    store.rows[rec.public_id] = rec
    r = await app.test_client().delete(f"/api/v1/marketplace/sources/{rec.public_id}", headers=_h())
    assert r.status_code == 409
    assert rec.public_id in store.rows


def _approve_url(rec: SourceRecord) -> str:
    return f"/api/v1/marketplace/sources/{rec.public_id}/approve-update"


async def test_approve_update_advances_pin_only_when_pending(app: Quart, store: Store) -> None:
    rec = _record(pending_commit_sha=None)
    store.rows[rec.public_id] = rec
    c = app.test_client()
    url = _approve_url(rec)
    assert (await c.post(url, json={"pending_commit_sha": SHA2}, headers=_h())).status_code == 409
    assert store.rows[rec.public_id].pinned_commit_sha == SHA
    store.rows[rec.public_id] = dataclasses.replace(rec, pending_commit_sha=SHA2)
    r = await c.post(url, json={"pending_commit_sha": SHA2}, headers=_h())
    assert r.status_code == 200
    src = (await r.get_json())["source"]
    assert (src["pinnedCommitSha"], src["pendingCommitSha"]) == (SHA2, None)


async def test_approve_update_requires_body_and_valid_sha(app: Quart, store: Store) -> None:
    rec = _record(pending_commit_sha=SHA2)
    store.rows[rec.public_id] = rec
    c = app.test_client()
    assert (await c.post(_approve_url(rec), headers=_h())).status_code in (400, 415)
    r = await c.post(_approve_url(rec), json={"pending_commit_sha": "zz"}, headers=_h())
    assert r.status_code == 400
    assert store.rows[rec.public_id].pinned_commit_sha == SHA


async def test_approve_update_disabled_409(app: Quart, store: Store) -> None:
    rec = _record(pending_commit_sha=SHA2, status=SourceStatus.DISABLED)
    store.rows[rec.public_id] = rec
    r = await app.test_client().post(
        _approve_url(rec), json={"pending_commit_sha": SHA2}, headers=_h()
    )
    assert r.status_code == 409
    assert store.rows[rec.public_id].pinned_commit_sha == SHA


async def test_approve_update_below_tier_402(app: Quart, store: Store, env: Env) -> None:
    prebuilt = _record(pending_commit_sha=SHA2)
    sandbox = _record(pending_commit_sha=SHA2, build_mode=BuildMode.SANDBOX)
    store.rows |= {prebuilt.public_id: prebuilt, sandbox.public_id: sandbox}
    c = app.test_client()
    env.tier = "free"
    r = await c.post(_approve_url(prebuilt), json={"pending_commit_sha": SHA2}, headers=_h())
    assert r.status_code == 402
    env.tier = "professional"
    body = {"pending_commit_sha": SHA2, "sandbox_trust_ack": WARNING_HASH}
    r = await c.post(_approve_url(sandbox), json=body, headers=_h())
    assert r.status_code == 402
    assert store.rows[sandbox.public_id].pinned_commit_sha == SHA


@pytest.mark.parametrize("ack", [None, "stale-hash"])
async def test_approve_update_sandbox_needs_fresh_ack_422(
    app: Quart, store: Store, ack: str | None
) -> None:
    rec = _record(pending_commit_sha=SHA2, build_mode=BuildMode.SANDBOX)
    store.rows[rec.public_id] = rec
    r = await app.test_client().post(
        _approve_url(rec), json={"pending_commit_sha": SHA2, "sandbox_trust_ack": ack},
        headers=_h(),
    )  # fmt: skip
    assert r.status_code == 422
    assert store.rows[rec.public_id].pinned_commit_sha == SHA


async def test_approve_update_sandbox_with_ack_updates_ack_to_new_commit(
    app: Quart, store: Store
) -> None:
    rec = _record(pending_commit_sha=SHA2, build_mode=BuildMode.SANDBOX, sandbox_ack_commit_sha=SHA)
    store.rows[rec.public_id] = rec
    r = await app.test_client().post(
        _approve_url(rec), json={"pending_commit_sha": SHA2, "sandbox_trust_ack": WARNING_HASH},
        headers=_h(),
    )  # fmt: skip
    assert r.status_code == 200
    row = store.rows[rec.public_id]
    assert (row.pinned_commit_sha, row.sandbox_ack_commit_sha) == (SHA2, SHA2)
    assert (row.sandbox_ack_by, row.sandbox_ack_text_hash) == (1, WARNING_HASH)
    assert row.sandbox_ack_at is not None


async def test_approve_update_echoed_sha_mismatch_409(app: Quart, store: Store) -> None:
    rec = _record(pending_commit_sha=SHA2)
    store.rows[rec.public_id] = rec
    r = await app.test_client().post(
        _approve_url(rec), json={"pending_commit_sha": "c" * 40}, headers=_h()
    )
    assert r.status_code == 409
    assert store.rows[rec.public_id].pinned_commit_sha == SHA


async def test_delete_disable_path_deactivates_bundles(app: Quart, store: Store) -> None:
    a, b = _record(), _record()
    store.rows |= {a.public_id: a, b.public_id: b}
    c = app.test_client()
    await c.delete(f"/api/v1/marketplace/sources/{a.public_id}", headers=_h())
    assert store.deactivated == []  # hard-deleted: no versions, nothing installed
    store.has_versions = True
    r = await c.delete(f"/api/v1/marketplace/sources/{b.public_id}", headers=_h())
    assert (await r.get_json())["disposition"] == "disabled"
    assert store.deactivated == [b.public_id]


async def test_rescan_runs_scan_then_ingest_with_tenant_ctx(
    app: Quart, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _record()
    store.rows[rec.public_id] = rec
    seen: dict[str, Any] = {}

    async def fake_scan(
        source: SourceRecord, *, workdir: Path, dal: Any = None, actor_id: int | None = None
    ) -> ScanResult:
        assert str(workdir)
        return ScanResult(source.public_id, SHA, (), (), 3)

    async def fake_ingest(dal: Any, source: Any, result: Any, **kw: Any) -> IngestSummary:
        seen.update(kw)
        return IngestSummary(created=2, rejected=1)

    monkeypatch.setattr(bp, "scan_source", fake_scan)
    monkeypatch.setattr(bp, "ingest_candidates", fake_ingest)
    r = await app.test_client().post(
        f"/api/v1/marketplace/sources/{rec.public_id}/rescan", headers=_h()
    )
    body = await r.get_json()
    assert r.status_code == 200
    assert (body["examined"], body["created"], body["rejectedAtIngest"]) == (3, 2, 1)
    assert seen["tenant_slug"] and isinstance(seen["tenant_id"], int)
    assert seen["fetch_wasm"] is bp._fetch_wasm
    assert seen["requested_by"] == 1


async def test_rescan_scan_rejection_is_422_reason_only(
    app: Quart, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _record()
    store.rows[rec.public_id] = rec

    async def boom(*a: Any, **k: Any) -> ScanResult:
        raise ScanRejectedError("sha_mismatch", "secret internal detail /tmp/x")

    monkeypatch.setattr(bp, "scan_source", boom)
    r = await app.test_client().post(
        f"/api/v1/marketplace/sources/{rec.public_id}/rescan", headers=_h()
    )
    assert r.status_code == 422
    assert "secret internal detail" not in str(await r.get_json())


async def test_rescan_refuses_builtin_sandbox_and_below_tier(
    app: Quart, store: Store, env: Env
) -> None:
    built = _record(is_builtin=True, build_mode=BuildMode.BUILTIN_IMAGE)
    sbx = _record(build_mode=BuildMode.SANDBOX)
    pre = _record()
    store.rows |= {built.public_id: built, sbx.public_id: sbx, pre.public_id: pre}
    c = app.test_client()
    base = "/api/v1/marketplace/sources"
    assert (await c.post(f"{base}/{built.public_id}/rescan", headers=_h())).status_code == 409
    assert store.launched == []
    env.tier = "free"
    assert (await c.post(f"{base}/{pre.public_id}/rescan", headers=_h())).status_code == 402


async def test_rescan_sandbox_launches_async_build_no_409(app: Quart, store: Store) -> None:
    """G1: a SANDBOX source's rescan is no longer a 409 -- it launches the background build."""
    sbx = _record(build_mode=BuildMode.SANDBOX, status=SourceStatus.ACTIVE)
    store.rows[sbx.public_id] = sbx
    r = await app.test_client().post(
        f"/api/v1/marketplace/sources/{sbx.public_id}/rescan", headers=_h()
    )
    body = await r.get_json()
    assert r.status_code == 202
    assert body["status"] == "pending_scan"
    assert len(store.launched) == 1
    src, kw = store.launched[0]
    assert src.public_id == sbx.public_id and src.status is SourceStatus.PENDING_SCAN
    assert kw["tier"] == "enterprise" and kw["requested_by"] == 1
    assert kw["tenant_slug"] and isinstance(kw["tenant_id"], int)


async def test_rescan_sandbox_below_tier_402_never_launches(
    app: Quart, store: Store, env: Env
) -> None:
    sbx = _record(build_mode=BuildMode.SANDBOX)
    store.rows[sbx.public_id] = sbx
    env.tier = "professional"
    r = await app.test_client().post(
        f"/api/v1/marketplace/sources/{sbx.public_id}/rescan", headers=_h()
    )
    assert r.status_code == 402 and store.launched == []


async def test_rescan_builtin_still_409(app: Quart, store: Store) -> None:
    built = _record(is_builtin=True, build_mode=BuildMode.BUILTIN_IMAGE)
    store.rows[built.public_id] = built
    r = await app.test_client().post(
        f"/api/v1/marketplace/sources/{built.public_id}/rescan", headers=_h()
    )
    assert r.status_code == 409 and store.launched == []


async def test_add_sandbox_launches_build_prebuilt_launches_scan_not_build(
    app: Quart, store: Store
) -> None:
    c = app.test_client()
    r = await c.post("/api/v1/marketplace/sources", json=_body(), headers=_h())
    assert r.status_code == 201 and store.launched == []  # no sandbox build for prebuilt
    assert len(store.prebuilt_launched) == 1  # G2b: initial scan+ingest is launched
    src, kw = store.prebuilt_launched[0]
    assert src.build_mode is BuildMode.PREBUILT and src.status is SourceStatus.PENDING_SCAN
    assert {"tenant_id", "tenant_slug", "requested_by"} <= kw.keys()
    r = await c.post("/api/v1/marketplace/sources", json=_sandbox(), headers=_h())
    assert r.status_code == 201
    assert len(store.prebuilt_launched) == 1  # sandbox add does not take the prebuilt path
    assert len(store.launched) == 1
    assert store.launched[0][0].build_mode is BuildMode.SANDBOX


async def test_approve_update_sandbox_launches_build_of_new_pin(app: Quart, store: Store) -> None:
    sbx = _record(
        build_mode=BuildMode.SANDBOX, pending_commit_sha=SHA2, status=SourceStatus.NEEDS_APPROVAL
    )
    store.rows[sbx.public_id] = sbx
    r = await app.test_client().post(
        f"/api/v1/marketplace/sources/{sbx.public_id}/approve-update",
        json={"pending_commit_sha": SHA2, "sandbox_trust_ack": WARNING_HASH},
        headers=_h(),
    )
    assert r.status_code == 200
    assert len(store.launched) == 1
    assert store.launched[0][0].pinned_commit_sha == SHA2
