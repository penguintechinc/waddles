"""Secrets are encrypted at rest, decrypted only at the point of use, and never logged or traced."""

from __future__ import annotations

import base64
import logging
import re
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event

from services.provider_connections import service as service_module
from services.provider_connections import telemetry
from services.provider_connections.errors import VaultCorrupt, VaultUnavailable
from services.provider_connections.service import ProviderConnectionService
from services.provider_connections.store import ConnectionRow, SealedSecrets
from services.provider_connections.vault import PlatformKeyVault
from tests.provider_connections.conftest import (
    BOT,
    CHANNEL_LOGIN,
    OTHER_CHANNEL_LOGIN,
    TEST_CREDENTIAL_KEY,
    FakeClock,
)
from tests.provider_connections.fake_twitch import APP_CLIENT_SECRET, FakeTwitch
from tests.provider_connections.support import create_twitch, open_row, raw_rows, set_column


class TestEncryptionAtRest:
    async def test_tokens_are_ciphertext_in_the_row_and_nowhere_in_the_database_file(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        access, refresh = bot_tokens
        assert refresh is not None
        await create_twitch(pc_service, pc_db, bot_tokens)
        row = (await raw_rows(pc_db, "platform_connections"))[0]

        for column, plaintext in (("access_token", access), ("refresh_token", refresh)):
            stored = row[column]
            assert stored and stored != plaintext and plaintext not in stored
            blob = base64.b64decode(stored, validate=True)
            assert plaintext.encode() not in blob
            assert len(blob) == 12 + len(plaintext) + 16  # iv || ciphertext || GCM tag
            assert open_row(row, column) == plaintext

        assert access not in repr(row["resource_metadata"])
        assert refresh not in repr(row["resource_metadata"])
        # The strongest form of "at rest": scan every byte the database wrote to disk.
        on_disk = pc_db.db_path.read_bytes()
        assert len(on_disk) > 4096  # the scan below must examine a real database file
        for secret in (access, refresh):
            assert secret.encode() not in on_disk

    async def test_rotating_credentials_leaves_no_trace_of_the_old_ones_on_disk(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        new_access, new_refresh = fake_twitch.mint(account=BOT)
        await pc_service.update(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            actor_user_id=1,
            connection_id=view.binding.connection.id,
            enabled=None,
            metadata=None,
            access_token=new_access,
            refresh_token=new_refresh,
        )
        rows = await raw_rows(pc_db, "platform_connections")
        assert open_row(rows[0]) == new_access
        on_disk = pc_db.db_path.read_bytes()
        for secret in (new_access, new_refresh):
            assert secret and secret.encode() not in on_disk

    async def test_the_same_secret_never_encrypts_to_the_same_ciphertext(
        self, pc_service: ProviderConnectionService, pc_db: Any, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT, with_refresh=False)
        a = await create_twitch(pc_service, pc_db, (access, None))
        await pc_service.delete(
            tenant_id=pc_db.seed.tenant_id,
            community_id=pc_db.seed.community_id,
            connection_id=a.binding.connection.id,
        )
        first = PlatformKeyVault().seal(access, aad=b"x")
        assert first != PlatformKeyVault().seal(access, aad=b"x")

    async def test_a_ciphertext_from_another_key_is_never_decrypted(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ee" * 32)
        with pytest.raises(VaultCorrupt):
            await pc_service.resolve(
                community_id=pc_db.seed.community_id, connection_id=view.binding.connection.id
            )

    async def test_a_tampered_row_fails_closed_instead_of_returning_garbage(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        stored = (await raw_rows(pc_db, "platform_connections"))[0]["access_token"]
        raw = bytearray(base64.b64decode(stored))
        raw[20] ^= 0xFF
        await set_column(
            pc_db, "platform_connections", cid, access_token=base64.b64encode(bytes(raw)).decode()
        )
        with pytest.raises(VaultCorrupt):
            await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)

    async def test_a_missing_key_refuses_to_store_anything(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
        with pytest.raises(VaultUnavailable):
            await create_twitch(pc_service, pc_db, bot_tokens)
        assert await raw_rows(pc_db, "platform_connections") == []  # no plaintext fallback


class TestNoSecretIsEverSelectedForAdminReads:
    async def test_list_get_toggle_and_delete_never_read_a_token_column(
        self, pc_service: ProviderConnectionService, pc_db: Any, bot_tokens: Any
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        statements: list[str] = []

        def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = pc_db.install_dal.engine.sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            scope = {"tenant_id": pc_db.seed.tenant_id, "community_id": pc_db.seed.community_id}
            await pc_service.list(**scope)
            await pc_service.get(connection_id=cid, **scope)
            await pc_service.update(
                connection_id=cid,
                actor_user_id=1,
                enabled=False,
                metadata=None,
                access_token=None,
                refresh_token=None,
                **scope,
            )
            await pc_service.delete(connection_id=cid, **scope)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        assert statements  # the scan below must examine something
        for sql in statements:
            scrubbed = re.sub(
                r"platform_connections\.refresh_token IS NOT NULL AS has_refresh_token", "", sql
            )
            assert "access_token" not in scrubbed, sql
            assert "refresh_token" not in scrubbed, sql
            assert "sealed_secret" not in scrubbed, sql

    def test_read_models_do_not_repr_secrets(self) -> None:
        sealed = SealedSecrets(
            access_token="CIPHERTEXT-A", refresh_token="CIPHERTEXT-R", seal_version=1
        )
        assert "CIPHERTEXT" not in repr(sealed)
        row = ConnectionRow(
            1,
            uuid4(),
            1,
            "twitch",
            "twitch_channel",
            "7",
            "active",
            None,
            {},
            None,
            True,
            None,
            None,
            None,
        )
        assert "CIPHERTEXT" not in repr(row)


#: Loggers whose output is ours (or the HTTP client's, which sees every Twitch URL). Third-party DB
#: *driver* loggers are excluded on purpose: ``aiosqlite`` (a test-only driver) logs bound
#: parameters at DEBUG, which production's asyncpg does not -- and no code here can change that.
_OWN_LOGGERS = (
    "services.provider_connections",
    "blueprints.v1.provider_connections",
    "httpx",
    "httpcore",
)


def _own_log_text(caplog: pytest.LogCaptureFixture) -> tuple[int, str]:
    records = [r for r in caplog.records if r.name.startswith(_OWN_LOGGERS)]
    return len(records), "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in records)


def _debug_own_loggers(caplog: pytest.LogCaptureFixture) -> None:
    for name in _OWN_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)


class TestNothingSecretInLogs:
    async def test_full_lifecycle_including_failures_logs_no_secret_and_no_account_name(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _debug_own_loggers(caplog)
        access, refresh = bot_tokens
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        scope = {"tenant_id": pc_db.seed.tenant_id, "community_id": pc_db.seed.community_id}
        await pc_service.list(**scope)
        await pc_service.get(connection_id=cid, **scope)
        # a rotation, a metadata change, a refresh, a transient failure, a rejection, a delete
        rotated_access, rotated_refresh = fake_twitch.mint(account=BOT)
        await pc_service.update(
            connection_id=cid,
            actor_user_id=1,
            enabled=None,
            metadata=None,
            access_token=rotated_access,
            refresh_token=rotated_refresh,
            **scope,
        )
        await pc_service.update(
            connection_id=cid,
            actor_user_id=1,
            enabled=None,
            metadata={"channel_login": OTHER_CHANNEL_LOGIN},
            access_token=None,
            refresh_token=None,
            **scope,
        )
        clock.advance(seconds=14000)
        refreshed = await pc_service.resolve(
            community_id=pc_db.seed.community_id, connection_id=cid
        )
        fake_twitch.down = True
        clock.advance(seconds=14500)
        with pytest.raises(Exception):  # noqa: B017, PT011 - any failure; we only scan the logs
            await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)
        fake_twitch.down = False
        await pc_service.delete(connection_id=cid, **scope)

        count, text = _own_log_text(caplog)
        assert count > 10  # the scan below must examine real output
        secrets = [
            access,
            refresh,
            rotated_access,
            rotated_refresh,
            refreshed.access_token,
            APP_CLIENT_SECRET,
            TEST_CREDENTIAL_KEY,
        ]
        for secret in secrets:
            assert secret and secret not in text
        for name in (BOT.login, CHANNEL_LOGIN, OTHER_CHANNEL_LOGIN):
            assert name not in text, name

    async def test_a_hostile_provider_error_body_never_reaches_the_logs_or_the_error(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        fake_twitch: FakeTwitch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _debug_own_loggers(caplog)
        fake_twitch.raw_body_override["validate"] = (
            b'{"message": "victim@acme.test sk-live-HOSTILE"}'
        )
        with pytest.raises(Exception) as info:  # noqa: PT011
            await create_twitch(pc_service, pc_db, bot_tokens)
        count, logs = _own_log_text(caplog)
        assert count > 0
        blob = f"{info.value} {logs}"
        assert "victim@acme.test" not in blob and "HOSTILE" not in blob


class TestNothingSecretInTelemetry:
    @pytest.fixture
    def recorders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, list[tuple[float, dict[str, Any]]]]:
        calls: dict[str, list[tuple[float, dict[str, Any]]]] = {}

        class _Instrument:
            def __init__(self, name: str) -> None:
                self.name = name
                calls[name] = []

            def add(self, amount: float, attributes: dict[str, Any] | None = None) -> None:
                calls[self.name].append((amount, attributes or {}))

            def record(self, amount: float, attributes: dict[str, Any] | None = None) -> None:
                calls[self.name].append((amount, attributes or {}))

        for module, attr in (
            (telemetry, "operation_counter"),
            (telemetry, "operation_duration"),
            (telemetry, "provider_call_duration"),
            (service_module, "token_refresh_counter"),
            (service_module, "dataplane_list_size"),
        ):
            monkeypatch.setattr(
                module, attr, _Instrument(f"{module.__name__.split('.')[-1]}.{attr}")
            )
        return calls

    @pytest.fixture
    def spans(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("test"))
        return exporter

    async def test_metrics_and_spans_carry_only_bounded_labels(
        self,
        pc_service: ProviderConnectionService,
        pc_db: Any,
        bot_tokens: Any,
        clock: FakeClock,
        recorders: dict[str, Any],
        spans: Any,
        fake_twitch: FakeTwitch,
    ) -> None:
        view = await create_twitch(pc_service, pc_db, bot_tokens)
        cid = view.binding.connection.id
        clock.advance(seconds=14000)
        resolved = await pc_service.resolve(community_id=pc_db.seed.community_id, connection_id=cid)
        await pc_service.list_for_dataplane(provider="twitch", capability=None, after=0, limit=10)
        fake_twitch.status_override["validate"] = 401
        with pytest.raises(Exception):  # noqa: B017, PT011
            await create_twitch(pc_service, pc_db, bot_tokens, channel=OTHER_CHANNEL_LOGIN)

        ops = recorders["telemetry.operation_counter"]
        assert (1, {"provider": "twitch", "operation": "create", "outcome": "ok"}) in ops
        assert (1, {"provider": "twitch", "operation": "resolve", "outcome": "ok"}) in ops
        assert (
            1,
            {"provider": "twitch", "operation": "create", "outcome": "credentials_rejected"},
        ) in ops
        assert {a["call"] for _, a in recorders["telemetry.provider_call_duration"]} >= {
            "validate",
            "resolve",
            "refresh",
        }
        assert (1, {"provider": "twitch", "outcome": "refreshed"}) in recorders[
            "service.token_refresh_counter"
        ]
        assert all(v >= 0 for v, _ in recorders["telemetry.operation_duration"])

        blob = repr(recorders)
        for forbidden in (
            bot_tokens[0],
            bot_tokens[1],
            resolved.access_token,
            BOT.login,
            CHANNEL_LOGIN,
        ):
            assert forbidden and forbidden not in blob
        label_keys = {
            key for entries in recorders.values() for _, attrs in entries for key in attrs
        }
        assert label_keys <= {"provider", "operation", "outcome", "call"}  # bounded enums only

        finished = spans.get_finished_spans()
        assert {s.name for s in finished} >= {
            "provider_connections.create",
            "provider_connections.resolve",
            "provider_connections.dataplane_list",
        }
        span_blob = repr(
            [(s.name, dict(s.attributes or {}), s.status.description) for s in finished]
        )
        for forbidden in (bot_tokens[0], resolved.access_token, BOT.login, CHANNEL_LOGIN):
            assert forbidden not in span_blob
        failed = [s for s in finished if s.status.status_code.name == "ERROR"]
        assert failed and all("rejected" not in (s.status.description or "") for s in failed)
