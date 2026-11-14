"""Contract: every `calls.py` route maps to a `RoomsService` RPC that exists (or an explicit 501).

The retired REST proxy forwarded 13 paths that svc-streaming never served, so every call
404'd and nothing noticed. This test fails when (a) a route is added to the calls
blueprints without a `ROUTE_RPCS` entry, (b) a mapped RPC is not in the proto descriptor,
(c) a `NOT_IMPLEMENTED` route gains an RPC, or (d) a `RoomsClient` method calls an RPC name
absent from the proto. The Rust side (server implements every proto RPC) is pinned by
`core/svc_streaming/tests/rooms_grpc.rs`.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from quart import Quart

# isort: split
import grpc_internal  # noqa: F401  (puts the generated pb/ tree on sys.path)

# isort: split
from waddles.streaming.internal.v1 import rooms_pb2

import blueprints.v1.calls as calls_module
from services import calls_proxy
from services.calls_proxy import NOT_IMPLEMENTED, ROUTE_RPCS, RoomsClient

PROTO = Path(__file__).resolve().parents[2] / "proto/waddles/streaming/internal/v1/rooms.proto"


def _descriptor_rpcs() -> set[str]:
    return set(rooms_pb2.DESCRIPTOR.services_by_name["RoomsService"].methods_by_name)


def _routes() -> set[tuple[str, str]]:
    app = Quart(__name__)
    for bp in calls_module.BLUEPRINTS:
        app.register_blueprint(bp)
    return {
        (method, rule.rule)
        for rule in app.url_map.iter_rules()
        if rule.endpoint != "static"
        for method in (rule.methods or set()) - {"HEAD", "OPTIONS"}
    }


def test_every_calls_route_is_mapped_and_nothing_stale() -> None:
    routes = _routes()
    assert len(routes) == 15, routes  # denominator: 11 admin + 4 member routes
    assert routes == set(ROUTE_RPCS), routes ^ set(ROUTE_RPCS)


def test_every_mapped_rpc_exists_in_the_proto() -> None:
    rpcs = _descriptor_rpcs()
    assert len(rpcs) == 13
    mapped = {r for r in ROUTE_RPCS.values() if r != NOT_IMPLEMENTED}
    assert mapped <= rpcs, mapped - rpcs


def test_not_implemented_routes_are_exactly_the_hand_raise_pair() -> None:
    assert {k for k, v in ROUTE_RPCS.items() if v == NOT_IMPLEMENTED} == {
        ("GET", "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>/raised-hands"),
        ("POST", "/api/v1/admin/<int:community_id>/calls/rooms/<room_name>/acknowledge-hand"),
    }


def test_client_only_calls_rpcs_that_exist() -> None:
    called = set(re.findall(r'"([A-Z][A-Za-z]+)",\s*\n?\s*SCOPE_', inspect.getsource(RoomsClient)))
    assert called, "no RPC calls found -- the scan itself is broken"
    assert called <= _descriptor_rpcs(), called - _descriptor_rpcs()


def test_descriptor_matches_the_checked_in_proto_text() -> None:
    declared = set(re.findall(r"^\s*rpc\s+(\w+)\(", PROTO.read_text(), re.MULTILINE))
    assert declared == _descriptor_rpcs(), "regenerate stubs: scripts/compile_protos_v1.sh"


def test_proxy_module_has_no_rest_remnants() -> None:
    source = inspect.getsource(calls_proxy)
    assert "httpx" not in source and "http://" not in source.replace("https://", "")
