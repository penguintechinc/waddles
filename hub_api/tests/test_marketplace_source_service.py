"""`marketplace_source_service` against a REAL, fully-migrated Postgres (migration 0067).

A SKIP means docker is absent -- treat it as a gate FAILURE, never a pass.
"""

# ruff: noqa: F811  -- imported pytest fixtures (`pg_db`/`pg_dal`) are re-bound as test params

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from services.errors import ApiError
from services.marketplace_publisher_attestation import fingerprint_publisher_key
from services.marketplace_source_service import (
    BuildMode,
    SandboxAck,
    SourceRecord,
    SourceStatus,
    create_source,
    delete_source,
    disable_source,
    get_source,
    list_sources,
    update_status,
)
from tests.test_seed_core_bundles_pg_integration import (  # noqa: F401  # fixtures
    pg_dal,
    pg_db,
)

SHA = "c" * 40


def _url() -> str:
    return f"https://github.com/acme/{uuid.uuid4().hex[:10]}"


_PK = base64.b64encode(b"\x07" * 32).decode()
_FP = fingerprint_publisher_key(_PK)


async def _prebuilt(dal: Any, **kw: Any) -> SourceRecord:
    return await create_source(
        dal,
        name="acme",
        git_url=kw.pop("git_url", _url()),
        ref="main",
        build_mode=BuildMode.PREBUILT,
        publisher_key_fingerprint=_FP,
        publisher_public_key=_PK,
        **kw,
    )


def test_enum_codes_are_stable_ints() -> None:
    assert [int(m) for m in BuildMode] == [1, 2, 3]
    assert BuildMode.PREBUILT == 1 and BuildMode.SANDBOX == 2 and BuildMode.BUILTIN_IMAGE == 3
    assert [(s.name, int(s)) for s in SourceStatus] == [
        ("PENDING_SCAN", 1),
        ("ACTIVE", 2),
        ("NEEDS_APPROVAL", 3),
        ("DISABLED", 4),
        ("ERROR", 5),
    ]


async def test_create_get_roundtrip_with_uuid_and_pending_status(pg_dal: Any) -> None:
    rec = await _prebuilt(pg_dal, created_by=3)
    assert isinstance(rec.public_id, uuid.UUID)
    assert rec.build_mode is BuildMode.PREBUILT and rec.status is SourceStatus.PENDING_SCAN
    assert not rec.is_builtin and rec.pinned_commit_sha is None and rec.created_by == 3
    assert await get_source(pg_dal, rec.public_id) == rec
    assert await get_source(pg_dal, uuid.uuid4()) is None


async def test_public_ids_unique(pg_dal: Any) -> None:
    a, b = await _prebuilt(pg_dal), await _prebuilt(pg_dal)
    assert a.public_id != b.public_id


async def test_sandbox_requires_and_stores_ack(pg_dal: Any) -> None:
    with pytest.raises(ApiError) as ei:
        await create_source(
            pg_dal, name="s", git_url=_url(), ref="main", build_mode=BuildMode.SANDBOX
        )
    assert ei.value.status_code == 400
    ack = SandboxAck(by=9, at=datetime.now(UTC), text_hash="d" * 64, commit_sha=SHA)
    rec = await create_source(
        pg_dal, name="s", git_url=_url(), ref="main", build_mode=BuildMode.SANDBOX, sandbox_ack=ack
    )
    assert (rec.sandbox_ack_by, rec.sandbox_ack_text_hash, rec.sandbox_ack_commit_sha) == (
        9,
        "d" * 64,
        SHA,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"git_url": "http://github.com/a/b"},
        {"git_url": "https://u:p@github.com/a/b"},
        {"git_url": "file:///etc/passwd"},
        {"ref": ""},
        {"name": "   "},
        {"build_mode": BuildMode.BUILTIN_IMAGE},
        {"publisher_public_key": None},
    ],
)
async def test_create_validation_rejects(pg_dal: Any, kwargs: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "name": "n",
        "git_url": _url(),
        "ref": "main",
        "build_mode": BuildMode.PREBUILT,
        "publisher_key_fingerprint": _FP,
        "publisher_public_key": _PK,
    }
    with pytest.raises(ApiError) as ei:
        await create_source(pg_dal, **{**base, **kwargs})
    assert ei.value.status_code == 400


async def test_list_puts_builtin_first_and_filters_disabled(pg_dal: Any) -> None:
    rec = await _prebuilt(pg_dal)
    await disable_source(pg_dal, rec.public_id)
    everything = await list_sources(pg_dal)
    assert everything[0].is_builtin and sum(r.is_builtin for r in everything) == 1
    assert rec.public_id in {r.public_id for r in everything}
    active_only = await list_sources(pg_dal, include_disabled=False)
    assert rec.public_id not in {r.public_id for r in active_only}


async def test_update_status_bookkeeping_never_moves_pin(pg_dal: Any) -> None:
    rec = await _prebuilt(pg_dal)
    out = await update_status(
        pg_dal, rec.public_id, SourceStatus.NEEDS_APPROVAL, pending_commit_sha=SHA, scanned=True
    )
    assert out.status is SourceStatus.NEEDS_APPROVAL and out.pending_commit_sha == SHA
    assert out.last_scanned_at is not None and out.pinned_commit_sha is None
    err = await update_status(pg_dal, rec.public_id, SourceStatus.ERROR, last_scan_error="boom")
    assert err.last_scan_error == "boom" and err.pending_commit_sha is None
    with pytest.raises(ApiError) as ei:
        await update_status(pg_dal, rec.public_id, SourceStatus.ACTIVE, pending_commit_sha="nothex")
    assert ei.value.status_code == 400
    with pytest.raises(ApiError) as ei2:
        await update_status(pg_dal, uuid.uuid4(), SourceStatus.ACTIVE)
    assert ei2.value.status_code == 404


async def test_delete_external_ok_builtin_refused_missing_404(pg_dal: Any) -> None:
    rec = await _prebuilt(pg_dal)
    await delete_source(pg_dal, rec.public_id)
    assert await get_source(pg_dal, rec.public_id) is None
    builtin = (await list_sources(pg_dal))[0]
    with pytest.raises(ApiError) as ei:
        await delete_source(pg_dal, builtin.public_id)
    assert ei.value.status_code == 409
    with pytest.raises(ApiError) as ei2:
        await delete_source(pg_dal, uuid.uuid4())
    assert ei2.value.status_code == 404


async def test_delete_source_with_referencing_catalog_row_conflicts(pg_dal: Any) -> None:
    rec = await _prebuilt(pg_dal)
    app_id = f"app-{uuid.uuid4().hex[:8]}"
    async with pg_dal.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO app_catalog (app_id, source_id) "
                "SELECT :a, id FROM marketplace_sources "
                "WHERE public_id = :p"
            ),
            {"a": app_id, "p": rec.public_id},
        )
    with pytest.raises(ApiError) as ei:
        await delete_source(pg_dal, rec.public_id)
    assert ei.value.status_code == 409


async def test_source_short_unique_index_and_create_retries_collision(
    pg_dal: Any, monkeypatch: Any
) -> None:
    from sqlalchemy import text

    first = await _prebuilt(pg_dal)
    clash = uuid.UUID(first.public_id.hex[:12] + "4" + "0" * 19)
    fresh = uuid.uuid4()
    seq = iter([clash, fresh])
    monkeypatch.setattr("services.marketplace_source_service.uuid.uuid4", lambda: next(seq))
    second = await _prebuilt(pg_dal)
    assert second.public_id == fresh
    async with pg_dal.engine.begin() as conn:
        n = (
            await conn.execute(
                text(
                    "SELECT count(DISTINCT left(replace(public_id::text,'-',''),12)) = count(*) "
                    "FROM marketplace_sources"
                )
            )
        ).scalar_one()
    assert n is True


async def test_create_with_pin_and_approve_update_promotes_pending(pg_dal: Any) -> None:
    from services.marketplace_source_service import approve_update

    rec = await _prebuilt(pg_dal, pinned_commit_sha=SHA)
    assert rec.pinned_commit_sha == SHA
    new = "d" * 40
    with pytest.raises(ApiError) as no_pending:  # nothing pending -> never moves the pin
        await approve_update(pg_dal, rec.public_id, expected_pending_sha=new)
    assert no_pending.value.status_code == 409
    await update_status(pg_dal, rec.public_id, SourceStatus.NEEDS_APPROVAL, pending_commit_sha=new)
    with pytest.raises(ApiError) as swapped:  # echoed SHA != stored pending SHA (TOCTOU guard)
        await approve_update(pg_dal, rec.public_id, expected_pending_sha="e" * 40)
    assert swapped.value.status_code == 409
    assert (await get_source(pg_dal, rec.public_id)).pinned_commit_sha == SHA  # type: ignore[union-attr]
    out = await approve_update(pg_dal, rec.public_id, expected_pending_sha=new)
    assert (out.pinned_commit_sha, out.pending_commit_sha) == (new, None)
    assert out.status is SourceStatus.ACTIVE
    with pytest.raises(ApiError) as missing:
        await approve_update(pg_dal, uuid.uuid4(), expected_pending_sha=new)
    assert missing.value.status_code == 404
    with pytest.raises(ApiError) as bad:
        await _prebuilt(pg_dal, pinned_commit_sha="nothex")
    assert bad.value.status_code == 400


async def test_approve_update_refuses_disabled_and_writes_sandbox_ack(pg_dal: Any) -> None:
    from services.marketplace_source_service import approve_update

    new = "d" * 40
    ack = SandboxAck(by=1, at=datetime.now(UTC), text_hash="h" * 64, commit_sha="stale")
    sb = await create_source(
        pg_dal, name="sb", git_url=_url(), ref="main", build_mode=BuildMode.SANDBOX,
        sandbox_ack=SandboxAck(by=1, at=datetime.now(UTC), text_hash="o" * 64, commit_sha=SHA),
        pinned_commit_sha=SHA,
    )  # fmt: skip
    await update_status(pg_dal, sb.public_id, SourceStatus.NEEDS_APPROVAL, pending_commit_sha=new)
    out = await approve_update(pg_dal, sb.public_id, expected_pending_sha=new, sandbox_ack=ack)
    assert out.sandbox_ack_commit_sha == new  # ack bound to the NEW commit, same UPDATE
    assert (out.sandbox_ack_text_hash, out.sandbox_ack_by) == ("h" * 64, 1)

    dis = await _prebuilt(pg_dal, pinned_commit_sha=SHA)
    await update_status(pg_dal, dis.public_id, SourceStatus.NEEDS_APPROVAL, pending_commit_sha=new)
    await disable_source(pg_dal, dis.public_id)
    with pytest.raises(ApiError) as ei:
        await approve_update(pg_dal, dis.public_id, expected_pending_sha=new)
    assert ei.value.status_code == 409


async def test_deactivate_source_bundles_revokes_install_keeps_versions(pg_dal: Any) -> None:
    from services.marketplace_source_service import deactivate_source_bundles, source_app_ids

    rec = await _prebuilt(pg_dal, pinned_commit_sha=SHA)
    app_id = f"app-{uuid.uuid4().hex[:8]}"
    other = f"app-{uuid.uuid4().hex[:8]}"  # not from this source: must stay installed
    async with pg_dal.engine.begin() as conn:
        pk = (
            await conn.execute(
                text("SELECT id FROM marketplace_sources WHERE public_id = :p"),
                {"p": rec.public_id},
            )
        ).scalar_one()
        for a, src in ((app_id, pk), (other, None)):
            await conn.execute(
                text("INSERT INTO app_catalog (app_id, source_id) VALUES (:a, :s)"),
                {"a": a, "s": src},
            )
            vid = (
                await conn.execute(
                    text(
                        "INSERT INTO app_versions (app_id, version, language, artifact_kind, "
                        "source_id) VALUES (:a, '1.0.0', 'rust', 'prebuilt', :s) RETURNING id"
                    ),
                    {"a": a, "s": src},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO app_global_installs (app_id, version, version_id, "
                    "permission_hash, summary_json, install_source, installed_at) "
                    "VALUES (:a, '1.0.0', :v, 'h', '{}', 'human', now())"
                ),
                {"a": a, "v": vid},
            )
    assert await source_app_ids(pg_dal, rec.public_id) == (app_id,)
    assert await deactivate_source_bundles(pg_dal, rec.public_id, revoked_by=None) == (app_id,)
    async with pg_dal.engine.connect() as conn:
        revoked = dict(
            (
                await conn.execute(
                    text(
                        "SELECT app_id, revoked_at IS NOT NULL FROM app_global_installs "
                        "WHERE app_id IN (:a, :b)"
                    ),
                    {"a": app_id, "b": other},
                )
            ).all()
        )
        n_versions = (
            await conn.execute(
                text("SELECT count(*) FROM app_versions WHERE app_id = :a"), {"a": app_id}
            )
        ).scalar_one()
    assert revoked == {app_id: True, other: False}
    assert n_versions == 1  # version history retained
    # idempotent: already-revoked bundle is skipped, not an error
    assert await deactivate_source_bundles(pg_dal, rec.public_id, revoked_by=None) == ()
