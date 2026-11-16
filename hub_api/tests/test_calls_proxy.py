"""`services/calls_proxy.py` -- config, token minting and status mapping for the rooms gRPC client.

Route-level behaviour (real in-process gRPC server) lives in `test_v1_calls_blueprint.py`;
the route -> RPC contract lives in `test_calls_rpc_contract.py`. This file covers the
fail-loud configuration paths and the client's own error translation.
"""

from __future__ import annotations

import grpc
import pytest

# isort: split
import grpc_internal  # noqa: F401  (puts the generated pb/ tree on sys.path)

# isort: split
from waddles.streaming.internal.v1 import rooms_pb2, rooms_pb2_grpc

from services.calls_proxy import (
    RoomsClient,
    RoomsClientConfig,
    RoomsRpcError,
    StreamingTargetNotConfiguredError,
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

    async def test_unreachable_server_is_a_masked_502(self) -> None:
        class _Issuer:
            def issue(self, service_id: str, scope: str, *, ttl_seconds: int = 0) -> str:
                return "t"

        channel = grpc.aio.insecure_channel("127.0.0.1:1")
        client = RoomsClient(
            issuer_provider=lambda: _Issuer(),  # type: ignore[arg-type,return-value]
            channel=channel,
            timeout_seconds=1.0,
        )
        with pytest.raises(RoomsRpcError) as err:
            await client.list_rooms(1)
        assert err.value.status_code == 502
        assert err.value.code == "STREAMING_UNAVAILABLE"
        await channel.close()


async def test_stub_exposes_every_proto_rpc() -> None:
    names = set(rooms_pb2.DESCRIPTOR.services_by_name["RoomsService"].methods_by_name)
    channel = grpc.aio.insecure_channel("127.0.0.1:1")
    stub = rooms_pb2_grpc.RoomsServiceStub(channel)
    assert names and all(hasattr(stub, n) for n in names)
    await channel.close()
