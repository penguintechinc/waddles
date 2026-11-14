"""`services/calls_proxy.py` -- config, token minting and status mapping for the rooms gRPC client.

Route-level behaviour (real in-process gRPC server) lives in `test_v1_calls_blueprint.py`;
the route -> RPC contract lives in `test_calls_rpc_contract.py`. This file covers the
fail-loud configuration paths and the client's own error translation.
"""

from __future__ import annotations

import logging

import grpc
import pytest
from flask_core.service_jwt import ServiceJwtError

# isort: split
import grpc_internal  # noqa: F401  (puts the generated pb/ tree on sys.path)

# isort: split
from waddles.streaming.internal.v1 import rooms_pb2, rooms_pb2_grpc

from services import calls_proxy
from services.calls_proxy import (
    RoomsClient,
    RoomsClientConfig,
    RoomsRpcError,
    StreamingTargetNotConfiguredError,
)
from tests.test_calls_support import (  # noqa: F401  (fixtures are consumed by name)
    PARTICIPANT,
    ROOM,
    FakeRooms,
    Issuer,
    fake,
)

_ENV = (
    "STREAMING_RTC_GRPC_TARGET",
    "STREAMING_RTC_GRPC_CA_FILE",
    "STREAMING_RTC_GRPC_CLIENT_CERT_FILE",
    "STREAMING_RTC_GRPC_CLIENT_KEY_FILE",
)


class TestConfigFromEnv:
    def test_unset_fails_loud_instead_of_guessing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in _ENV:
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(StreamingTargetNotConfiguredError, match="STREAMING_RTC_GRPC_TARGET"):
            RoomsClientConfig.from_env()

    def test_target_without_mtls_files_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in _ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("STREAMING_RTC_GRPC_TARGET", "svc-streaming:50208")
        with pytest.raises(StreamingTargetNotConfiguredError, match="mTLS"):
            RoomsClientConfig.from_env()

    def test_legacy_rest_url_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in _ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("STREAMING_RTC_URL", "http://svc-streaming:8208")
        with pytest.raises(StreamingTargetNotConfiguredError):
            RoomsClientConfig.from_env()

    def test_files_and_target_resolve(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
    ) -> None:
        files = {}
        for name in _ENV[1:]:
            path = tmp_path / name  # type: ignore[operator]
            path.write_bytes(b"pem")
            files[name] = str(path)
            monkeypatch.setenv(name, str(path))
        monkeypatch.setenv("STREAMING_RTC_GRPC_TARGET", "svc-streaming:50208")
        monkeypatch.delenv("STREAMING_RTC_TIMEOUT_SECONDS", raising=False)
        cfg = RoomsClientConfig.from_env()
        assert cfg.target == "svc-streaming:50208"
        assert cfg.timeout_seconds == 10.0
        assert cfg.ca_pem == b"pem"


class TestClientFailures:
    async def test_client_without_config_raises_on_first_call_not_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in _ENV:
            monkeypatch.delenv(name, raising=False)
        client = RoomsClient()  # import-time construction must stay safe
        with pytest.raises(StreamingTargetNotConfiguredError):
            await client.list_rooms(1)

    async def test_missing_issuer_is_a_masked_502_and_no_network(self) -> None:
        channel = grpc.aio.insecure_channel("127.0.0.1:1")
        client = RoomsClient(issuer_provider=lambda: None, channel=channel)
        with pytest.raises(RoomsRpcError) as err:
            await client.list_rooms(1)
        assert err.value.status_code == 502
        assert err.value.code == "STREAMING_AUTH_NOT_CONFIGURED"
        await channel.close()

    async def test_unreachable_server_is_a_503(self) -> None:
        channel = grpc.aio.insecure_channel("127.0.0.1:1")
        client = RoomsClient(
            issuer_provider=lambda: Issuer(),  # type: ignore[arg-type,return-value]
            channel=channel,
            timeout_seconds=1.0,
        )
        with pytest.raises(RoomsRpcError) as err:
            await client.list_rooms(1)
        assert err.value.status_code == 503
        assert err.value.code == "STREAMING_UNAVAILABLE"
        assert "127.0.0.1" not in err.value.message  # no internal address leaks to callers
        await channel.close()


async def test_stub_exposes_every_proto_rpc() -> None:
    names = set(rooms_pb2.DESCRIPTOR.services_by_name["RoomsService"].methods_by_name)
    channel = grpc.aio.insecure_channel("127.0.0.1:1")
    stub = rooms_pb2_grpc.RoomsServiceStub(channel)
    assert names and all(hasattr(stub, n) for n in names)
    await channel.close()


class _Counter:
    """Records `Counter.add` calls so the failure metric can be asserted without an SDK."""

    def __init__(self) -> None:
        self.points: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.points.append((amount, attributes))


@pytest.fixture
def failure_counter(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    counter = _Counter()
    monkeypatch.setattr(calls_proxy, "_upstream_failures", counter)
    return counter


class TestUpstreamFailureObservability:
    """Every failure class is explicit, logged at ERROR (status + message) and counted."""

    @pytest.mark.parametrize(
        "code,http,error_code",
        [
            (grpc.StatusCode.NOT_FOUND, 404, "NOT_FOUND"),
            (grpc.StatusCode.PERMISSION_DENIED, 403, "FORBIDDEN"),
            (grpc.StatusCode.INVALID_ARGUMENT, 400, "INVALID_ARGUMENT"),
            (grpc.StatusCode.RESOURCE_EXHAUSTED, 429, "RESOURCE_EXHAUSTED"),
            (grpc.StatusCode.UNIMPLEMENTED, 501, "STREAMING_UNIMPLEMENTED"),
            (grpc.StatusCode.UNAVAILABLE, 503, "STREAMING_UNAVAILABLE"),
            (grpc.StatusCode.DEADLINE_EXCEEDED, 504, "STREAMING_TIMEOUT"),
            (grpc.StatusCode.UNAUTHENTICATED, 502, "STREAMING_AUTH_REFUSED"),
            (grpc.StatusCode.INTERNAL, 502, "STREAMING_ERROR"),
        ],
    )
    async def test_each_status_maps_logs_and_counts(
        self,
        fake: FakeRooms,
        failure_counter: _Counter,
        caplog: pytest.LogCaptureFixture,
        code: grpc.StatusCode,
        http: int,
        error_code: str,
    ) -> None:
        import blueprints.v1.calls as calls_module

        fake.fail["GetRoom"] = (code, "downstream detail")
        caplog.set_level(logging.ERROR, logger="services.calls_proxy")
        with pytest.raises(RoomsRpcError) as err:
            await calls_module._client.get_room(1, ROOM)
        assert (err.value.status_code, err.value.code) == (http, error_code)
        records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(records) == 1
        message = records[0].getMessage()
        assert code.name in message and "GetRoom" in message
        assert "downstream detail" in message  # the log keeps the detail the caller never sees
        assert failure_counter.points == [(1, {"rpc": "GetRoom", "status": code.name})]

    async def test_missing_issuer_is_logged_and_counted(
        self, failure_counter: _Counter, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = grpc.aio.insecure_channel("127.0.0.1:1")
        client = RoomsClient(issuer_provider=lambda: None, channel=channel)
        caplog.set_level(logging.ERROR, logger="services.calls_proxy")
        with pytest.raises(RoomsRpcError):
            await client.list_rooms(1)
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        assert failure_counter.points == [(1, {"rpc": "(token)", "status": "NOT_CONFIGURED"})]
        await channel.close()

    async def test_issuer_failure_is_logged_counted_and_a_502(
        self, failure_counter: _Counter, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _BrokenIssuer:
            def issue(self, service_id: str, scope: str, *, ttl_seconds: int = 0) -> str:
                raise ServiceJwtError("signing key unreadable")

        channel = grpc.aio.insecure_channel("127.0.0.1:1")
        client = RoomsClient(
            issuer_provider=lambda: _BrokenIssuer(),  # type: ignore[arg-type,return-value]
            channel=channel,
        )
        caplog.set_level(logging.ERROR, logger="services.calls_proxy")
        with pytest.raises(RoomsRpcError) as err:
            await client.list_rooms(1)
        assert (err.value.status_code, err.value.code) == (502, "STREAMING_AUTH_NOT_CONFIGURED")
        assert "signing key unreadable" in caplog.text
        assert failure_counter.points == [(1, {"rpc": "(token)", "status": "ServiceJwtError"})]
        await channel.close()

    async def test_success_records_no_failure(
        self, fake: FakeRooms, failure_counter: _Counter, caplog: pytest.LogCaptureFixture
    ) -> None:
        import blueprints.v1.calls as calls_module

        caplog.set_level(logging.ERROR, logger="services.calls_proxy")
        await calls_module._client.list_rooms(1)
        assert failure_counter.points == []
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_every_client_method_stamps_api_version_v1(fake: FakeRooms) -> None:
    """Every proto RPC, driven through the typed client, carry `api_version == "v1"`."""
    from waddles.streaming.internal.v1 import rooms_pb2

    import blueprints.v1.calls as calls_module

    c = calls_module._client
    admin = {"actor_id": PARTICIPANT, "actor_role": rooms_pb2.ROOM_ROLE_ADMIN}
    await c.list_rooms(1)
    await c.get_room(1, ROOM)
    await c.list_participants(1, ROOM)
    await c.create_room(1, label="x", max_publishers=None)
    await c.delete_room(1, ROOM)
    fake.seed_room(1)
    await c.join_room(1, ROOM, PARTICIPANT)
    await c.leave_room(1, ROOM, PARTICIPANT, **admin)
    await c.kick_participant(1, ROOM, PARTICIPANT, **admin)
    await c.set_participant_muted(1, ROOM, PARTICIPANT, True, **admin)
    await c.set_participant_muted(1, ROOM, PARTICIPANT, False, **admin)
    await c.mute_all(1, ROOM, **admin)
    await c.set_locked(1, ROOM, True, **admin)
    await c.set_locked(1, ROOM, False, **admin)
    await c.raise_hand(1, ROOM, PARTICIPANT)
    await c.list_hands(1, ROOM)
    await c.acknowledge_hand(1, ROOM, PARTICIPANT, **admin)
    await c.lower_hand(1, ROOM, PARTICIPANT, **admin)
    await c.reset_hands(1, ROOM, **admin)
    seen = {name for name, _, _ in fake.calls}
    descriptor = set(rooms_pb2.DESCRIPTOR.services_by_name["RoomsService"].methods_by_name)
    assert seen == descriptor, descriptor ^ seen  # every RPC examined
    assert all(req.api_version == "v1" for _, req, _ in fake.calls)
    assert len(fake.calls) == len(descriptor)  # one call per RPC, none repeated


async def test_wrong_api_version_is_refused_by_the_server_contract(fake: FakeRooms) -> None:
    """Pins the fake to the real server rule: anything but `v1` is UNIMPLEMENTED."""
    from waddles.streaming.internal.v1 import rooms_pb2

    import blueprints.v1.calls as calls_module

    stub = rooms_pb2_grpc.RoomsServiceStub(calls_module._client._channel)  # type: ignore[arg-type]
    with pytest.raises(grpc.aio.AioRpcError) as err:
        await stub.ListRooms(
            rooms_pb2.ListRoomsRequest(api_version="v2", community_id="1"),
            metadata=(("authorization", "Bearer jwt-for-streaming.rooms:read"),),
        )
    assert err.value.code() == grpc.StatusCode.UNIMPLEMENTED
