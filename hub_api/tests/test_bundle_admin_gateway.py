"""Generic bundle-admin gateway: manifest blocks, transport, service rules and HTTP routes.

sqlite-backed (the `bundle_install_db` fixture); the real-Postgres + real-reputation-manifest path
is `test_bundle_admin_gateway_pg_integration.py`. The host is a `FakeTransport` here -- the
boundary this module owns (hub-api -> signed request) is tested for real in the transport tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.bundle_admin_gateway import BLUEPRINTS
from services import bundle_tool_transport as tt
from services.bundle_install_dal import raw_sql_write
from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2
from tests.conftest import make_user_token, make_user_token_with_roles

REPO = Path(__file__).resolve().parents[2]
SVC_APP = "waddles.core.service.reputation"
APP = "waddles.socials.demo.default"
BASE = f"/api/v1/apps/community/%d/bundles/{APP}"
SVC_BASE = f"/api/v1/apps/community/%d/bundles/{SVC_APP}"

_SCORE = {
    "type": "object",
    "properties": {"user": {"type": "string"}, "score": {"type": "integer"}},
}
_SVC_MANIFEST: dict[str, Any] = {
    "schema_version": 2,
    "app_id": SVC_APP,
    "name": "Rep",
    "version": "1.0.0",
    "feature": "waddles.core.service",
    "module": "core",
    "provider": "builtin",
    "language": "rust",
    "artifact": "prebuilt",
    "kind": "service",
    "stages": {"action": {"entry": "waddle:bundle/tool-provider#call"}},
    "exported_tools": [
        {
            "name": "leaderboard",
            "access": "read",
            "risk": "normal",
            "input_schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}, "cursor": {"type": "string"}},
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {"entries": {"type": "array", "items": _SCORE}},
                "required": ["entries"],
            },
        },
        {
            "name": "override",
            "access": "write",
            "risk": "dangerous",
            "required_scope": "community:admin",
            "input_schema": {
                "type": "object",
                "properties": {"user": {"type": "string"}, "absolute_score": {"type": "integer"}},
                "required": ["user", "absolute_score"],
                "additionalProperties": False,
            },
            "output_schema": _SCORE,
        },
        {
            "name": "nolimit",
            "access": "read",
            "risk": "normal",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "output_schema": {"type": "object"},
        },
    ],
    "admin_tools": [
        {"name": "board", "tool": "leaderboard", "kind": "read", "scope": "community:moderate"},
        {"name": "override", "tool": "override", "kind": "action", "scope": "community:admin"},
        {"name": "flat", "tool": "nolimit", "kind": "read", "scope": "community:moderate"},
        {"name": "wrongkind", "tool": "override", "kind": "read", "scope": "community:admin"},
    ],
}
_APP_MANIFEST: dict[str, Any] = {
    "schema_version": 2,
    "app_id": APP,
    "name": "Demo",
    "version": "1.0.0",
    "feature": "waddles.socials.demo",
    "module": "socials",
    "provider": "builtin",
    "language": "rust",
    "artifact": "prebuilt",
    "stages": {"action": {"entry": "x:y"}},
    "requested_tools": [
        {"target_app_id": SVC_APP, "tool": "leaderboard", "access": "read"},
        {"target_app_id": SVC_APP, "tool": "override", "access": "write"},
    ],
    "community_config_schema": {
        "type": "object",
        "properties": {
            "greeting": {"type": "string", "maxLength": 20},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["greeting"],
        "additionalProperties": False,
    },
    "admin_tools": [
        {
            "name": "board",
            "tool": "leaderboard",
            "kind": "read",
            "scope": "community:moderate",
            "target_app_id": SVC_APP,
        },
        {
            "name": "override",
            "tool": "override",
            "kind": "action",
            "scope": "community:admin",
            "target_app_id": SVC_APP,
        },
    ],
}


class FakeTransport:
    """Records calls; answers with `reply` or raises `exc`."""

    def __init__(self, reply: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        """Hold the canned reply / error."""
        self.reply = reply if reply is not None else {"entries": []}
        self.exc = exc
        self.calls: list[tt.ToolCall] = []

    async def invoke(self, call: tt.ToolCall) -> dict[str, Any]:
        self.calls.append(call)
        if self.exc is not None:
            raise self.exc
        return self.reply


class FakeLimiter:
    """check_rate_limit allows `allow` calls then refuses."""

    def __init__(self, allow: int = 10_000) -> None:
        """Set the budget."""
        self.allow = allow
        self.keys: list[str] = []

    async def check_rate_limit(self, identifier: str, limit: int, window: int) -> bool:
        self.keys.append(identifier)
        self.allow -= 1
        return self.allow >= 0


async def _publish(install_dal: Any, manifest: dict[str, Any], community_id: int) -> None:
    now = datetime.now(UTC)
    vid = await install_dal.app_versions.async_insert(
        app_id=manifest["app_id"],
        version=manifest["version"],
        artifact_digest="sha256:" + manifest["app_id"].encode().hex()[:64].ljust(64, "0"),
        language="rust",
        artifact_kind="prebuilt",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=manifest["app_id"],
        version=manifest["version"],
        tenant_id=1,
        artifact_kind="prebuilt",
        language="rust",
        status="PUBLISHED",
        manifest_json=manifest,
        app_version_id=vid,
        created_at=now,
        updated_at=now,
    )
    # app_active_versions has no single-column PK (composite) -- raw insert, like production.
    await raw_sql_write(
        install_dal,
        "INSERT INTO app_active_versions"
        " (app_id, tenant_id, community_id, version_id, activated_at)"
        " VALUES (:a, 1, :c, :v, :t)",
        {"a": manifest["app_id"], "c": community_id, "v": vid, "t": now},
    )


@pytest.fixture
async def env(bundle_install_db: Any, install_dal: Any) -> dict[str, Any]:
    dal = bundle_install_db.dal
    dal.define_table("community_roles", Field("name"), Field("base_claims", "json"), migrate=True)
    dal.define_table(
        "community_members",
        Field("community_id", "integer"),
        Field("user_id"),
        Field("is_active", "boolean", default=True),
        Field("claims_cache", "json"),
        Field("role"),
        Field("community_role_id", "integer"),
        migrate=True,
    )
    dal.define_table(
        "tenant_admins", Field("user_id", "integer"), Field("tenant_id", "integer"), migrate=True
    )
    dal.define_table("communities", Field("tenant_id", "integer"), Field("name"), migrate=False)
    dal(dal.community_members.id == 0).count()
    dal.commit()

    community_id = int(await install_dal.communities.async_insert(tenant_id=1, name="acme"))
    await _publish(install_dal, _SVC_MANIFEST, community_id)
    await _publish(install_dal, _APP_MANIFEST, community_id)
    for user, scopes in {
        10: ["community:manage_channels"],  # moderator
        11: ["community:manage_channels", "community:manage_members"],  # admin
        12: ["community:read"],  # plain member
    }.items():
        dal.community_members.insert(
            community_id=community_id,
            user_id=str(user),
            is_active=True,
            claims_cache={"scopes": scopes},
            role="x",
        )
    dal.commit()

    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    transport = FakeTransport()
    limiter = FakeLimiter()
    quart_app.config.update(
        async_dal=bundle_install_db,
        dal=dal,
        install_dal=install_dal,
        bundle_tool_transport=transport,
        rate_limiter=limiter,
    )
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return {
        "app": quart_app,
        "cid": community_id,
        "transport": transport,
        "limiter": limiter,
        "dal": install_dal,
    }


def _h(user: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=user)}"}


# ----------------------------------------------------------------------------- manifest


def _app(**over: Any) -> dict[str, Any]:
    return {**_APP_MANIFEST, **over}


def _parse(raw: dict[str, Any]) -> Any:
    return parse_bundle_manifest_v2(
        raw,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
        trusted_source=True,
    )


def test_manifest_parses_gateway_blocks() -> None:
    m = _parse(_app())
    assert m.community_config_schema["type"] == "object"
    assert [t.name for t in m.admin_tools] == ["board", "override"]
    assert m.admin_tools[0].target_app_id == SVC_APP


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"community_config_schema": []}, "invalid_config_schema"),
        ({"community_config_schema": {"type": "array"}}, "invalid_config_schema"),
        ({"community_config_schema": {"type": "object", "properties": 3}}, "invalid_config_schema"),
        (
            {"community_config_schema": {"type": "object", "x": "y" * 70_000}},
            "invalid_config_schema",
        ),
        ({"admin_tools": "no"}, "invalid_admin_tools"),
        ({"admin_tools": ["no"]}, "invalid_admin_tools"),
        (
            {
                "admin_tools": [
                    {"name": "Bad", "tool": "t", "kind": "read", "scope": "community:admin"}
                ]
            },
            "invalid_admin_tools",
        ),
        (
            {
                "admin_tools": [
                    {"name": "a", "tool": "Bad", "kind": "read", "scope": "community:admin"}
                ]
            },
            "invalid_admin_tools",
        ),
        (
            {
                "admin_tools": [
                    {"name": "a", "tool": "t", "kind": "zap", "scope": "community:admin"}
                ]
            },
            "invalid_admin_tools",
        ),
        (
            {
                "admin_tools": [
                    {"name": "a", "tool": "t", "kind": "read", "scope": "community:owner"}
                ]
            },
            "invalid_admin_tools",
        ),
        (
            {
                "admin_tools": [
                    {
                        "name": "a",
                        "tool": "t",
                        "kind": "read",
                        "scope": "community:admin",
                        "target_app_id": "nope",
                    }
                ]
            },
            "invalid_admin_tools",
        ),
        (
            {
                "admin_tools": [
                    {
                        "name": "a",
                        "tool": "leaderboard",
                        "kind": "read",
                        "scope": "community:admin",
                        "target_app_id": SVC_APP,
                    },
                    {
                        "name": "a",
                        "tool": "leaderboard",
                        "kind": "read",
                        "scope": "community:admin",
                        "target_app_id": SVC_APP,
                    },
                ]
            },
            "invalid_admin_tools",
        ),
        # un-consented cross-bundle tool, and own-tool not exported
        (
            {
                "admin_tools": [
                    {
                        "name": "a",
                        "tool": "adjust",
                        "kind": "action",
                        "scope": "community:admin",
                        "target_app_id": SVC_APP,
                    }
                ]
            },
            "admin_tool_undeclared",
        ),
        (
            {
                "admin_tools": [
                    {"name": "a", "tool": "ghost", "kind": "action", "scope": "community:admin"}
                ]
            },
            "admin_tool_undeclared",
        ),
    ],
)
def test_manifest_rejects_bad_gateway_blocks(over: dict[str, Any], code: str) -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(_app(**over))
    assert exc.value.reason == code


def test_real_reputation_manifest_declares_admin_tools() -> None:
    raw = yaml.safe_load((REPO / "bundles/rust/reputation/bundle.yaml").read_text())
    hub = yaml.safe_load((REPO / "bundles/rust/reputation/hub-manifest.yaml").read_text())
    m = _parse(hub)
    assert {(t.name, t.kind, t.scope) for t in m.admin_tools} == {
        ("leaderboard", "read", "community:moderate"),
        ("override", "action", "community:admin"),
    }
    assert hub["admin_tools"] == raw["admin_tools"]


# ----------------------------------------------------------------------------- transport


class StoreValkey:
    """lpush/blpop store; `reply` simulates the host answering."""

    def __init__(self, reply: Any = None, *, fail: bool = False) -> None:
        """Hold the canned reply / outage flag."""
        self.reply, self.fail, self.sent, self.closed = reply, fail, [], False

    async def lpush(self, key: str, value: str) -> None:
        if self.fail:
            raise ConnectionError("down")
        assert key == tt.REQUEST_QUEUE
        self.sent.append(json.loads(value))

    async def blpop(self, key: str, timeout: int) -> Any:  # noqa: ASYNC109
        assert key == f"{tt.RESULT_PREFIX}{self.sent[-1]['request_id']}"
        return None if self.reply is None else (key, self.reply)

    async def aclose(self) -> None:
        self.closed = True


_RING = tt.parse_key_ring("k1:" + "ab" * 16, "k1")
_CALL = tt.ToolCall(3, 7, 11, "community:admin", APP, SVC_APP, "override", '{"a":1}')


async def _roundtrip(reply: Any, *, fail: bool = False) -> tuple[Any, StoreValkey]:
    store = StoreValkey(reply, fail=fail)
    transport = tt.ValkeyToolTransport(lambda: store, keys=_RING)
    try:
        return await transport.invoke(_CALL), store
    finally:
        assert store.closed


async def test_transport_signs_and_returns_result() -> None:
    result, store = await _roundtrip(json.dumps({"ok": True, "result": {"score": 5}}))
    assert result == {"score": 5}
    req = store.sent[0]
    expect = tt.sign_call(
        _RING[0]["k1"], "k1", _CALL, request_id=req["request_id"], exp_ms=req["exp_ms"]
    )
    assert req["sig"] == expect and req["actor_scope"] == "community:admin"


def test_signature_binds_every_field() -> None:
    base = tt.sign_call(b"k" * 16, "k", _CALL, request_id="r", exp_ms=1)
    import dataclasses

    for field_name, value in [
        ("tool", "x"),
        ("actor_scope", "community:moderate"),
        ("actor_user_id", 99),
        ("community_id", 8),
        ("tenant_id", 4),
        ("args_json", "{}"),
        ("target_app_id", "waddles.a.b.c"),
        ("caller_app_id", "waddles.a.b.c"),
    ]:
        other = dataclasses.replace(_CALL, **{field_name: value})
        assert tt.sign_call(b"k" * 16, "k", other, request_id="r", exp_ms=1) != base, field_name
    assert tt.sign_call(b"k" * 16, "k", _CALL, request_id="r2", exp_ms=1) != base
    assert tt.sign_call(b"k" * 16, "k", _CALL, request_id="r", exp_ms=2) != base


@pytest.mark.parametrize(
    ("reply", "status", "code"),
    [
        (None, 504, "bundle_tool_timeout"),
        ("not json", 502, "bundle_tool_bad_reply"),
        (json.dumps({"ok": True, "result": [1]}), 502, "bundle_tool_bad_reply"),
        (json.dumps({"ok": False, "code": "denied"}), 502, "bundle_tool_denied"),
        (json.dumps({"ok": False, "code": "bad code!"}), 502, "bundle_tool_failed"),
        (json.dumps({"ok": False, "code": "nope", "tool_error": True}), 422, "bundle_tool_nope"),
        (json.dumps({"ok": False}), 502, "bundle_tool_failed"),
        (b'{"ok": true, "result": {}}', 200, ""),
        ("x" * (tt.MAX_RESULT_BYTES + 1), 502, "bundle_tool_result_too_large"),
    ],
)
async def test_transport_reply_mapping(reply: Any, status: int, code: str) -> None:
    if status == 200:
        result, _ = await _roundtrip(reply)
        assert result == {}
        return
    with pytest.raises(tt.ToolTransportError) as exc:
        await _roundtrip(reply)
    assert (exc.value.status, exc.value.code) == (status, code)


async def test_transport_valkey_down_is_503() -> None:
    with pytest.raises(tt.ToolTransportError) as exc:
        await _roundtrip(None, fail=True)
    assert exc.value.status == 503 and exc.value.code == "bundle_tool_transport_unavailable"


def test_keys_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUNDLE_ADMIN_REQUEST_KEYS", raising=False)
    with pytest.raises(tt.ToolTransportError) as exc:
        tt.keys_from_env()
    assert exc.value.code == "bundle_tool_transport_unconfigured"
    monkeypatch.setenv("BUNDLE_ADMIN_REQUEST_KEYS", "bad")
    with pytest.raises(tt.ToolTransportError):
        tt.keys_from_env()
    monkeypatch.setenv("BUNDLE_ADMIN_REQUEST_KEYS", "k1:" + "cd" * 16)
    monkeypatch.setenv("BUNDLE_ADMIN_REQUEST_ACTIVE_KID", "k1")
    assert tt.keys_from_env()[1] == "k1"


def test_canonical_args_is_deterministic() -> None:
    assert tt.canonical_args({"b": 1, "a": "é"}) == '{"a":"é","b":1}'


# ----------------------------------------------------------------------------- routes: config


async def test_descriptor_lists_tools_and_permission(env: dict[str, Any]) -> None:
    c = env["app"].test_client()
    r = await c.get(BASE % env["cid"] + "/admin", headers=_h(10))
    assert r.status_code == 200
    body = await r.get_json()
    tools = {t["name"]: t for t in body["tools"]}
    assert tools["board"]["permitted"] is True and tools["override"]["permitted"] is False
    assert body["configSchema"]["type"] == "object"


async def test_member_and_stranger_are_denied(env: dict[str, Any]) -> None:
    c = env["app"].test_client()
    assert (await c.get(BASE % env["cid"] + "/admin", headers=_h(12))).status_code == 403
    assert (await c.get(BASE % env["cid"] + "/admin", headers=_h(999))).status_code == 403
    r = await c.get(BASE % env["cid"] + "/config", headers=_h(12))
    assert r.status_code == 403


async def test_config_default_save_conflict_history(env: dict[str, Any]) -> None:
    c, url = env["app"].test_client(), BASE % env["cid"] + "/config"
    first = await (await c.get(url, headers=_h(10))).get_json()
    assert first["version"] == 0 and first["config"] == {}
    r = await c.put(url, headers=_h(11), json={"config": {"greeting": "hi"}, "expectedVersion": 0})
    assert r.status_code == 200
    saved = await r.get_json()
    assert (
        saved["version"] == 1 and saved["config"] == {"greeting": "hi"} and saved["updatedBy"] == 11
    )
    stale = await c.put(
        url, headers=_h(11), json={"config": {"greeting": "yo"}, "expectedVersion": 0}
    )
    assert stale.status_code == 409
    assert (await stale.get_json())["error"]["details"] == {"currentVersion": 1}
    ok2 = await c.put(
        url, headers=_h(11), json={"config": {"greeting": "yo"}, "expectedVersion": 1}
    )
    assert (await ok2.get_json())["version"] == 2
    hist = await (await c.get(url + "/history?limit=1", headers=_h(10))).get_json()
    assert [v["version"] for v in hist["versions"]] == [2]
    assert (await c.get(url + "/history?limit=zz", headers=_h(10))).status_code == 400


async def test_config_validation_is_value_free_and_moderator_cannot_write(
    env: dict[str, Any],
) -> None:
    c, url = env["app"].test_client(), BASE % env["cid"] + "/config"
    r = await c.put(
        url,
        headers=_h(11),
        json={"config": {"greeting": 5, "secret-ish": "hunter2"}, "expectedVersion": 0},
    )
    assert r.status_code == 422
    raw = await r.get_data(as_text=True)
    assert "hunter2" not in raw
    errs = json.loads(raw)["error"]["details"]["errors"]
    assert {"path": "$.greeting", "rule": "type"} in errs
    mod = await c.put(url, headers=_h(10), json={"config": {"greeting": "a"}, "expectedVersion": 0})
    assert mod.status_code == 403


async def test_config_size_cap_and_save_race(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, url = env["app"].test_client(), BASE % env["cid"] + "/config"
    big = await c.put(
        url,
        headers=_h(11),
        json={"config": {"greeting": "a", "x": "y" * 70_000}, "expectedVersion": 0},
    )
    assert big.status_code == 413
    # Two racing saves of the same next version: the UNIQUE constraint turns the loser into 409.
    from services import bundle_admin_gateway_service as svc

    real = svc.get_config
    calls = {"n": 0}

    async def stale_read(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        snap = await real(*a, **k)
        return snap if calls["n"] > 1 else type(snap)(0, {}, None, None)

    ok = await c.put(url, headers=_h(11), json={"config": {"greeting": "a"}, "expectedVersion": 0})
    assert ok.status_code == 200
    monkeypatch.setattr(svc, "get_config", stale_read)
    lost = await c.put(
        url, headers=_h(11), json={"config": {"greeting": "b"}, "expectedVersion": 0}
    )
    assert lost.status_code == 409


async def test_service_bundle_without_config_schema_is_404(env: dict[str, Any]) -> None:
    c = env["app"].test_client()
    r = await c.get(SVC_BASE % env["cid"] + "/config", headers=_h(10))
    assert r.status_code == 404
    assert (await r.get_json())["error"]["code"] == "bundle_has_no_config"
    r = await c.put(
        SVC_BASE % env["cid"] + "/config", headers=_h(11), json={"config": {}, "expectedVersion": 0}
    )
    assert r.status_code == 404


async def test_inactive_bundle_and_bad_ids(env: dict[str, Any]) -> None:
    c = env["app"].test_client()
    r = await c.get(
        f"/api/v1/apps/community/{env['cid']}/bundles/waddles.no.such.bundle/admin",
        headers=_h(10),
    )
    assert r.status_code == 404
    assert (await r.get_json())["error"]["code"] == "bundle_not_active"
    assert (
        await c.get(f"/api/v1/apps/community/{env['cid']}/bundles/Bad_ID/admin", headers=_h(10))
    ).status_code == 400
    assert (await c.get(BASE % 999_999 + "/admin", headers=_h(10))).status_code == 403
    assert (await c.get(BASE % env["cid"] + "/admin")).status_code in (401, 403)


# ----------------------------------------------------------------------------- routes: tools


async def test_admin_action_happy_path_audited_and_scoped(env: dict[str, Any]) -> None:
    env["transport"].reply = {"user": "u", "score": 5}
    c, url = env["app"].test_client(), BASE % env["cid"] + "/admin-actions/override"
    r = await c.post(url, headers=_h(11), json={"args": {"user": "u", "absolute_score": 5}})
    assert r.status_code == 200
    assert (await r.get_json())["result"] == {"user": "u", "score": 5}
    call = env["transport"].calls[0]
    assert (call.tenant_id, call.community_id, call.actor_user_id) == (1, env["cid"], 11)
    assert (call.actor_scope, call.caller_app_id, call.target_app_id) == (
        "community:admin",
        APP,
        SVC_APP,
    )
    rows = await env["dal"](env["dal"].audit_log.action == "bundle_admin.tool_invoked").select()
    phases = sorted(row.details["phase"] for row in rows)
    assert phases == ["attempt", "result"]
    assert all(
        "hunter" not in json.dumps(row.details) and "absolute_score" not in json.dumps(row.details)
        for row in rows
    )


async def test_admin_action_authz_and_validation(env: dict[str, Any]) -> None:
    c, url = env["app"].test_client(), BASE % env["cid"] + "/admin-actions/override"
    good = {"args": {"user": "u", "absolute_score": 5}}
    assert (await c.post(url, headers=_h(10), json=good)).status_code == 403  # moderator < admin
    assert (await c.post(url, headers=_h(12), json=good)).status_code == 403
    assert env["transport"].calls == []
    bad = await c.post(url, headers=_h(11), json={"args": {"user": "u", "extra": "secretvalue"}})
    assert bad.status_code == 422
    assert "secretvalue" not in await bad.get_data(as_text=True)
    assert (
        await c.post(BASE % env["cid"] + "/admin-actions/ghost", headers=_h(11), json={"args": {}})
    ).status_code == 404
    # a `read`-kind tool is not invocable as an action, and vice versa
    assert (
        await c.post(BASE % env["cid"] + "/admin-actions/board", headers=_h(11), json={"args": {}})
    ).status_code == 404
    assert (await c.get(BASE % env["cid"] + "/data/override", headers=_h(11))).status_code == 404
    assert (
        await c.post(
            BASE % env["cid"] + "/admin-actions/Bad-Name", headers=_h(11), json={"args": {}}
        )
    ).status_code == 400
    too_big = await c.post(
        url, headers=_h(11), json={"args": {"user": "x" * 70_000, "absolute_score": 1}}
    )
    assert too_big.status_code == 413


async def test_admin_action_failure_mapping_and_bad_output(env: dict[str, Any]) -> None:
    c, url = env["app"].test_client(), BASE % env["cid"] + "/admin-actions/override"
    body = {"args": {"user": "u", "absolute_score": 5}}
    env["transport"].exc = tt.ToolTransportError(504, "bundle_tool_timeout", "slow")
    r = await c.post(url, headers=_h(11), json=body)
    assert r.status_code == 504
    assert (await r.get_json())["error"]["code"] == "bundle_tool_timeout"
    env["transport"].exc = None
    env["transport"].reply = {"score": "not-an-int"}
    r = await c.post(url, headers=_h(11), json=body)
    assert r.status_code == 502
    assert (await r.get_json())["error"]["code"] == "bundle_tool_bad_output"
    rows = await env["dal"](env["dal"].audit_log.action == "bundle_admin.tool_invoked").select()
    assert {row.details.get("outcome") for row in rows} >= {"bundle_tool_timeout", "bad_output"}


async def test_data_view_pagination_pass_through(env: dict[str, Any]) -> None:
    env["transport"].reply = {"entries": [{"user": "u", "score": 1}], "next_cursor": "c2"}
    c, url = env["app"].test_client(), BASE % env["cid"] + "/data/board"
    r = await c.get(url + "?limit=500&cursor=c1&args=%7B%7D", headers=_h(10))
    assert r.status_code == 200
    body = await r.get_json()
    assert (body["limit"], body["nextCursor"]) == (100, "c2")
    assert json.loads(env["transport"].calls[-1].args_json) == {"cursor": "c1", "limit": 100}
    await c.get(url, headers=_h(10))
    assert json.loads(env["transport"].calls[-1].args_json) == {"limit": 25}
    # read views are not audited (no state change), but are tenant/scope gated
    assert (await c.get(url, headers=_h(12))).status_code == 403


async def test_data_view_own_tool_and_undeclared_paging(env: dict[str, Any]) -> None:
    env["transport"].reply = {"anything": 1}
    c = env["app"].test_client()
    r = await c.get(SVC_BASE % env["cid"] + "/data/flat?limit=5&cursor=x", headers=_h(10))
    assert r.status_code == 200
    body = await r.get_json()
    assert body["limit"] is None and body["nextCursor"] is None
    assert env["transport"].calls[-1].args_json == "{}"
    assert env["transport"].calls[-1].target_app_id == SVC_APP


async def test_data_view_rejects_bad_query_and_write_tool(env: dict[str, Any]) -> None:
    c, url = env["app"].test_client(), BASE % env["cid"] + "/data/board"
    assert (await c.get(url + "?args=not-json", headers=_h(10))).status_code == 400
    assert (await c.get(url + "?args=%5B%5D", headers=_h(10))).status_code == 400
    assert (await c.get(url + "?limit=x", headers=_h(10))).status_code == 400
    assert (await c.get(url + "?args=" + "a" * 5000, headers=_h(10))).status_code == 413
    r = await c.get(SVC_BASE % env["cid"] + "/data/wrongkind", headers=_h(11))
    assert r.status_code == 502
    assert (await r.get_json())["error"]["code"] == "admin_tool_not_read"


async def test_cross_bundle_target_must_be_active(env: dict[str, Any]) -> None:
    await env["dal"](env["dal"].app_active_versions.app_id == SVC_APP).delete()
    r = await env["app"].test_client().get(BASE % env["cid"] + "/data/board", headers=_h(10))
    assert r.status_code == 404
    assert (await r.get_json())["error"]["code"] == "bundle_not_active"


async def test_tenant_wide_activation_is_used(env: dict[str, Any]) -> None:
    await env["dal"](env["dal"].app_active_versions.app_id == APP).update(community_id=0)
    r = await env["app"].test_client().get(BASE % env["cid"] + "/admin", headers=_h(10))
    assert r.status_code == 200


async def test_platform_admin_bypass_holds_admin_scope(env: dict[str, Any]) -> None:
    env["transport"].reply = {"score": 1}
    token = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    r = (
        await env["app"]
        .test_client()
        .post(
            BASE % env["cid"] + "/admin-actions/override",
            headers={"Authorization": f"Bearer {token}"},
            json={"args": {"user": "u", "absolute_score": 1}},
        )
    )
    assert r.status_code == 200
    assert env["transport"].calls[0].actor_scope == "community:admin"


async def test_rate_limit_and_missing_limiter(env: dict[str, Any]) -> None:
    c, url = env["app"].test_client(), BASE % env["cid"] + "/data/board"
    env["limiter"].allow = 1
    assert (await c.get(url, headers=_h(10))).status_code == 200
    r = await c.get(url, headers=_h(10))
    assert r.status_code == 429
    assert env["limiter"].keys[-1] == f"bundle_admin:10:{env['cid']}:{APP}:board"
    env["app"].config["rate_limiter"] = None
    r = await c.get(url, headers=_h(10))
    assert r.status_code == 503
    assert (await r.get_json())["error"]["code"] == "rate_limiter_unavailable"


async def test_default_transport_fails_loud_when_unconfigured(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BUNDLE_ADMIN_REQUEST_KEYS", raising=False)
    env["app"].config["bundle_tool_transport"] = None
    r = await env["app"].test_client().get(BASE % env["cid"] + "/data/board", headers=_h(10))
    assert r.status_code == 503
    assert (await r.get_json())["error"]["code"] == "bundle_tool_transport_unconfigured"


def test_scope_helpers() -> None:
    from services.bundle_admin_gateway_service import caller_holds, caller_scope
    from services.community_authz import CommunityMembership

    mem = CommunityMembership(
        role="r", scopes=frozenset({"community:manage_channels"}), is_admin=False, bypass=False
    )
    assert caller_holds(mem, "community:moderate") and not caller_holds(mem, "community:admin")
    assert caller_scope(mem) == "community:moderate"
    assert caller_scope(CommunityMembership("r", frozenset(), False, False)) is None
