"""Gate: the rooms app and the webrtc service agree on every consumed tool.

`rooms` (app) may only call tools the `webrtc` (service) bundle exports, with
arguments its input schema accepts. Two files pin the contract:

* `bundles/rust/rooms/tests/contract_calls.json` -- the exact (tool, args) shapes
  rooms sends (the rooms unit test `consumed_tool_calls_match_the_contract_fixture`
  proves the code still sends exactly these);
* `bundles/rust/webrtc/hub-manifest.yaml` -- `exported_tools` input schemas.

Also proves rooms' `requested_tools` / `tool.call:` permissions name only exported
tools, and that webrtc's two manifests agree. Denominators asserted: zero tools or
zero calls examined FAILS.

Run: python3 -m pytest scripts/ci/tests/test_rooms_webrtc_contract.py
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WEBRTC = ROOT / "bundles" / "rust" / "webrtc"
ROOMS = ROOT / "bundles" / "rust" / "rooms"
GRANTS = WEBRTC / "contract" / "grants.json"
HUB_PROXY = ROOT / "hub_api" / "services" / "calls_proxy.py"
HUB_CALLS = ROOT / "hub_api" / "blueprints" / "v1" / "calls.py"
TOKEN_RS = ROOT / "core" / "svc_streaming" / "src" / "rooms" / "token.rs"
ROOMS_PROTO = ROOT / "proto" / "waddles" / "streaming" / "internal" / "v1" / "rooms.proto"
SERVICE = "waddles.core.service.webrtc"
HAND_TOOLS = (
    "room.hand.raise",
    "room.hand.lower",
    "room.hand.lower_other",
    "room.hand.list",
    "room.hand.list_all",
    "room.hand.ack",
    "room.hand.reset",
)
# Hand tools that touch another participant's hand or the whole queue: moderate scope only.
MODERATOR_HAND_TOOLS = ("room.hand.lower_other", "room.hand.list_all", "room.hand.ack", "room.hand.reset")
# Hand tools that take a participant_id argument (the others act on the verified caller).
HAND_TOOLS_WITH_PARTICIPANT = ("room.hand.lower_other", "room.hand.ack")
MODERATOR_ONLY_TOOLS = ("room.kick", "room.mute", "room.unmute", "room.mute_all", "room.lock", "room.unlock")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _check(schema: dict[str, Any], value: Any, where: str) -> None:
    """Minimal JSON-schema subset check (type/enum/format/min/max/required/additionalProperties)."""
    if "enum" in schema:
        assert value in schema["enum"], f"{where}: {value!r} not in enum"
        return
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else (kinds or [])
    py = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list, "null": type(None)}
    if kinds:
        assert any(isinstance(value, py[k]) and not (k == "integer" and isinstance(value, bool)) for k in kinds), (
            f"{where}: wrong type {type(value).__name__} for {kinds}"
        )
    if schema.get("format") == "uuid":
        assert isinstance(value, str) and UUID_RE.match(value), f"{where}: not a lowercase uuid"
    if isinstance(value, str) and "maxLength" in schema:
        assert len(value) <= schema["maxLength"], f"{where}: too long"
    if isinstance(value, int) and not isinstance(value, bool):
        assert value >= schema.get("minimum", value) and value <= schema.get("maximum", value), f"{where}: out of range"
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            assert req in value, f"{where}: missing required {req}"
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            assert not extra, f"{where}: unknown arguments {sorted(extra)}"
        for k, v in value.items():
            if k in props:
                _check(props[k], v, f"{where}.{k}")


def _manifest(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def test_every_consumed_call_matches_a_webrtc_input_schema() -> None:
    tools = {t["name"]: t for t in _manifest(WEBRTC / "hub-manifest.yaml")["exported_tools"]}
    calls = json.loads((ROOMS / "tests" / "contract_calls.json").read_text())
    assert len(tools) >= 12, f"only {len(tools)} webrtc tools examined"
    assert calls, "zero consumed calls examined"
    for name, args in calls.items():
        assert name in tools, f"rooms calls {name}, which webrtc does not export"
        _check(tools[name]["input_schema"], args, name)


def test_rooms_requested_tools_and_permissions_name_only_exported_tools() -> None:
    exported = {t["name"] for t in _manifest(WEBRTC / "hub-manifest.yaml")["exported_tools"]}
    rooms = _manifest(ROOMS / "hub-manifest.yaml")
    requested = [r for r in rooms["requested_tools"] if r["target_app_id"] == SERVICE]
    assert requested, "rooms requests no webrtc tools"
    for r in requested:
        assert r["tool"] in exported, r
    perm_tools = {
        p["id"].split(":", 2)[2]
        for p in rooms["permissions"]
        if p["id"].startswith(f"tool.call:{SERVICE}:")
    }
    assert perm_tools == {r["tool"] for r in requested}, "tool.call permissions drifted from requested_tools"
    calls = json.loads((ROOMS / "tests" / "contract_calls.json").read_text())
    assert set(calls) <= {r["tool"] for r in requested}, "rooms sends a tool it does not request"


def test_privileged_tools_carry_the_admin_scope_rooms_relies_on() -> None:
    tools = {t["name"]: t for t in _manifest(WEBRTC / "hub-manifest.yaml")["exported_tools"]}
    for name in ("room.create", "room.close", "token.revoke_all", "admin.verify"):
        assert tools[name].get("required_scope") == "community:admin", name


def test_webrtc_bundle_and_hub_manifests_agree_on_tools() -> None:
    a = _manifest(WEBRTC / "hub-manifest.yaml")["exported_tools"]
    b = _manifest(WEBRTC / "bundle.yaml")["exported_tools"]
    assert a == b, "exported_tools drifted between hub-manifest.yaml and bundle.yaml"


# ---------------------------------------------------------------------------------------------
# Token / access-policy contract: bundles/rust/webrtc/contract/grants.json is replayed by the
# Rust unit tests (webrtc + rooms, which compile the same grants.rs) and checked here against
# the manifests, the rooms fixture, svc-streaming's token and hub-api's REST join derivation.
# Strict xfails mark KNOWN disagreements owned by another branch: when the owner fixes them the
# strict xfail turns red and the marker must be deleted (a ratchet, not a skip). The hub-api join
# disagreements were fixed by T4 (PR #1326) and their xfails removed.
# ---------------------------------------------------------------------------------------------


def _contract() -> dict[str, Any]:
    return json.loads(GRANTS.read_text())


def _tools() -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in _manifest(WEBRTC / "hub-manifest.yaml")["exported_tools"]}


def _rust_before_tests(path: Path) -> str:
    """Rust source with its `#[cfg(test)]` tail removed (production code only)."""
    return path.read_text().split("#[cfg(test)]", 1)[0]


def _calls(tree: ast.AST, attr: str) -> list[ast.Call]:
    """Every call whose callee ends in `.attr` (or is the bare name `attr`)."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == attr) or (isinstance(f, ast.Name) and f.id == attr):
                out.append(n)
    return out


def _const_kwarg(call: ast.Call, name: str) -> Any:
    """The literal value of keyword `name` (`...` when present but not a literal, None when absent)."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value.value if isinstance(kw.value, ast.Constant) else ...
    return None


def test_contract_file_is_complete_and_closed() -> None:
    c = _contract()
    assert c["version"] == 1
    cases = c["cases"]
    assert len(cases) >= 20, f"only {len(cases)} cases examined"
    ids = [x["id"] for x in cases]
    assert len(ids) == len(set(ids)), "duplicate case id"
    for x in cases:
        assert x["role"] in c["roles"] and x["kind"] in c["kinds"], x["id"]
        assert x["expect"].keys() in ({"denied"}, {"publish", "subscribe"}), x["id"]
    # Every role and every kind is exercised at least once.
    assert {x["role"] for x in cases} == set(c["roles"])
    assert {x["kind"] for x in cases} == set(c["kinds"])
    # A viewer-class participant never publishes anywhere in the contract.
    for x in cases:
        if x["role"] == "guest" and "publish" in x["expect"]:
            assert x["expect"]["publish"] is False, x["id"]
    # Staff are never refused by a lock or a window (only by a closed room).
    for x in cases:
        if x["role"] in ("moderator", "admin") and not x.get("closed"):
            assert "denied" not in x["expect"], x["id"]


def test_hand_tools_declare_the_contract_scopes_and_schema() -> None:
    tools, hands = _tools(), _contract()["hands"]
    assert set(hands) == set(HAND_TOOLS), "contract and test disagree on the hand tool set"
    for name in HAND_TOOLS:
        t = tools[name]
        assert t.get("required_scope") == hands[name]["scope"], name
        schema = t["input_schema"]
        assert schema["additionalProperties"] is False, name
        # The actor is the host-verified context, never an argument (security review fix).
        forbidden = set(_contract()["hands_args_forbidden"])
        assert forbidden == {"actor_id", "actor_role"}
        assert not forbidden & set(schema["properties"]), f"{name} accepts a caller-asserted actor"
        want_props = {"api_version", "room_id"} | ({"participant_id"} if name in HAND_TOOLS_WITH_PARTICIPANT else set())
        assert set(schema["properties"]) == want_props, name
        assert set(schema["required"]) == want_props - {"api_version"}, name
        assert schema["properties"]["api_version"] == {"enum": ["v1"]}, name
        for uuid_field in ("room_id", "participant_id"):
            if uuid_field in schema["properties"]:
                assert schema["properties"][uuid_field]["format"] == "uuid", (name, uuid_field)
    # Scope does the gating: own-hand tools need member, everything else moderate.
    for name in HAND_TOOLS:
        want = "community:moderate" if name in MODERATOR_HAND_TOOLS else "community:member"
        assert tools[name]["required_scope"] == want, name
    assert {tools[n]["access"] for n in ("room.hand.list", "room.hand.list_all")} == {"read"}
    assert {tools[n]["access"] for n in HAND_TOOLS if "list" not in n} == {"write"}
    # Every host op named by the contract is one of the five svc-streaming ops.
    assert {h["host_op"] for h in hands.values()} == {
        "room.hand.raise",
        "room.hand.lower",
        "room.hand.list",
        "room.hand.ack",
        "room.hand.reset",
    }


def test_moderator_only_tools_need_the_moderate_scope_and_leave_stays_member_level() -> None:
    c, tools = _contract(), _tools()
    assert set(c["moderation"]["min_role"]) == set(MODERATOR_ONLY_TOOLS)
    forbidden = set(c["moderation"]["args_forbidden"])
    assert forbidden == {"actor_id", "actor_role"}
    for name in MODERATOR_ONLY_TOOLS:
        assert tools[name].get("required_scope") == c["moderation"]["scope_for_moderator_only"], name
    # Own participation only: `room.leave` is member-scope and has no participant argument.
    assert c["moderation"]["self_service"] == ["room.leave"]
    assert tools["room.leave"].get("required_scope") == c["moderation"]["leave_scope"] == "community:member"
    assert "participant_id" not in tools["room.leave"]["input_schema"]["properties"]
    # No moderation tool accepts a caller-asserted actor or role: the host-verified
    # context is the actor and the declared scope is the gate.
    for name in (*MODERATOR_ONLY_TOOLS, "room.leave"):
        props = set(tools[name]["input_schema"]["properties"])
        assert not forbidden & props, f"{name} accepts a caller-asserted actor {forbidden & props}"
        assert tools[name]["input_schema"]["additionalProperties"] is False, name


def test_mute_all_names_the_caller_so_the_server_can_exclude_them() -> None:
    assert _contract()["moderation"]["mute_all_excludes_actor"] is True
    # The caller is the host-verified actor, forwarded by the tool on the host op; the
    # tool itself takes no actor argument.
    assert "actor_id" not in _tools()["room.mute_all"]["input_schema"]["properties"]
    proto = ROOMS_PROTO.read_text()
    body = re.search(r"message MuteAllRequest \{(.*?)\n\}", proto, re.S)
    assert body and "actor_id" in body.group(1), "MuteAllRequest lost actor_id"
    sent = [c for c in _calls(ast.parse(HUB_CALLS.read_text()), "mute_all") if isinstance(c.func, ast.Attribute)]
    assert sent, "hub-api never calls mute_all"
    for call in sent:
        assert any(k.arg == "actor_id" for k in call.keywords), "hub-api mute-all must pass the caller"
    fixture = json.loads((ROOMS / "tests" / "contract_calls.json").read_text())
    assert not {"actor_id", "actor_role"} & set(fixture["room.mute_all"]), "rooms must not assert the caller"
    for name in (*MODERATOR_ONLY_TOOLS, "room.leave"):
        assert not {"actor_id", "actor_role"} & set(fixture[name]), f"rooms sends an actor to {name}"


def test_token_tools_carry_short_ttl_bounds_and_no_display_name() -> None:
    c, tools = _contract(), _tools()
    tok = c["token"]
    forbidden = set(tok["forbidden_claims"])
    for name in ("token.issue", "whip.endpoint", "whep.endpoint"):
        props = tools[name]["input_schema"]["properties"]
        assert not forbidden & set(props), f"{name} accepts a forbidden field {forbidden & set(props)}"
        ttl = props["ttl_secs"]
        assert (ttl["minimum"], ttl["maximum"]) == (tok["ttl_min_secs"], tok["ttl_max_secs"]), name
    assert tok["join_ttl_secs"] <= tok["join_ttl_max_secs"] <= tok["ttl_max_secs"]
    assert tok["join_ttl_secs"] >= tok["ttl_min_secs"]
    fixture = json.loads((ROOMS / "tests" / "contract_calls.json").read_text())
    for name in ("whip.endpoint", "whep.endpoint"):
        assert fixture[name]["ttl_secs"] == tok["join_ttl_secs"], f"rooms must ask {name} for the short TTL"
        assert not forbidden & set(fixture[name]), name


def test_svc_streaming_room_token_matches_policy_numbers_and_has_no_name_claims() -> None:
    tok = _contract()["token"]
    src = TOKEN_RS.read_text()
    consts = {m.group(1): int(m.group(2)) for m in re.finditer(r"pub const (\w+_TTL_SECS): i64 = (\d+);", src)}
    assert consts["MIN_TTL_SECS"] == tok["ttl_min_secs"]
    assert consts["MAX_TTL_SECS"] == tok["ttl_max_secs"]
    assert consts["DEFAULT_TTL_SECS"] <= tok["join_ttl_max_secs"], "default token life exceeds the join maximum"
    claims = re.search(r"pub struct RoomClaims \{(.*?)\n\}", src, re.S)
    assert claims, "RoomClaims struct not found"
    fields = set(re.findall(r"pub (\w+):", claims.group(1)))
    assert tok["identity_claim"] in fields
    assert not set(tok["forbidden_claims"]) & fields, f"token carries {set(tok['forbidden_claims']) & fields}"
    assert {"exp", "iat", "jti", "room"} <= fields


def test_room_token_is_signed_es256() -> None:
    assert "Algorithm::ES256" in _rust_before_tests(TOKEN_RS)


def test_rooms_app_keeps_no_hand_state_and_no_second_access_policy() -> None:
    sources = sorted((ROOMS / "src").glob("*.rs"))
    assert len(sources) >= 3, f"only {len(sources)} rooms sources examined"
    for path in sources:
        prod = _rust_before_tests(path)
        assert "hands_key" not in prod and '"hands.' not in prod and "hands.{" not in prod, (
            f"{path.name} keeps hand state in kv"
        )
        if path.name != "grants.rs":
            assert "green room is for moderators" not in prod, f"{path.name} re-implements the access policy"
    lib = (ROOMS / "src" / "lib.rs").read_text()
    assert '#[path = "../../webrtc/src/grants.rs"]' in lib, "rooms no longer compiles the shared grants.rs"
    mod = _rust_before_tests(ROOMS / "src" / "moderation.rs")
    for tool in HAND_TOOLS:
        assert f'"{tool}"' in mod, f"rooms hand verbs no longer call {tool}"


def test_rooms_fixture_sends_every_hand_tool_with_v1_and_uuid_participants() -> None:
    fixture = json.loads((ROOMS / "tests" / "contract_calls.json").read_text())
    for name in HAND_TOOLS:
        assert name in fixture, name
        assert fixture[name]["api_version"] == "v1", name
        assert not {"actor_id", "actor_role"} & set(fixture[name]), f"rooms sends an actor to {name}"
        assert set(fixture[name]) == {"api_version", "room_id"} | (
            {"participant_id"} if name in HAND_TOOLS_WITH_PARTICIPANT else set()
        ), name
        for k in ("room_id", "participant_id"):
            if k in fixture[name]:
                assert UUID_RE.match(fixture[name][k]), (name, k)


def test_bundle_versions_agree_across_manifests_and_catalog() -> None:
    catalog = {e["app_id"]: e for e in yaml.safe_load((ROOT / "bundles" / "core-bundles.yaml").read_text())["bundles"]}
    for app_id, d in ((SERVICE, WEBRTC), ("waddles.core.app.rooms", ROOMS)):
        versions = {
            str(_manifest(d / "bundle.yaml")["version"]),
            str(_manifest(d / "hub-manifest.yaml")["version"]),
            str(catalog[app_id]["version"]),
        }
        assert len(versions) == 1, f"{app_id}: versions disagree {versions}"
        assert versions != {"1.1.0"}, f"{app_id}: source changed but the version was not bumped"


# --- hub-api REST join vs the contract (read-only; hub_api is owned by the T4 branch) ----------


def _hub_join_request() -> ast.Call:
    calls = _calls(ast.parse(HUB_PROXY.read_text()), "JoinRoomRequest")
    assert len(calls) == 1, f"expected exactly one JoinRoomRequest in hub-api, found {len(calls)}"
    return calls[0]


def test_hub_api_join_is_uuid_only_and_names_no_display_field() -> None:
    call = _hub_join_request()
    forbidden = set(_contract()["token"]["forbidden_claims"])
    sent = {k.arg for k in call.keywords}
    assert "participant_id" in sent and "api_version" in sent
    assert not forbidden & sent, f"hub-api sends {forbidden & sent} in the join request"
    msg = re.search(r"message JoinRoomRequest \{(.*?)\n\}", ROOMS_PROTO.read_text(), re.S)
    assert msg and not forbidden & set(re.findall(r"\b(\w+) = \d+;", msg.group(1)))


def test_hub_api_moderation_rpcs_use_the_moderate_scope() -> None:
    src = HUB_PROXY.read_text()
    found = dict(
        re.findall(r'"(KickParticipant|MuteParticipant|UnmuteParticipant|MuteAll|LockRoom|UnlockRoom)",\s*(SCOPE_\w+)', src)
    )
    # hub-api has no single-participant mute route today; the four it does call are required,
    # (Un)MuteParticipant are checked too if/when a route is added.
    assert {"KickParticipant", "MuteAll", "LockRoom", "UnlockRoom"} <= set(found), found
    assert set(found.values()) == {"SCOPE_MODERATE"}, found
    # Hand RPCs, where hub-api already calls them, follow the contract's member/moderate split.
    expect = {
        "RaiseHand": "SCOPE_WRITE", "LowerHand": "SCOPE_WRITE", "ListHands": "SCOPE_READ",
        "AcknowledgeHand": "SCOPE_MODERATE", "ResetHands": "SCOPE_MODERATE",
    }
    for rpc, scope in re.findall(r'"(RaiseHand|LowerHand|ListHands|AcknowledgeHand|ResetHands)",\s*(SCOPE_\w+)', src):
        assert expect[rpc] == scope, (rpc, scope)


def _hub_call_access() -> Any:
    """hub-api's `services/call_access.py`, loaded by path (it is pure: no hub-api deps needed)."""
    import importlib.util
    import sys

    path = ROOT / "hub_api" / "services" / "call_access.py"
    spec = importlib.util.spec_from_file_location("hub_api_call_access", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass/enum resolution needs the module registered
    spec.loader.exec_module(mod)
    return mod


def hub_api_grant_mismatches(grants_for: Any = None) -> list[str]:
    """Contract cases (every role) where hub-api's REST-join policy disagrees with the contract."""
    mod = _hub_call_access()
    grants_for = grants_for or mod.grants_for
    bad = []
    for x in _contract()["cases"]:
        ctx = mod.GrantCtx(
            closed=x.get("closed", False),
            locked=x.get("locked", False),
            allow_guests=x.get("allow_guests", False),
            window=mod.Window(x.get("window", "none")),
        )
        want = x["expect"]
        try:
            g = grants_for(mod.GrantKind(x["kind"]), mod.GrantRole[x["role"].upper()], ctx)
            got: dict[str, Any] = {"publish": g.publish, "subscribe": g.subscribe}
        except mod.AccessDeniedError as e:
            got = {"denied": e.reason}
        if got != want:
            bad.append(f"{x['id']}: contract {want}, hub-api policy gives {got}")
    return bad


def test_hub_api_member_join_derives_grants_from_the_policy_not_constants() -> None:
    """The REST join passes policy-derived grants (no literal True/False) and no stale constant."""
    call = _hub_join_request()
    assert _const_kwarg(call, "publish") is None or _const_kwarg(call, "publish") is ...
    assert _const_kwarg(call, "subscribe") is None or _const_kwarg(call, "subscribe") is ...
    sent = {k.arg for k in call.keywords}
    assert {"publish", "subscribe", "ttl_secs"} <= sent
    baseline = next(x for x in _contract()["cases"] if x["id"] == "community-member")["expect"]
    assert baseline == {"publish": True, "subscribe": True}


def test_hub_api_join_grants_follow_the_shared_policy() -> None:
    assert hub_api_grant_mismatches() == []


def test_hub_api_mismatch_detector_actually_examines_cases() -> None:
    """Guards the check above: it must examine every case and catch a deliberately wrong policy."""
    cases = _contract()["cases"]
    assert len(cases) >= 20, "detector examined too few cases"
    mod = _hub_call_access()

    def constant_true(kind: Any, role: Any, ctx: Any) -> Any:
        return mod.Grants(publish=True, subscribe=True)

    found = hub_api_grant_mismatches(constant_true)  # the pre-fix hub-api behaviour
    assert any(m.startswith("stage-member-is-viewer") for m in found), found
    assert any(m.startswith("green-room-member-refused") for m in found), found
    assert any(m.startswith("locked-member-refused") for m in found), found


def test_hub_api_join_requests_the_contract_ttl() -> None:
    mod = _hub_call_access()
    token = _contract()["token"]
    assert mod.JOIN_TTL_SECS == token["join_ttl_secs"]
    assert mod.JOIN_TTL_MAX_SECS == token["join_ttl_max_secs"]
    assert mod.JOIN_TTL_SECS <= mod.JOIN_TTL_MAX_SECS
    # the blueprint passes the contract constant, and the proxy forwards it as ttl_secs
    joins = _calls(ast.parse(HUB_CALLS.read_text()), "join_room")
    assert len(joins) == 1, f"expected exactly one join_room call in calls.py, found {len(joins)}"
    ttl = next(k.value for k in joins[0].keywords if k.arg == "ttl_secs")
    assert isinstance(ttl, ast.Attribute) and ttl.attr == "JOIN_TTL_SECS"
    assert _hub_join_request() and any(k.arg == "ttl_secs" for k in _hub_join_request().keywords)
