"""Contract: every `calls.py` route maps to a `RoomsService` RPC that exists, and vice versa.

The retired REST proxy forwarded 13 paths that svc-streaming never served, so every call
404'd and nothing noticed. This test fails when (a) a route is added to the calls
blueprints without a `ROUTE_RPCS` entry, (b) a mapped RPC is not in the proto descriptor,
(c) a proto RPC is neither routed nor listed with a reason in `UNROUTED_RPCS` -- so a future
RPC cannot be left unmapped silently -- or (d) a `RoomsClient` method calls an RPC name absent
from the proto. The Rust side (server implements every proto RPC) is pinned by
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
from waddles.streaming.internal.v1 import rooms_pb2, rooms_pb2_grpc

import blueprints.v1.calls as calls_module
from services import calls_proxy
from services.calls_proxy import ROUTE_RPCS, UNROUTED_RPCS, RoomsClient

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
    assert len(routes) == 23, routes  # denominator: 14 admin + 6 member + 3 community routes
    assert routes == set(ROUTE_RPCS), routes ^ set(ROUTE_RPCS)


def test_every_mapped_rpc_exists_in_the_proto() -> None:
    rpcs = _descriptor_rpcs()
    assert rpcs, "no RPCs found in the descriptor -- the scan itself is broken"
    mapped = set(ROUTE_RPCS.values())
    assert mapped <= rpcs, mapped - rpcs


def test_every_proto_rpc_is_routed_or_explicitly_unrouted() -> None:
    """A new RPC in the proto fails this until it gets a route or a documented reason."""
    rpcs = _descriptor_rpcs()
    routed = set(ROUTE_RPCS.values())
    assert rpcs - routed == set(UNROUTED_RPCS), (
        "proto RPCs without a hub-api route: "
        f"{sorted(rpcs - routed - set(UNROUTED_RPCS))}; stale UNROUTED_RPCS entries: "
        f"{sorted(set(UNROUTED_RPCS) - (rpcs - routed))}"
    )
    assert all(reason.strip() for reason in UNROUTED_RPCS.values())
    assert not set(UNROUTED_RPCS) & routed


def test_client_has_a_method_for_every_routed_rpc() -> None:
    called = set(
        re.findall(
            r'"([A-Z][A-Za-z]+)",\s*\n?\s*(?:SCOPE_|scope\b)', inspect.getsource(RoomsClient)
        )
    )
    assert set(ROUTE_RPCS.values()) <= called, set(ROUTE_RPCS.values()) - called


def test_client_only_calls_rpcs_that_exist() -> None:
    called = set(
        re.findall(
            r'"([A-Z][A-Za-z]+)",\s*\n?\s*(?:SCOPE_|scope\b)', inspect.getsource(RoomsClient)
        )
    )
    assert called, "no RPC calls found -- the scan itself is broken"
    assert called <= _descriptor_rpcs(), called - _descriptor_rpcs()


def test_descriptor_matches_the_checked_in_proto_text() -> None:
    declared = set(re.findall(r"^\s*rpc\s+(\w+)\(", PROTO.read_text(), re.MULTILINE))
    assert declared == _descriptor_rpcs(), "regenerate stubs: scripts/compile_protos_v1.sh"


def test_proxy_module_has_no_rest_remnants() -> None:
    source = inspect.getsource(calls_proxy)
    assert "httpx" not in source and "http://" not in source.replace("https://", "")


def test_in_process_fake_implements_every_proto_rpc() -> None:
    """The fake server the blueprint tests use must grow with the proto, never lag it."""
    from tests.test_calls_support import FakeRooms

    missing = {r for r in _descriptor_rpcs() if getattr(FakeRooms, r, None) is None}
    assert not missing, f"FakeRooms lacks: {sorted(missing)}"
    overridden = {
        r
        for r in _descriptor_rpcs()
        if getattr(FakeRooms, r) is not getattr(rooms_pb2_grpc.RoomsServiceServicer, r)
    }
    assert overridden == _descriptor_rpcs()
