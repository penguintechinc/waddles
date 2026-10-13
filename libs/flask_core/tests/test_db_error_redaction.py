"""DB driver error log redaction -- SECURITY (PII/secrets in logs).

# regression: AsyncDAL.executesql_async() (and every sibling in flask_core)
# logged ``"ExecuteSQL error: {e}"`` at ERROR, where ``{e}`` is the raw
# driver message. psycopg2 / pydal / SQLAlchemy messages embed the BOUND
# VALUES of the failed statement (``DETAIL: Key (email)=(a@b.c) already
# exists``, invalid-input-syntax echoes, inlined INSERT text, SQLAlchemy's
# ``[parameters: (...)]``), so PII, tokens and handles reached every
# consuming service's log stream.

Every test drives a driver-shaped error whose message AND diagnostic detail
embed `SENTINEL` (a known secret) and asserts the sentinel is absent from
everything the logging system emitted -- both the rendered message and the
fully formatted record (so a traceback / `exc_info` rendering the exception
text would also fail the test) -- while the operation, exception type and
SQLSTATE stay present, i.e. the log line remains actionable.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import types
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "flask_core"
if "flask_core" not in sys.modules:
    _stub = types.ModuleType("flask_core")
    _stub.__path__ = [str(_PKG_DIR)]
    sys.modules["flask_core"] = _stub

import psycopg2.errors  # noqa: E402
import sqlalchemy.exc  # noqa: E402

from flask_core.api_utils import async_endpoint  # noqa: E402
from flask_core.database import AsyncDAL, db_operation, install_db_resilience  # noqa: E402
from flask_core.db_errors import (  # noqa: E402
    describe_db_error,
    format_sanitized_traceback,
    is_db_driver_error,
    log_db_error,
    summarize_db_error,
)
from flask_core.read_replica import (  # noqa: E402
    ReadReplicaManager,
    ReadReplicaRouter,
    ReplicaConfig,
    ReplicaStatus,
)
from flask_core.sharding import ChannelShardManager  # noqa: E402

SENTINEL = "SENTINEL-s3cr3t-tok-9f2c1a7e"
PII_EMAIL = "victim.person@example.invalid"

_DETAIL = f"Key (email)=({SENTINEL}) already exists."
_MESSAGE = (
    'duplicate key value violates unique constraint "users_email_key"\n'
    f"DETAIL:  {_DETAIL}\n"
    f'invalid input syntax for type uuid: "{PII_EMAIL}"'
)


class FakePgError(Exception):
    """Driver-shaped error: duck-types psycopg2 (`pgcode` + `diag`), embeds the sentinel."""

    def __init__(self, message: str = _MESSAGE, pgcode: str = "23505") -> None:
        super().__init__(message)
        self.pgcode = pgcode
        self.diag = SimpleNamespace(
            sqlstate=pgcode,
            constraint_name="users_email_key",
            table_name="users",
            column_name="email",
            message_primary=message,
            message_detail=_DETAIL,
        )


class RealPsycopg2UniqueViolation(psycopg2.errors.UniqueViolation):
    """A genuine psycopg2 exception class (so the module-prefix path is exercised)."""

    pgcode = "23505"  # type: ignore[assignment]  # psycopg2 sets this from a live result; fixed for the test
    diag = SimpleNamespace(  # type: ignore[assignment]  # same: normally built from the server response
        constraint_name="users_email_key",
        table_name="users",
        column_name="email",
        message_detail=_DETAIL,
    )


def _assert_redacted(caplog: pytest.LogCaptureFixture) -> None:
    """Assert nothing captured -- message, args, formatted record, exc text -- holds a secret."""
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    rendered = [formatter.format(r) for r in caplog.records]
    haystack = "\n".join([caplog.text, *rendered, *(r.getMessage() for r in caplog.records)])
    haystack += "\n".join(repr(r.args) for r in caplog.records)
    assert caplog.records, "expected log records, found none -- the check proved nothing"
    for secret in (SENTINEL, PII_EMAIL, "DETAIL", "duplicate key value"):
        assert secret not in haystack, f"leaked {secret!r} into logs:\n{haystack}"
    assert all(r.exc_info is None or not _exc_text_leaks(r) for r in caplog.records)


def _exc_text_leaks(record: logging.LogRecord) -> bool:
    """True if the record carries an exception whose text would render the sentinel."""
    exc_info = record.exc_info
    if not isinstance(exc_info, tuple) or exc_info[1] is None:  # None / False: no exception
        return False
    return SENTINEL in str(exc_info[1])


@pytest.fixture
async def async_dal(tmp_path: Path) -> AsyncDAL:
    """A real `AsyncDAL` (sqlite file) whose `.dal` tests swap for a MagicMock."""
    dal = AsyncDAL("sqlite://redact.db", folder=str(tmp_path), pool_size=1, migrate=True)
    dal.define_table("widgets", dal.Field("name"))
    return dal


class TestDbErrorsModule:
    """`flask_core.db_errors` -- allowlist-only description of driver errors."""

    def test_render_has_type_sqlstate_category_and_schema_identifiers_only(self) -> None:
        text = describe_db_error(FakePgError())

        assert "type=" in text and "FakePgError" in text
        assert "sqlstate=23505" in text
        assert "category=unique_violation" in text
        assert "constraint=users_email_key" in text
        assert "table=users" in text and "column=email" in text
        assert SENTINEL not in text and PII_EMAIL not in text and "DETAIL" not in text

    def test_real_psycopg2_error_class_is_detected_and_redacted(self) -> None:
        err = RealPsycopg2UniqueViolation(_MESSAGE)

        assert is_db_driver_error(err)
        text = describe_db_error(err)
        assert "psycopg2.errors.UniqueViolation" in text or "RealPsycopg2UniqueViolation" in text
        assert "sqlstate=23505" in text
        assert SENTINEL not in text and PII_EMAIL not in text

    def test_real_sqlite_driver_error_reports_error_name_not_message(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (name TEXT UNIQUE)")
        conn.execute("INSERT INTO t VALUES (?)", (SENTINEL,))
        with pytest.raises(sqlite3.IntegrityError) as info:
            conn.execute("INSERT INTO t VALUES (?)", (SENTINEL,))

        text = describe_db_error(info.value)

        assert "sqlite3.IntegrityError" in text
        assert "code=SQLITE_CONSTRAINT_UNIQUE" in text
        assert "UNIQUE constraint failed" not in text  # the driver message is never emitted

    def test_sqlalchemy_wrapper_params_suffix_never_emitted(self) -> None:
        wrapped = sqlalchemy.exc.IntegrityError(
            "INSERT INTO users (email) VALUES (%(email)s)",
            {"email": SENTINEL},
            FakePgError(),
        )
        assert SENTINEL in str(wrapped), "precondition: SQLAlchemy's own str() embeds params"

        text = describe_db_error(wrapped)

        assert "sqlalchemy.exc.IntegrityError" in text
        assert "sqlstate=23505" in text  # found via `.orig`
        assert SENTINEL not in text and "INSERT INTO" not in text

    def test_cause_chain_is_walked_for_sqlstate(self) -> None:
        try:
            try:
                raise FakePgError(pgcode="40P01")
            except FakePgError as inner:
                raise RuntimeError(f"wrapped {SENTINEL}") from inner
        except RuntimeError as outer:
            assert is_db_driver_error(outer)
            text = describe_db_error(outer)

        assert "type=RuntimeError" in text and "sqlstate=40P01" in text
        assert "category=deadlock_detected" in text
        assert SENTINEL not in text

    def test_mysql_driver_reports_errno_not_message(self) -> None:
        mysql_error = type("IntegrityError", (Exception,), {"__module__": "pymysql.err"})(
            1062, f"Duplicate entry '{SENTINEL}' for key 'users.email'"
        )

        assert is_db_driver_error(mysql_error)
        text = describe_db_error(mysql_error)

        assert text == "type=pymysql.err.IntegrityError code=mysql-1062"
        assert SENTINEL not in text

    def test_exception_group_members_are_inspected(self) -> None:
        group = ExceptionGroup("batch", [ValueError("x"), FakePgError(pgcode="23503")])

        assert is_db_driver_error(group)
        text = describe_db_error(group)
        assert "sqlstate=23503" in text and "category=foreign_key_violation" in text
        assert SENTINEL not in text

    def test_hostile_driver_attribute_cannot_break_logging(self) -> None:
        class Hostile(Exception):
            @property
            def diag(self) -> object:
                raise RuntimeError(f"explodes with {SENTINEL}")

        text = describe_db_error(Hostile("boom"))

        assert text.startswith("type=") and "Hostile" in text and SENTINEL not in text
        assert "sqlstate" not in text

    def test_unknown_sqlstate_falls_back_to_class_label(self) -> None:
        assert "category=data_exception" in describe_db_error(FakePgError(pgcode="22ZZZ"))

    @pytest.mark.parametrize("bad", ["not a code", "23505; DROP", SENTINEL, "2350", ""])
    def test_malformed_sqlstate_is_dropped_fail_closed(self, bad: str) -> None:
        err = FakePgError()
        err.pgcode = bad
        err.diag.sqlstate = bad

        summary = summarize_db_error(err)

        assert summary.sqlstate is None and summary.category is None
        assert SENTINEL not in summary.render()

    def test_non_identifier_schema_names_are_dropped(self) -> None:
        err = FakePgError()
        err.diag.constraint_name = f"uq {SENTINEL}"
        err.diag.table_name = f"users'; --{SENTINEL}"
        err.diag.column_name = PII_EMAIL

        text = describe_db_error(err)

        assert "constraint=" not in text and "table=" not in text and "column=" not in text
        assert SENTINEL not in text and PII_EMAIL not in text

    def test_plain_exception_is_not_a_driver_error_and_message_is_not_read(self) -> None:
        err = ValueError(SENTINEL)

        assert not is_db_driver_error(err)
        assert describe_db_error(err) == "type=ValueError"

    def test_exception_cycle_does_not_loop(self) -> None:
        a, b = RuntimeError("a"), RuntimeError("b")
        a.__context__, b.__context__ = b, a

        assert describe_db_error(a) == "type=RuntimeError"

    def test_sanitized_traceback_has_frames_but_no_exception_text(self) -> None:
        def explode() -> None:
            raise FakePgError()

        with pytest.raises(FakePgError) as info:
            explode()

        text = format_sanitized_traceback(info.value)

        assert "Traceback (most recent call last):" in text
        assert "in explode" in text and "test_db_error_redaction.py" in text
        assert "sqlstate=23505" in text
        assert SENTINEL not in text and PII_EMAIL not in text and "DETAIL" not in text

    def test_log_db_error_emits_error_line_and_sanitized_debug_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = logging.getLogger("redaction.unit")
        with caplog.at_level(logging.DEBUG, logger="redaction.unit"):
            log_db_error(log, "Widget op error", FakePgError())

        by_level = {r.levelno: r.getMessage() for r in caplog.records}
        assert by_level[logging.ERROR].startswith("Widget op error: type=")
        assert "sqlstate=23505" in by_level[logging.ERROR]
        assert "sanitized traceback" in by_level[logging.DEBUG]
        _assert_redacted(caplog)

    def test_log_db_error_skips_traceback_work_when_debug_disabled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        log = logging.getLogger("redaction.unit.quiet")
        with caplog.at_level(logging.INFO, logger="redaction.unit.quiet"):
            log_db_error(log, "Widget op error", FakePgError())

        assert [r.levelno for r in caplog.records] == [logging.ERROR]


class TestAsyncDalNeverLogsDriverMessage:
    """Every `AsyncDAL` operation logs sanitized text -- never `{e}`."""

    async def test_executesql_async_regression_sentinel_never_logged(
        self, async_dal: AsyncDAL, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The reported leak: executesql_async with a bound-value-bearing driver error."""
        mock_dal = MagicMock()
        mock_dal.executesql.side_effect = FakePgError()
        async_dal.dal = mock_dal

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(FakePgError):  # still re-raised untouched for the caller
                await async_dal.executesql_async(
                    "INSERT INTO users (email) VALUES (%s)", [SENTINEL]
                )

        mock_dal.rollback.assert_called_once()
        error = next(r for r in caplog.records if r.levelno == logging.ERROR)
        assert error.getMessage().startswith("ExecuteSQL error: type=")
        assert "sqlstate=23505" in error.getMessage()
        assert "category=unique_violation" in error.getMessage()
        _assert_redacted(caplog)

    async def test_real_sqlite_failure_through_executesql_async_logs_type_and_code(
        self, async_dal: AsyncDAL, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Real driver path (no mocks): a genuine sqlite3 failure is still actionable."""
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(Exception):  # noqa: B017,PT011 -- driver exception type varies by version
                await async_dal.executesql_async(
                    "INSERT INTO no_such_table (name) VALUES (?)", [SENTINEL]
                )

        error = next(r for r in caplog.records if r.levelno == logging.ERROR)
        assert error.getMessage().startswith("ExecuteSQL error: type=")
        assert "sqlite3.OperationalError" in error.getMessage()
        assert "code=SQLITE_ERROR" in error.getMessage()
        _assert_redacted(caplog)

    @pytest.mark.parametrize(
        ("label", "call"),
        [
            (
                "Select error",
                lambda d: d.select_async(_failing_query()),
            ),
            (
                "Insert error",
                lambda d: d.insert_async(_failing_table("insert"), name=SENTINEL),
            ),
            (
                "Update error",
                lambda d: d.update_async(object(), name=SENTINEL),
            ),
            (
                "Delete error",
                lambda d: d.delete_async(object()),
            ),
            (
                "Count error",
                lambda d: d.count_async(object()),
            ),
            (
                "Execute error",
                lambda d: d.execute("SELECT * FROM users WHERE email = $1", [SENTINEL]),
            ),
            (
                "Bulk insert error",
                lambda d: d.bulk_insert_async(_failing_table("bulk_insert"), [{"email": SENTINEL}]),
            ),
        ],
        ids=["select", "insert", "update", "delete", "count", "execute", "bulk_insert"],
    )
    async def test_every_operation_logs_sanitized_error(
        self,
        async_dal: AsyncDAL,
        caplog: pytest.LogCaptureFixture,
        label: str,
        call: Callable[[AsyncDAL], Awaitable[Any]],
    ) -> None:
        mock_dal = MagicMock()
        failing_set = MagicMock()
        failing_set.update.side_effect = FakePgError()
        failing_set.delete.side_effect = FakePgError()
        failing_set.count.side_effect = FakePgError()
        mock_dal.return_value = failing_set
        mock_dal._adapter.execute.side_effect = FakePgError()
        async_dal.dal = mock_dal

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(FakePgError):
                await call(async_dal)

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert errors[0].startswith(f"{label}: type=")
        assert "sqlstate=23505" in errors[0] and "category=unique_violation" in errors[0]
        _assert_redacted(caplog)

    async def test_transaction_async_rollback_log_is_sanitized(
        self, async_dal: AsyncDAL, caplog: pytest.LogCaptureFixture
    ) -> None:
        async_dal.dal = MagicMock()

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(FakePgError):
                async with async_dal.transaction_async():
                    raise FakePgError()

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and errors[0].startswith("Transaction rolled back: type=")
        _assert_redacted(caplog)


def _failing_query() -> MagicMock:
    """A query whose `.select()` raises the sentinel-bearing driver error."""
    query = MagicMock()
    query.select.side_effect = FakePgError()
    return query


def _failing_table(method: str) -> MagicMock:
    """A table whose `method` raises the sentinel-bearing driver error."""
    table = MagicMock()
    getattr(table, method).side_effect = FakePgError()
    return table


class TestDbOperationAndTeardown:
    """`db_operation()` and `install_db_resilience()` log sanitized, still re-raise."""

    def test_db_operation_driver_error_logs_type_sqlstate_operation_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        dal = MagicMock()

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(FakePgError):
                with db_operation(dal, "resolve_tenant_context:tenants.select"):
                    raise FakePgError()

        dal.rollback.assert_called_once()
        error = next(r for r in caplog.records if r.levelno == logging.ERROR).getMessage()
        assert "resolve_tenant_context:tenants.select" in error
        assert "FakePgError" in error and "sqlstate=23505" in error
        _assert_redacted(caplog)

    def test_db_operation_fails_closed_for_non_driver_exceptions(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """pydal casts bound values before the driver sees them, so even its own
        ValueError/TypeError text can echo a value -- never log it inside a DB guard."""
        dal = MagicMock()

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ValueError):
                with db_operation(dal, "widgets.insert"):
                    raise ValueError(f"invalid literal for int(): {SENTINEL!r}")

        error = next(r for r in caplog.records if r.levelno == logging.ERROR).getMessage()
        assert "widgets.insert" in error and "type=ValueError" in error
        _assert_redacted(caplog)

    def test_rollback_failure_log_is_sanitized_and_original_error_propagates(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`dal.rollback()` raising a value-bearing error must not leak either."""
        dal = MagicMock()
        dal.rollback.side_effect = FakePgError()

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(KeyError):
                with db_operation(dal, "widgets.select"):
                    raise KeyError("original")

        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            m.startswith("dal.rollback() itself failed after 'widgets.select': type=")
            for m in messages
        )
        _assert_redacted(caplog)

    def _make_app(self, dal: Any) -> Any:
        from quart import Quart

        app = Quart(__name__)
        app.config["dal"] = dal
        install_db_resilience(app)

        @app.route("/driver-boom")
        async def driver_boom() -> str:
            raise FakePgError()

        @app.route("/plain-boom")
        async def plain_boom() -> str:
            raise RuntimeError("plain failure stays fully visible")

        return app

    async def test_teardown_driver_error_is_sanitized(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        dal = MagicMock()
        app = self._make_app(dal)
        app.testing = True  # propagate exc to teardown_request (see test_database.py)

        with caplog.at_level(logging.DEBUG):
            async with app.test_app() as running:
                response = await running.test_client().get("/driver-boom")

        assert response.status_code == 500
        dal.rollback.assert_called_once()
        messages = [r.getMessage() for r in caplog.records if r.name == "flask_core.database"]
        assert any(
            "Request failed -- rolling back shared DAL connection: type=" in m for m in messages
        )
        # Only assert on flask_core.database's own records: Quart's own
        # "Exception on request" logger is outside flask_core (see README).
        db_records = [r for r in caplog.records if r.name == "flask_core.database"]
        haystack = "\n".join(logging.Formatter("%(message)s").format(r) for r in db_records)
        assert SENTINEL not in haystack and PII_EMAIL not in haystack and "DETAIL" not in haystack

    async def test_teardown_non_driver_error_keeps_full_detail(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Redaction is scoped to DB driver errors -- other failures stay diagnosable."""
        dal = MagicMock()
        app = self._make_app(dal)
        app.testing = True

        with caplog.at_level(logging.ERROR):
            async with app.test_app() as running:
                await running.test_client().get("/plain-boom")

        assert any(
            "plain failure stays fully visible" in r.getMessage()
            for r in caplog.records
            if r.name == "flask_core.database"
        )

    async def test_teardown_commit_failure_is_sanitized(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        dal = MagicMock()
        dal.commit.side_effect = FakePgError(pgcode="40001")
        app = self._make_app(dal)

        with caplog.at_level(logging.DEBUG, logger="flask_core.database"):
            async with app.test_app() as running:
                await running.test_client().get("/driver-boom")

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            m.startswith("dal.commit() failed at request teardown -- rolling back: type=")
            and "sqlstate=40001" in m
            and "category=serialization_failure" in m
            for m in errors
        )
        dal.rollback.assert_called_once()
        db_records = [r for r in caplog.records if r.name == "flask_core.database"]
        haystack = "\n".join(logging.Formatter("%(message)s").format(r) for r in db_records)
        assert SENTINEL not in haystack and PII_EMAIL not in haystack and "DETAIL" not in haystack


class TestOtherFlaskCoreDbErrorSites:
    """Every other flask_core site that logs a DB-layer failure is sanitized too."""

    async def test_async_endpoint_driver_error_logs_sanitized_and_returns_generic_500(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from quart import Quart

        @async_endpoint
        async def handler() -> str:
            raise FakePgError()

        app = Quart(__name__)
        with caplog.at_level(logging.DEBUG, logger="flask_core.api_utils"):
            async with app.test_request_context("/"):
                response, status = await handler()

        assert status == 500
        assert SENTINEL not in (await response.get_data(as_text=True))
        error = next(r for r in caplog.records if r.levelno == logging.ERROR).getMessage()
        assert error.startswith("Request to handler failed with exception: type=")
        assert "sqlstate=23505" in error
        _assert_redacted(caplog)

    async def test_async_endpoint_non_driver_error_keeps_full_detail(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from quart import Quart

        @async_endpoint
        async def handler() -> str:
            raise RuntimeError("ordinary failure detail")

        app = Quart(__name__)
        with caplog.at_level(logging.ERROR, logger="flask_core.api_utils"):
            async with app.test_request_context("/"):
                _, status = await handler()

        assert status == 500
        record = next(r for r in caplog.records if r.levelno == logging.ERROR)
        assert "ordinary failure detail" in record.getMessage()
        assert record.exc_info is not None

    async def test_read_replica_router_select_failure_is_sanitized_and_retries_primary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = MagicMock()
        manager.primary_uri = "postgres://primary"
        manager.get_read_uri.return_value = "postgres://replica"
        replica = MagicMock(uri="postgres://replica")
        replica.select_async = AsyncMock(side_effect=FakePgError())
        primary = MagicMock()
        primary.select_async = AsyncMock(return_value="rows")
        router = ReadReplicaRouter(manager, primary, [replica])

        with caplog.at_level(logging.DEBUG):
            result = await router.select_async(MagicMock())

        assert result == "rows"
        error = next(r for r in caplog.records if r.levelno == logging.ERROR).getMessage()
        assert error.startswith("Replica select failed, retrying on primary: type=")
        _assert_redacted(caplog)

    async def test_read_replica_router_count_failure_is_sanitized(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = MagicMock()
        manager.primary_uri = "postgres://primary"
        manager.get_read_uri.return_value = "postgres://replica"
        replica = MagicMock(uri="postgres://replica")
        replica.count_async = AsyncMock(side_effect=FakePgError())
        primary = MagicMock()
        primary.count_async = AsyncMock(return_value=3)
        router = ReadReplicaRouter(manager, primary, [replica])

        with caplog.at_level(logging.DEBUG):
            assert await router.count_async(MagicMock()) == 3

        assert any(
            r.getMessage().startswith("Replica count failed, retrying on primary: type=")
            for r in caplog.records
        )
        _assert_redacted(caplog)

    def test_replica_connection_string_parse_error_does_not_echo_uri_fragments(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """urlparse's `.port` ValueError echoes the offending port text -- which is
        wherever a malformed credential/URI fragment ends up."""
        manager = ReadReplicaManager(primary_uri="postgres://primary:5432/db")

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ValueError):
                manager._parse_connection_string(f"postgres://u:p@host:{SENTINEL}/db")

        error = next(r for r in caplog.records if r.levelno == logging.ERROR).getMessage()
        assert error.startswith("Failed to parse connection string: type=ValueError")
        _assert_redacted(caplog)

    async def test_replica_health_check_connect_failure_is_sanitized(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(**_: Any) -> None:
            raise FakePgError(pgcode="28P01")

        monkeypatch.setattr("psycopg2.connect", boom)
        config = ReplicaConfig(host="replica.invalid", port=5432)
        manager = ReadReplicaManager(
            primary_uri="postgres://primary:5432/db", replica_configs=[config]
        )
        replica_id = next(iter(manager.replicas))

        with caplog.at_level(logging.DEBUG):
            await manager._check_replica_health(replica_id, config)

        assert manager.metrics[replica_id].status is ReplicaStatus.UNHEALTHY
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            m.startswith(f"Health check failed for {replica_id}: type=")
            and "category=invalid_password" in m
            for m in errors
        )
        _assert_redacted(caplog)

    async def test_channel_shard_get_channels_failure_is_sanitized(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        dal = MagicMock()
        dal.executesql.side_effect = FakePgError()
        manager = ChannelShardManager(dal, MagicMock(), pod_id="pod-0", total_pods=2)

        with caplog.at_level(logging.DEBUG):
            assert await manager.get_my_channels(SENTINEL) == []

        error = next(r for r in caplog.records if r.levelno == logging.ERROR).getMessage()
        assert error.startswith("Failed to get channels: type=")
        _assert_redacted(caplog)
