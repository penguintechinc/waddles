"""Art. 17 erasure core -- `services/data_privacy_service.py::anonymize_user_data`.

GRC#1 regression suite. Real pydal against a sqlite file, real SQL
triggers for fault injection (no mocking of the transaction boundary:
the property under test IS the database's commit/rollback behaviour).

Three properties, each with its own class:

- `TestChatMessageErasure` -- erasure used to EXPORT `hub_chat_messages`
  (Art. 15) but never DELETE them (Art. 17): a "completed" erasure left the
  subject's message bodies, platform username and avatar behind.
- `TestRetention` -- consent records, the consent change log, the platform
  audit trail and the deletion ledger MUST survive erasure (GDPR Art. 5(2)
  accountability). Pinned so a future "tidy-up" cannot delete them.
- `TestAtomicity` -- every delete, the `hub_users` anonymization and the
  `completed` ledger row commit as ONE transaction. Before the fix each
  statement committed on its own (`AsyncDAL.delete_async()` commits per
  call, #280), so a mid-way failure left a half-erased subject.

Fail-first proofs (executed 2026-10-10, not narrated -- each mutation applied
to `_sync_erase()` in-process, this file + `test_admin_data_privacy.py` re-run,
then reverted to green):
- chat-message delete removed: 9 red -- all 5 of `TestChatMessageErasure`, plus
  `test_a_failed_erasure_can_be_retried_to_completion`,
  `test_no_email_account_is_erased_atomically_too`, and the two admin-console
  chat tests (single erase + bulk erase).
- `dal.commit()` after each delete (the old per-statement commit model): 4 red --
  `test_anonymization_failure_rolls_back_every_delete`,
  `test_ledger_write_failure_rolls_back_the_anonymization`,
  `test_success_commits_exactly_once`,
  `test_self_service_route_failure_leaves_the_account_whole`.
- a `cookie_consent` delete added (Art. 5(2) violation): 3 red --
  `TestRetention::test_consent_and_audit_logs_survive_erasure`,
  `TestRetention::test_erasure_never_references_a_retained_table`, and the admin
  console's `test_erase_removes_chat_messages_but_keeps_consent_and_audit_logs`.
- `start_as_current_span()` left at the OTel SDK defaults (record_exception /
  set_status_on_exception on): `TestTelemetry::test_failure_emits_error_span_and_counter_
  without_driver_text` red -- the span status carried the raw driver message.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from flask_core.database import AsyncDAL
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

import services.bundle_telemetry as telemetry
from blueprints.v1.data_privacy import data_privacy_bp
from services import data_privacy_service as privacy
from services.schema import bind_admin_privacy_tables
from tests.conftest import TENANT_SLUG, make_user_token

#: Fixed sentinel string the fault-injection triggers raise with -- must NEVER
#: reach `data_deletion_requests.error_detail` (driver text can echo PII).
TRIGGER_MESSAGE = "erasure-fault-injected"


@pytest.fixture(scope="module")
def module_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """Build the schema ONCE per module (defining ~100 tables costs seconds)."""
    path = tmp_path_factory.mktemp("erasure") / "erasure.db"
    async_dal = AsyncDAL(f"sqlite://{path}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=True,
    )
    bind_admin_privacy_tables(dal, migrate=True)
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme", is_active=True)
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    dal.commit()
    yield async_dal
    dal.close()


@pytest.fixture
def db(module_db: Any) -> Any:
    """The module's database with every non-`tenants` table emptied -- per-test isolation."""
    dal = module_db.dal
    for table_name in dal.tables:
        if table_name != "tenants":
            dal(dal[table_name]).delete()
    dal.commit()
    return module_db


@pytest.fixture
def app(db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(data_privacy_bp)
    quart_app.config["dal"] = db.dal
    quart_app.config["async_dal"] = db
    return quart_app


def _now() -> datetime:
    return datetime.now(UTC)


def _user(db: Any, *, name: str) -> int:
    user_id = db.dal.hub_users.insert(
        email=f"{name}@example.com",
        username=name,
        display_name=name.title(),
        password_hash=None,
        is_active=True,
        created_at=_now(),
        updated_at=_now(),
    )
    db.dal.commit()
    return int(user_id)


def _chat(db: Any, *, user_id: int | None, community_id: int, text: str) -> None:
    db.dal.hub_chat_messages.insert(
        community_id=community_id,
        channel_name="general",
        sender_hub_user_id=user_id,
        sender_platform="discord",
        sender_username="someone",
        sender_avatar_url="https://cdn.example/avatar.png",
        message_content=text,
        message_type="text",
        created_at=_now(),
    )
    db.dal.commit()


def _seed_footprint(db: Any, user_id: int) -> None:
    """Give `user_id` a row in every table erasure is expected to clear."""
    dal = db.dal
    dal.hub_user_profiles.insert(hub_user_id=user_id, bio="about me")
    dal.hub_sessions.insert(
        session_token=f"token-{user_id}",
        user_id=user_id,
        platform="discord",
        platform_username="someone",
        is_active=True,
        created_at=_now(),
    )
    dal.user_passkeys.insert(
        user_id=user_id,
        credential_id=f"cred-{user_id}",
        public_key="pk",
        device_name="laptop",
        created_at=_now(),
    )
    dal.hub_temp_passwords.insert(
        user_identifier=_user_row(db, user_id).email,
        password_hash=f"tmp-{user_id}",
        expires_at=_now(),
        created_at=_now(),
    )
    dal.activity_message_events.insert(
        community_id=1,
        hub_user_id=user_id,
        platform="discord",
        platform_user_id=f"p{user_id}",
        platform_username="someone",
        created_at=_now(),
    )
    dal.activity_watch_sessions.insert(
        community_id=1,
        hub_user_id=user_id,
        platform="twitch",
        platform_user_id=f"p{user_id}",
        platform_username="someone",
        channel_id="c1",
        created_at=_now(),
    )
    dal.commit()
    _chat(db, user_id=user_id, community_id=1, text="first message")
    _chat(db, user_id=user_id, community_id=2, text="second message")


def _footprint(db: Any, user_id: int) -> dict[str, int]:
    """Row counts per erasable table for `user_id` (commits so no read lock lingers)."""
    dal = db.dal
    counts = {
        "profiles": dal(dal.hub_user_profiles.hub_user_id == user_id).count(),
        "sessions": dal(dal.hub_sessions.user_id == user_id).count(),
        "passkeys": dal(dal.user_passkeys.user_id == user_id).count(),
        "temp_passwords": dal(dal.hub_temp_passwords.password_hash == f"tmp-{user_id}").count(),
        "message_events": dal(dal.activity_message_events.hub_user_id == user_id).count(),
        "watch_sessions": dal(dal.activity_watch_sessions.hub_user_id == user_id).count(),
        "chat_messages": dal(dal.hub_chat_messages.sender_hub_user_id == user_id).count(),
    }
    dal.commit()
    return counts


def _user_row(db: Any, user_id: int) -> Any:
    row = db.dal(db.dal.hub_users.id == user_id).select().first()
    db.dal.commit()
    return row


def _ledger(db: Any, user_id: int) -> list[Any]:
    rows = db.dal(db.dal.data_deletion_requests.hub_user_id == user_id).select(
        orderby=db.dal.data_deletion_requests.id
    )
    db.dal.commit()
    return list(rows)


def _install_trigger(db: Any, name: str, sql: str) -> None:
    db.dal.executesql(f"DROP TRIGGER IF EXISTS {name}")
    db.dal.executesql(sql)
    db.dal.commit()


def _drop_trigger(db: Any, name: str) -> None:
    db.dal.executesql(f"DROP TRIGGER IF EXISTS {name}")
    db.dal.commit()


@pytest.fixture
def block_anonymization(db: Any) -> Iterator[None]:
    """Make the `hub_users` anonymization UPDATE fail -- AFTER every delete has run."""
    _install_trigger(
        db,
        "block_anonymize",
        "CREATE TRIGGER block_anonymize BEFORE UPDATE ON hub_users "
        "WHEN NEW.username LIKE 'deleted_%' "
        f"BEGIN SELECT RAISE(ABORT, '{TRIGGER_MESSAGE}'); END",
    )
    yield
    _drop_trigger(db, "block_anonymize")


@pytest.fixture
def block_completion_ledger(db: Any) -> Iterator[None]:
    """Make the final `completed` ledger INSERT fail -- AFTER the anonymization ran."""
    _install_trigger(
        db,
        "block_ledger",
        "CREATE TRIGGER block_ledger BEFORE INSERT ON data_deletion_requests "
        "WHEN NEW.status = 'completed' "
        f"BEGIN SELECT RAISE(ABORT, '{TRIGGER_MESSAGE}'); END",
    )
    yield
    _drop_trigger(db, "block_ledger")


class TestChatMessageErasure:
    """GRC#1: `hub_chat_messages` are exported (Art. 15) so they must be erased (Art. 17)."""

    async def test_erasure_removes_the_subjects_chat_messages(self, db: Any) -> None:
        user_id = _user(db, name="chatty")
        _seed_footprint(db, user_id)
        assert _footprint(db, user_id)["chat_messages"] == 2

        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email="chatty@example.com")

        assert _footprint(db, user_id) == {
            "profiles": 0,
            "sessions": 0,
            "passkeys": 0,
            "temp_passwords": 0,
            "message_events": 0,
            "watch_sessions": 0,
            "chat_messages": 0,
        }

    async def test_erasure_leaves_other_users_chat_messages(self, db: Any) -> None:
        victim = _user(db, name="victim")
        bystander = _user(db, name="bystander")
        _chat(db, user_id=victim, community_id=1, text="mine")
        _chat(db, user_id=bystander, community_id=1, text="theirs")
        _chat(db, user_id=None, community_id=1, text="anonymous system message")

        await privacy.anonymize_user_data(db, db.dal, user_id=victim, email="victim@example.com")

        remaining = db.dal(db.dal.hub_chat_messages.id > 0).select(
            db.dal.hub_chat_messages.message_content, orderby=db.dal.hub_chat_messages.id
        )
        db.dal.commit()
        assert [r.message_content for r in remaining] == ["theirs", "anonymous system message"]

    async def test_ledger_counts_deleted_chat_messages(self, db: Any) -> None:
        user_id = _user(db, name="counted")
        _seed_footprint(db, user_id)

        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email="counted@example.com")

        [row] = _ledger(db, user_id)
        assert row.status == "completed"
        assert row.deletion_scope["chat_messages"] == 2
        assert row.deletion_scope["hub_users_anonymized"] == 1

    async def test_export_and_erasure_cover_the_same_chat_messages(self, db: Any) -> None:
        """Whatever Art. 15 discloses, Art. 17 must remove -- the two lists cannot drift."""
        user_id = _user(db, name="symmetric")
        _seed_footprint(db, user_id)
        before, _ = await privacy.collect_user_data(db, db.dal, user_id=user_id)
        assert len(before["chat_messages"]) == 2

        await privacy.anonymize_user_data(
            db, db.dal, user_id=user_id, email="symmetric@example.com"
        )

        after, failures = await privacy.collect_user_data(db, db.dal, user_id=user_id)
        assert failures == []
        assert after["chat_messages"] == []
        assert after["message_activity"] == []
        assert after["watch_activity"] == []

    async def test_self_service_route_removes_chat_messages(self, app: Quart, db: Any) -> None:
        user_id = _user(db, name="viaroute")
        _seed_footprint(db, user_id)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}

        response = await app.test_client().delete("/api/v1/user/me/data", headers=headers, json={})

        assert response.status_code == 200
        assert _footprint(db, user_id)["chat_messages"] == 0
        assert _user_row(db, user_id).email == f"deleted_{user_id}@deleted.waddlebot"


class TestRetention:
    """Consent + audit evidence outlives the subject's data -- GDPR Art. 5(2)."""

    @staticmethod
    def _seed_evidence(db: Any, user_id: int) -> dict[str, int]:
        dal = db.dal
        dal.cookie_consent.insert(
            user_id=user_id,
            consent_id=f"consent-{user_id}",
            preferences={"analytics": True},
            consent_version="1.0",
            consent_method="banner",
            ip_address="203.0.113.7",
            consented_at=_now(),
        )
        dal.cookie_audit_log.insert(
            consent_id=f"consent-{user_id}",
            user_id=user_id,
            action="granted",
            category="analytics",
            new_value=True,
            consent_version="1.0",
            created_at=_now(),
        )
        dal.audit_log.insert(
            user_id=user_id,
            action="login",
            target_type="user",
            target_id=str(user_id),
            created_at=_now(),
        )
        dal.commit()
        return TestRetention._evidence_counts(db, user_id)

    @staticmethod
    def _evidence_counts(db: Any, user_id: int) -> dict[str, int]:
        dal = db.dal
        counts = {
            "cookie_consent": dal(dal.cookie_consent.user_id == user_id).count(),
            "cookie_audit_log": dal(dal.cookie_audit_log.user_id == user_id).count(),
            "audit_log": dal(dal.audit_log.user_id == user_id).count(),
        }
        dal.commit()
        return counts

    async def test_consent_and_audit_logs_survive_erasure(self, db: Any) -> None:
        user_id = _user(db, name="evidence")
        _seed_footprint(db, user_id)
        before = self._seed_evidence(db, user_id)
        assert before == {"cookie_consent": 1, "cookie_audit_log": 1, "audit_log": 1}

        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email="evidence@example.com")

        assert self._evidence_counts(db, user_id) == before
        consent = db.dal(db.dal.cookie_consent.user_id == user_id).select().first()
        db.dal.commit()
        assert consent.consent_id == f"consent-{user_id}"
        assert consent.consent_version == "1.0"
        assert [r.status for r in _ledger(db, user_id)] == ["completed"]  # the proof of erasure

    async def test_failed_erasure_also_retains_everything(
        self, db: Any, block_anonymization: None
    ) -> None:
        user_id = _user(db, name="evidence2")
        before = self._seed_evidence(db, user_id)

        with pytest.raises(Exception, match=TRIGGER_MESSAGE):
            await privacy.anonymize_user_data(
                db, db.dal, user_id=user_id, email="evidence2@example.com"
            )

        assert self._evidence_counts(db, user_id) == before

    def test_erasure_never_references_a_retained_table(self) -> None:
        source = inspect.getsource(privacy._sync_erase)
        for table in privacy.ERASURE_RETAINED_TABLES:
            if table == "data_deletion_requests":
                # The ledger is INSERTed to (never deleted from) -- pin the verb.
                assert "dal.data_deletion_requests.insert(" in source
                assert "data_deletion_requests.id" not in source
                assert "data_deletion_requests.hub_user_id ==" not in source
                continue
            assert f"dal.{table}" not in source, f"erasure must not touch {table} (Art. 5(2))"

    def test_retained_table_set_is_exactly_the_documented_one(self) -> None:
        assert privacy.ERASURE_RETAINED_TABLES == (
            "cookie_consent",
            "cookie_audit_log",
            "audit_log",
            "data_deletion_requests",
        )


class TestAtomicity:
    """Erasure is all-or-nothing -- a failure leaves the subject exactly as they were."""

    async def test_anonymization_failure_rolls_back_every_delete(
        self, db: Any, block_anonymization: None
    ) -> None:
        user_id = _user(db, name="halfway")
        _seed_footprint(db, user_id)
        before = _footprint(db, user_id)
        assert all(count > 0 for count in before.values())

        with pytest.raises(Exception, match=TRIGGER_MESSAGE):
            await privacy.anonymize_user_data(
                db, db.dal, user_id=user_id, email="halfway@example.com"
            )

        assert _footprint(db, user_id) == before  # no delete survived the failure
        row = _user_row(db, user_id)
        assert row.email == "halfway@example.com"
        assert row.is_active is True

    async def test_ledger_write_failure_rolls_back_the_anonymization(
        self, db: Any, block_completion_ledger: None
    ) -> None:
        """No erasure without its proof: the completion row and the erasure commit together."""
        user_id = _user(db, name="noledger")
        _seed_footprint(db, user_id)
        before = _footprint(db, user_id)

        with pytest.raises(Exception, match=TRIGGER_MESSAGE):
            await privacy.anonymize_user_data(
                db, db.dal, user_id=user_id, email="noledger@example.com"
            )

        assert _footprint(db, user_id) == before
        assert _user_row(db, user_id).email == "noledger@example.com"  # not anonymized
        assert [r.status for r in _ledger(db, user_id)] == ["failed"]

    async def test_failure_is_recorded_then_the_original_error_propagates(
        self, db: Any, block_anonymization: None
    ) -> None:
        user_id = _user(db, name="recorded")

        with pytest.raises(Exception, match=TRIGGER_MESSAGE) as caught:
            await privacy.anonymize_user_data(
                db, db.dal, user_id=user_id, email="recorded@example.com"
            )

        [row] = _ledger(db, user_id)
        assert row.status == "failed"
        assert row.completed_at is None
        assert row.error_detail == privacy.describe_db_error(caught.value)

    async def test_failure_record_carries_no_driver_text_or_pii(
        self, db: Any, block_anonymization: None
    ) -> None:
        """`error_detail` is retained forever -- exception TYPE only, never message/values."""
        user_id = _user(db, name="private")

        with pytest.raises(Exception, match=TRIGGER_MESSAGE):
            await privacy.anonymize_user_data(
                db, db.dal, user_id=user_id, email="private@example.com"
            )

        [row] = _ledger(db, user_id)
        assert TRIGGER_MESSAGE not in row.error_detail
        assert "private@example.com" not in row.error_detail
        assert row.error_detail.startswith("type=")

    async def test_failure_to_record_the_failure_never_masks_the_original_error(
        self, db: Any, block_anonymization: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id = _user(db, name="unrecordable")

        async def failing_insert(table: Any, **fields: Any) -> Any:
            raise OSError("cannot record either")

        monkeypatch.setattr(db, "insert_async", failing_insert)

        with pytest.raises(Exception, match=TRIGGER_MESSAGE):  # NOT the OSError
            await privacy.anonymize_user_data(
                db, db.dal, user_id=user_id, email="unrecordable@example.com"
            )

        assert _ledger(db, user_id) == []

    async def test_failure_logs_are_pii_free(
        self, db: Any, block_anonymization: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        user_id = _user(db, name="quiet")
        _seed_footprint(db, user_id)

        with caplog.at_level("DEBUG", logger=privacy.logger.name):
            with pytest.raises(Exception, match=TRIGGER_MESSAGE):
                await privacy.anonymize_user_data(
                    db, db.dal, user_id=user_id, email="quiet@example.com"
                )

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "dsar.erasure_failed" in text
        assert "quiet@example.com" not in text
        assert TRIGGER_MESSAGE not in text

    async def test_a_failed_erasure_can_be_retried_to_completion(self, db: Any) -> None:
        user_id = _user(db, name="retry")
        _seed_footprint(db, user_id)
        _install_trigger(
            db,
            "block_retry",
            "CREATE TRIGGER block_retry BEFORE UPDATE ON hub_users "
            "WHEN NEW.username LIKE 'deleted_%' "
            f"BEGIN SELECT RAISE(ABORT, '{TRIGGER_MESSAGE}'); END",
        )
        try:
            with pytest.raises(Exception, match=TRIGGER_MESSAGE):
                await privacy.anonymize_user_data(
                    db, db.dal, user_id=user_id, email="retry@example.com"
                )
        finally:
            _drop_trigger(db, "block_retry")

        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email="retry@example.com")

        assert sum(_footprint(db, user_id).values()) == 0
        assert _user_row(db, user_id).email == f"deleted_{user_id}@deleted.waddlebot"
        assert [r.status for r in _ledger(db, user_id)] == ["failed", "completed"]

    async def test_success_commits_exactly_once(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One transaction == one commit; per-statement commits are the bug this pins."""
        user_id = _user(db, name="onecommit")
        _seed_footprint(db, user_id)
        commits: list[int] = []
        rollbacks: list[int] = []
        real_commit, real_rollback = db.dal.commit, db.dal.rollback
        monkeypatch.setattr(db.dal, "commit", lambda: (commits.append(1), real_commit())[1])
        monkeypatch.setattr(db.dal, "rollback", lambda: (rollbacks.append(1), real_rollback())[1])

        await privacy.anonymize_user_data(
            db, db.dal, user_id=user_id, email="onecommit@example.com"
        )

        assert (len(commits), len(rollbacks)) == (1, 0)

    async def test_no_email_account_is_erased_atomically_too(self, db: Any) -> None:
        user_id = _user(db, name="noemail")
        db.dal(db.dal.hub_users.id == user_id).update(email=None)
        db.dal.commit()
        _chat(db, user_id=user_id, community_id=1, text="no email on this account")

        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email=None)

        [row] = _ledger(db, user_id)
        assert row.status == "completed"
        assert row.deletion_scope["temp_passwords"] == 0
        assert row.deletion_scope["chat_messages"] == 1
        assert sum(_footprint(db, user_id).values()) == 0

    async def test_self_service_route_failure_leaves_the_account_whole(
        self, app: Quart, db: Any, block_anonymization: None
    ) -> None:
        """End to end: a 500 from DELETE /user/me/data means NOTHING was erased."""
        user_id = _user(db, name="viaroute2")
        _seed_footprint(db, user_id)
        before = _footprint(db, user_id)
        headers = {"Authorization": f"Bearer {make_user_token(user_id=user_id)}"}

        response = await app.test_client().delete("/api/v1/user/me/data", headers=headers, json={})

        assert response.status_code == 500
        assert _footprint(db, user_id) == before
        assert _user_row(db, user_id).email == "viaroute2@example.com"


class TestModuleDocumentation:
    def test_erasure_docstrings_state_the_all_or_nothing_and_retention_contract(self) -> None:
        assert "all-or-nothing" in (privacy.anonymize_user_data.__doc__ or "").lower()
        assert "ERASURE_RETAINED_TABLES" in (privacy.anonymize_user_data.__doc__ or "")
        # The stale "no transaction primitive" claim this PR removed must not return.
        source = Path(privacy.__file__).read_text()
        assert "Not wrapped in an explicit pydal transaction" not in source


@dataclass(slots=True)
class OtelSink:
    """In-process OTel SDK sinks: the local OTLP test sink for this module's telemetry."""

    spans: InMemorySpanExporter
    metrics: InMemoryMetricReader

    def points(self, name: str) -> list[Any]:
        """All data points of metric `name` received so far (empty list if none)."""
        data = self.metrics.get_metrics_data()
        if data is None:
            return []
        return [
            point
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]


@pytest.fixture
def otel(monkeypatch: pytest.MonkeyPatch) -> OtelSink:
    """Real SDK tracer/meter providers feeding in-memory sinks; erasure instruments rebound."""
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(telemetry, "_TRACER", tracer_provider.get_tracer("test"))
    monkeypatch.setattr(telemetry, "_METER", meter_provider.get_meter("test"))
    monkeypatch.setattr(privacy, "_INSTRUMENTS", None)
    return OtelSink(spans=exporter, metrics=reader)


class TestTelemetry:
    """Erasure emits a span + counter + duration/row histograms -- PII-free (testing.md)."""

    async def test_success_emits_span_and_metrics(self, db: Any, otel: OtelSink) -> None:
        user_id = _user(db, name="observed")
        _seed_footprint(db, user_id)  # 1+1+1+1+1+1+2 = 8 deletable rows

        await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email="observed@example.com")

        [span] = otel.spans.get_finished_spans()
        [total] = otel.points("waddles_hub_dsar_erasure_total")
        [duration] = otel.points("waddles_hub_dsar_erasure_duration_ms")
        [rows] = otel.points("waddles_hub_dsar_erasure_rows")
        print(
            f"telemetry check: spans={1} counter_points={1} "
            f"duration_points={duration.count} row_points={rows.count}"
        )
        assert span.name == "hub.dsar.erase"
        assert span.status.status_code == trace.StatusCode.UNSET
        assert span.attributes["rows_deleted"] == 8
        assert (total.value, dict(total.attributes)) == (1, {"result": "completed"})
        assert duration.count == 1 and duration.sum >= 0
        assert (rows.count, rows.sum) == (1, 8)

    async def test_failure_emits_error_span_and_counter_without_driver_text(
        self, db: Any, otel: OtelSink, block_anonymization: None
    ) -> None:
        user_id = _user(db, name="observedfail")

        with pytest.raises(Exception, match=TRIGGER_MESSAGE):
            await privacy.anonymize_user_data(
                db, db.dal, user_id=user_id, email="observedfail@example.com"
            )

        [span] = otel.spans.get_finished_spans()
        [total] = otel.points("waddles_hub_dsar_erasure_total")
        assert span.status.status_code == trace.StatusCode.ERROR
        assert span.status.description.startswith("type=")
        assert (total.value, dict(total.attributes)) == (1, {"result": "failed"})
        assert otel.points("waddles_hub_dsar_erasure_rows") == []  # nothing was deleted
        rendered = repr((span.status.description, span.events, dict(span.attributes)))
        assert TRIGGER_MESSAGE not in rendered
        assert "observedfail@example.com" not in rendered
        assert span.events == ()  # no record_exception: it would carry the raw message

    async def test_telemetry_failure_never_fails_the_erasure(
        self, db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken() -> Any:
            raise RuntimeError("exporter on fire")

        monkeypatch.setattr(privacy, "_instruments", broken)
        user_id = _user(db, name="telemetrybroken")
        _chat(db, user_id=user_id, community_id=1, text="bye")

        with caplog.at_level("ERROR", logger=privacy.logger.name):
            await privacy.anonymize_user_data(
                db, db.dal, user_id=user_id, email="telemetrybroken@example.com"
            )

        assert _footprint(db, user_id)["chat_messages"] == 0
        assert [r.status for r in _ledger(db, user_id)] == ["completed"]
        assert any("dsar.erasure_metrics_failed" in r.getMessage() for r in caplog.records)
