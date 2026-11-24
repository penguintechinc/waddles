"""DSAR verification mail is delivered OFF the request path (durable outbox + worker).

Real paths: the real `aiosmtpd` sink and `SmtpDsarNotifier` (reused from test_dsar_email_sender),
the real outbox table and worker. Only the remote mail server and the (instant) fake notifier used
for the timing/structure tests are fakes.
"""

# ruff: noqa: F811, F401, E501 - reused fixtures re-declared as args

from __future__ import annotations

import asyncio
import inspect
import logging
import statistics
import time
from datetime import timedelta
from typing import Any

import pytest

from blueprints.v1 import dsar_requests as dsar_module
from services import dsar_notifier, dsar_outbox, dsar_rate_limit
from services import dsar_service as svc
from tests.conftest import TENANT_SLUG
from tests.test_dsar_email_sender import (  # noqa: F401 - fixtures reused as-is
    RESEND,
    _body_text,
    _reasons,
    _token_of,
    make_sink,
    pki,
    smtp_env,
)
from tests.test_dsar_inbox import (  # noqa: F401
    PUB,
    FakeNotifier,
    _env,
    _events,
    _public_submit,
    _rows,
    app,
    client,
    db,
    flag_on,
    module_db,
    notifier,
    raw_client,
    solve_altcha,
)

E = svc.EventType
O = dsar_outbox.OutboxStatus  # noqa: E741


@pytest.fixture(autouse=True)
def _no_backoff_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry delay = the full-jitter UPPER bound, never a random draw near zero.

    Production backoff is `uniform(0, cap)`; a draw of ~0 makes the retried row due again at once, so
    `run_once` (which loops until nothing is due) re-claimed it inside the same pass and a test that
    expects ONE attempt per drain saw 2 (flaked under CPU load, ~1-2% of runs). Tests that want an
    immediate retry set `DSAR_OUTBOX_BACKOFF_SECONDS=0`, whose upper bound is 0.
    """
    monkeypatch.setattr(dsar_outbox._RNG, "uniform", lambda _low, high: high, raising=False)


EMAIL = "zelda.requester@example.org"


def _outbox(db_: Any) -> list[Any]:
    t = db_.dal.dsar_mail_outbox
    return list(db_.dal(t.id > 0).select(orderby=t.id))


async def _drain(app_: Any, db_: Any) -> int:
    return await dsar_outbox.run_once(db_, db_.dal, lambda: dsar_notifier.get_notifier(app_))


async def _resend(client_: Any, email: str = EMAIL) -> Any:
    return await client_.post(
        RESEND, json={"email": email, "requestType": 1, "captchaToken": solve_altcha()}
    )


class TestOffTheRequestPath:
    async def test_submit_returns_202_before_any_mail_then_worker_delivers(
        self, raw_client: Any, app: Any, db: Any, make_sink: Any, smtp_env: Any
    ) -> None:
        sink = make_sink()
        smtp_env(sink)
        assert (await _public_submit(raw_client, email=EMAIL)).status_code == 202
        # nothing sent yet: the route only queued a row (token sealed, no email copied in)
        assert sink.handler.messages == []
        [row] = _outbox(db)
        assert row.status == int(O.PENDING) and row.token_enc and EMAIL not in str(row.__dict__)
        worker = asyncio.create_task(
            dsar_outbox.run_worker_loop(db, db.dal, app=app, is_ready=lambda: True)
        )
        try:
            for _ in range(100):
                if sink.handler.messages and _outbox(db)[0].status == int(O.SENT):
                    db.dal.commit()  # fresh read view
                    if E.VERIFICATION_SENT in [E(e.event_type) for e in _events(db)]:
                        break
                await asyncio.sleep(0.05)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert len(sink.handler.messages) == 1
        assert sink.handler.messages[0].rcpt_tos == [EMAIL]
        [row] = _outbox(db)
        assert row.status == int(O.SENT) and row.token_enc is None and row.completed_at
        assert E.VERIFICATION_SENT in [E(e.event_type) for e in _events(db)]
        # the delivered token is the one stored on the request (and works once)
        token = _token_of(sink.mail())
        assert (await raw_client.post(f"{PUB}/verify", json={"token": token})).status_code == 200

    async def test_smtp_down_is_still_a_fast_202_and_failure_is_recorded_after_retries(
        self,
        raw_client: Any,
        app: Any,
        db: Any,
        smtp_env: Any,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        smtp_env(
            None,
            DSAR_SMTP_MAX_RETRIES="0",
            DSAR_OUTBOX_MAX_ATTEMPTS="3",
            DSAR_OUTBOX_BACKOFF_SECONDS="0",
        )
        started = time.monotonic()
        response = await _public_submit(raw_client, email=EMAIL)
        assert response.status_code == 202 and time.monotonic() - started < 1.5
        assert _reasons(db) == []  # no verdict yet
        for _ in range(3):  # three worker attempts, backoff 0 => each due immediately
            await _drain(app, db)
        assert _reasons(db) == ["connect"]
        [row] = _outbox(db)
        assert row.status == int(O.FAILED) and row.attempts == 3 and row.token_enc is None
        errors = [r for r in caplog.records if r.getMessage() == "dsar.verification_send_failed"]
        assert len(errors) == 1 and errors[0].levelno == logging.ERROR
        assert EMAIL not in caplog.text
        assert _rows(db)[0].status == int(svc.RequestStatus.VERIFICATION_PENDING)

    async def test_unconfigured_sender_fails_loud_without_retrying(
        self, raw_client: Any, app: Any, db: Any
    ) -> None:
        app.config.pop(dsar_notifier.CONFIG_KEY)
        assert (await _public_submit(raw_client, email=EMAIL)).status_code == 202
        await _drain(app, db)
        assert E.VERIFICATION_SEND_FAILED in [E(e.event_type) for e in _events(db)]
        assert _outbox(db)[0].status == int(O.FAILED) and _outbox(db)[0].attempts == 1


class TestRestartAndConcurrency:
    async def test_enqueue_then_restart_sends_exactly_once(
        self, raw_client: Any, app: Any, db: Any, make_sink: Any, smtp_env: Any
    ) -> None:
        sink = make_sink()
        smtp_env(sink)
        await _public_submit(raw_client, email=EMAIL)
        # "pod restart": all in-process state is gone; the row lives in the DB. A brand-new worker
        # pass (fresh notifier from env) finds and sends it, and a second pass sends nothing.
        assert await _drain(app, db) == 1
        assert await _drain(app, db) == 0
        assert len(sink.handler.messages) == 1

    async def test_crash_mid_send_is_reclaimed_after_the_lease_and_sent_once(
        self, raw_client: Any, app: Any, db: Any, make_sink: Any, smtp_env: Any
    ) -> None:
        sink = make_sink()
        smtp_env(sink)
        await _public_submit(raw_client, email=EMAIL)
        leased = await dsar_outbox.claim(db, db.dal)  # a worker takes the row, then dies
        assert len(leased) == 1
        assert await dsar_outbox.claim(db, db.dal) == []  # lease still held: nobody else may send
        t = db.dal.dsar_mail_outbox
        db.dal(t.id == leased[0].id).update(claimed_until=svc.now_utc() - timedelta(seconds=1))
        db.dal.commit()
        assert await _drain(app, db) == 1
        assert len(sink.handler.messages) == 1 and _outbox(db)[0].status == int(O.SENT)

    async def test_two_concurrent_workers_send_each_mail_once(
        self, raw_client: Any, app: Any, db: Any, make_sink: Any, smtp_env: Any
    ) -> None:
        sink = make_sink()
        smtp_env(sink)
        addresses = [f"person{i}@example.org" for i in range(6)]
        for address in addresses:
            assert (await _public_submit(raw_client, email=address)).status_code == 202
        handled = await asyncio.gather(*(_drain(app, db) for _ in range(2)))
        assert sum(handled) == 6
        got = sorted(env.rcpt_tos[0] for env in sink.handler.messages)
        assert got == sorted(addresses)
        assert [r.status for r in _outbox(db)] == [int(O.SENT)] * 6

    async def test_resend_before_first_send_supersedes_the_old_token(
        self, raw_client: Any, app: Any, db: Any, make_sink: Any, smtp_env: Any
    ) -> None:
        sink = make_sink()
        smtp_env(sink)
        await _public_submit(raw_client, email=EMAIL)
        assert (await _resend(raw_client)).status_code == 202
        await _drain(app, db)
        assert len(sink.handler.messages) == 1  # only the newest link is ever mailed
        statuses = sorted(r.status for r in _outbox(db))
        assert statuses == [int(O.SENT), int(O.DISCARDED)]
        assert E.VERIFICATION_RESENT in [E(e.event_type) for e in _events(db)]

    async def test_request_expired_before_send_is_discarded_not_mailed(
        self, raw_client: Any, app: Any, db: Any, make_sink: Any, smtp_env: Any
    ) -> None:
        sink = make_sink()
        smtp_env(sink)
        await _public_submit(raw_client, email=EMAIL)
        await svc.run_maintenance(db, db.dal, now=svc.now_utc() + timedelta(days=4))
        await _drain(app, db)
        assert sink.handler.messages == [] and _outbox(db)[0].status == int(O.DISCARDED)

    async def test_prune_drops_old_terminal_rows_only(
        self, raw_client: Any, app: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(raw_client, email=EMAIL)
        await _drain(app, db)
        t = db.dal.dsar_mail_outbox
        db.dal(t.id > 0).update(completed_at=svc.now_utc() - timedelta(days=30))
        db.dal.commit()
        await _public_submit(raw_client, email="two@example.org")  # still pending
        assert await dsar_outbox.prune(db, db.dal) == 1
        assert len(_outbox(db)) == 1


class TestMetrics:
    async def test_queue_depth_and_latency_are_recorded(
        self,
        raw_client: Any,
        app: Any,
        db: Any,
        notifier: FakeNotifier,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        samples: list[tuple[float, dict[str, str]]] = []
        monkeypatch.setattr(
            dsar_outbox.queue_latency, "record", lambda v, a=None: samples.append((v, a or {}))
        )
        for i in range(3):
            await _public_submit(raw_client, email=f"m{i}@example.org")
        assert await dsar_outbox.depth(db, db.dal) == 3 and dsar_outbox._depth_value == 3
        await _drain(app, db)
        assert dsar_outbox._depth_value == 0
        assert len(samples) == 3 and all(a == {"outcome": "sent"} and v >= 0 for v, a in samples)


class _Blocking:
    """A notifier that must never be awaited on the request path."""

    called = False

    async def send_verification(self, *, to_email: str, verify_url: str) -> None:
        _Blocking.called = True
        await asyncio.sleep(60)


class TestNoTimingOracle:
    async def test_routes_never_touch_the_notifier(
        self, raw_client: Any, app: Any, db: Any
    ) -> None:
        app.config[dsar_notifier.CONFIG_KEY] = _Blocking()
        _Blocking.called = False
        started = time.monotonic()
        assert (await _public_submit(raw_client, email=EMAIL)).status_code == 202
        assert (await _resend(raw_client)).status_code == 202  # pending => real enqueue
        assert (await _resend(raw_client, "nobody@example.org")).status_code == 202
        assert time.monotonic() - started < 5 and _Blocking.called is False
        source = inspect.getsource(dsar_module)
        assert "get_notifier" not in source and "send_verification(" not in source

    async def test_resend_latency_is_indistinguishable_with_and_without_a_pending_request(
        self, raw_client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _no_captcha(_token: str) -> None:
            return None

        monkeypatch.setattr(dsar_module, "_require_captcha", _no_captcha)
        await _public_submit(raw_client, email=EMAIL)  # one real pending request
        runs = 40
        with_pending: list[float] = []
        without: list[float] = []
        for i in range(runs):
            for email, bucket in ((EMAIL, with_pending), (f"none{i}@example.org", without)):
                dsar_rate_limit.reset_local()
                started = time.perf_counter()
                response = await raw_client.post(
                    RESEND, json={"email": email, "requestType": 1, "captchaToken": "x"}
                )
                bucket.append(time.perf_counter() - started)
                assert response.status_code == 202
        # Scheduler/CPU contention only ever ADDS latency, so the fastest observations are the
        # noise-free signal; a systematic oracle (extra work on one path) shifts the floor of that
        # path too. Comparing medians flaked under load (24ms vs 47ms on a busy runner).
        a, b = min(with_pending), min(without)
        assert abs(a - b) < 0.010, f"floor latency differs: {a * 1000:.2f}ms vs {b * 1000:.2f}ms"
        # and the pending path really enqueued while the other really did not
        by_status = [r.status for r in _outbox(db)]
        assert by_status.count(int(O.PENDING)) == 1 + runs  # real enqueues only
        assert by_status.count(int(O.DISCARDED)) == runs  # decoys: terminal, token already nulled
        assert all(r.token_enc is None for r in _outbox(db) if r.status == int(O.DISCARDED))


class _Raises:
    """A notifier that raises a configurable exception (optionally carrying PII in its text)."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def send_verification(self, *, to_email: str, verify_url: str) -> None:
        self.calls += 1
        raise self.exc


class TestLeaseToken:
    """regression: adversarial finding 1 - a stale worker must not clobber a re-claimed row."""

    async def test_stale_worker_cannot_finish_or_retry_a_row_another_worker_reclaimed(
        self, raw_client: Any, app: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(raw_client, email=EMAIL)
        stale = (await dsar_outbox.claim(db, db.dal))[0]
        t = db.dal.dsar_mail_outbox
        db.dal(t.id == stale.id).update(claimed_until=svc.now_utc() - timedelta(seconds=1))
        db.dal.commit()
        fresh = (await dsar_outbox.claim(db, db.dal))[0]  # lease expired -> second worker wins
        assert fresh.lease_token != stale.lease_token and fresh.attempts == 2
        # the stale worker's every state transition is refused
        assert await dsar_outbox._finish(db, db.dal, stale, O.SENT) is False
        assert await dsar_outbox._finish(db, db.dal, stale, O.FAILED, reason="x") is False
        assert await dsar_outbox._renew(db, db.dal, stale, 60) is False
        assert await dsar_outbox.process(db, db.dal, stale, notifier) == "lost"
        assert notifier.sent == []  # lost lease => no send at all
        row = _outbox(db)[0]
        assert row.status == int(O.PENDING) and row.lease_token == fresh.lease_token
        # the rightful owner still completes normally
        assert await dsar_outbox.process(db, db.dal, fresh, notifier) == "sent"
        assert len(notifier.sent) == 1 and _outbox(db)[0].status == int(O.SENT)

    async def test_slow_send_outlasting_its_lease_is_not_double_sent_or_overwritten(
        self, raw_client: Any, app: Any, db: Any
    ) -> None:
        release = asyncio.Event()
        sends: list[str] = []

        class _Slow:
            total_timeout = 30.0

            async def send_verification(self, *, to_email: str, verify_url: str) -> None:
                sends.append(to_email)
                await release.wait()

        await _public_submit(raw_client, email=EMAIL)
        slow = (await dsar_outbox.claim(db, db.dal))[0]
        task = asyncio.create_task(dsar_outbox.process(db, db.dal, slow, _Slow()))
        await asyncio.sleep(0.05)  # worker 1 is mid-send; its lease is renewed past the send
        t = db.dal.dsar_mail_outbox
        assert await dsar_outbox.claim(db, db.dal) == []  # renewed lease: nobody can take it
        db.dal(t.id == slow.id).update(claimed_until=svc.now_utc() - timedelta(seconds=1))
        db.dal.commit()  # force-expire (a worker that outlived even the renewed lease)
        other = (await dsar_outbox.claim(db, db.dal))[0]
        release.set()
        assert await task == "lost"  # worker 1 finishes sending but may not record anything
        row = _outbox(db)[0]
        assert row.status == int(O.PENDING) and row.lease_token == other.lease_token

    async def test_run_once_claims_one_row_per_iteration(
        self, raw_client: Any, app: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        for i in range(3):
            await _public_submit(raw_client, email=f"one{i}@example.org")
        assert len(await dsar_outbox.claim(db, db.dal)) == 1  # default limit is a single row
        db.dal(db.dal.dsar_mail_outbox.id > 0).update(claimed_until=None)
        db.dal.commit()
        assert await _drain(app, db) == 3


class TestFailureClassification:
    @pytest.mark.parametrize("exc", [TimeoutError("t"), TimeoutError(), ConnectionResetError("c")])
    async def test_timeouts_and_connection_errors_retry_not_fail(
        self, raw_client: Any, app: Any, db: Any, exc: BaseException
    ) -> None:
        await _public_submit(raw_client, email=EMAIL)
        app.config[dsar_notifier.CONFIG_KEY] = _Raises(exc)
        assert await _drain(app, db) == 1
        row = _outbox(db)[0]
        assert row.status == int(O.PENDING) and row.token_enc and row.claimed_until is None
        assert E.VERIFICATION_SEND_FAILED not in [E(e.event_type) for e in _events(db)]

    async def test_unknown_error_still_fails_permanently(
        self, raw_client: Any, app: Any, db: Any
    ) -> None:
        await _public_submit(raw_client, email=EMAIL)
        app.config[dsar_notifier.CONFIG_KEY] = _Raises(ValueError("boom"))
        await _drain(app, db)
        assert _outbox(db)[0].status == int(O.FAILED)


class TestExpiredTokenNotMailed:
    async def test_expired_verification_token_is_discarded_before_send(
        self, raw_client: Any, app: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(raw_client, email=EMAIL)
        r = db.dal.dsar_requests
        db.dal(r.id > 0).update(verification_expires_at=svc.now_utc() - timedelta(minutes=1))
        db.dal.commit()
        await _drain(app, db)
        row = _outbox(db)[0]
        assert notifier.sent == [] and row.status == int(O.DISCARDED)
        assert row.last_reason == "token_expired" and row.token_enc is None


class TestNoPiiInLogs:
    async def test_failure_logs_carry_class_and_reason_only(
        self, raw_client: Any, app: Any, db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        await _public_submit(raw_client, email=EMAIL)
        leak = f"550 rejected {EMAIL} token=SECRETTOKEN123"
        app.config[dsar_notifier.CONFIG_KEY] = _Raises(RuntimeError(leak))
        with caplog.at_level(logging.DEBUG):
            await _drain(app, db)
        text = "\n".join(
            f"{r.getMessage()} {r.__dict__} {r.exc_info} {r.exc_text}" for r in caplog.records
        )
        assert "dsar.verification_send_failed" in text and "RuntimeError" in text
        assert EMAIL not in text and "SECRETTOKEN123" not in text and "zelda" not in text
        assert all(r.exc_info is None for r in caplog.records)


class TestSentUnconfirmed:
    async def test_db_failure_after_smtp_accept_is_surfaced_and_event_written_first(
        self,
        raw_client: Any,
        app: Any,
        db: Any,
        notifier: FakeNotifier,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _public_submit(raw_client, email=EMAIL)

        async def _boom(*_a: Any, **_k: Any) -> bool:
            raise RuntimeError("db down")

        monkeypatch.setattr(dsar_outbox, "_finish", _boom)
        with caplog.at_level(logging.ERROR):
            assert await _drain(app, db) == 1
        assert len(notifier.sent) == 1  # the mail did leave
        assert "dsar.outbox.sent_unconfirmed" in [r.getMessage() for r in caplog.records]
        assert E.VERIFICATION_SENT in [E(e.event_type) for e in _events(db)]  # audit, written first
        assert _outbox(db)[0].status == int(O.PENDING)  # re-sent after lease: at-least-once


class TestTransactionVariant:
    async def test_enqueue_in_transaction_does_not_commit_and_rolls_back_with_the_caller(
        self, raw_client: Any, app: Any, db: Any
    ) -> None:
        await _public_submit(raw_client, email=EMAIL)
        request = db.dal(db.dal.dsar_requests.id > 0).select().first()
        before = len(_outbox(db))

        def _work() -> None:
            dsar_outbox.enqueue_in_transaction(
                db.dal, request_row=request, token="tok", resend=True, tenant_slug=TENANT_SLUG
            )
            raise RuntimeError("request write failed")

        with pytest.raises(RuntimeError):
            await dsar_outbox_run_in_transaction(db, db.dal, _work)
        assert len(_outbox(db)) == before  # mail row rolled back with the failed unit

        def _ok() -> str:
            return dsar_outbox.enqueue_in_transaction(
                db.dal, request_row=request, token="tok", resend=False, tenant_slug=TENANT_SLUG
            )

        public_id = await dsar_outbox_run_in_transaction(db, db.dal, _ok)
        assert len(_outbox(db)) == before + 1 and _outbox(db)[-1].public_id == public_id


async def dsar_outbox_run_in_transaction(async_dal: Any, dal: Any, work: Any) -> Any:
    """Thin alias so the test reads like the documented usage."""
    from services.ban_suppression import run_in_transaction

    return await run_in_transaction(async_dal, dal, work)


class TestOutboxEnvIsFailLoud:
    """A set-but-malformed tunable raises (naming the variable) instead of silently defaulting."""

    def test_malformed_poll_seconds_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DSAR_OUTBOX_POLL_SECONDS", "fast")
        with pytest.raises(ValueError, match="DSAR_OUTBOX_POLL_SECONDS"):
            dsar_outbox.poll_seconds()

    def test_unset_uses_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DSAR_OUTBOX_POLL_SECONDS", raising=False)
        assert dsar_outbox.poll_seconds() == 5.0
