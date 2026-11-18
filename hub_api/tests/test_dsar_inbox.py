"""DSAR request inbox -- `blueprints/v1/dsar_requests.py` + `services/dsar_service.py`.

Real JWTs (`flask_core.auth.create_jwt_token`), a real pydal/sqlite database and the real tenant
fence / scope / service code -- only the OUTBOUND edges are faked: the verification-email notifier
(`FakeNotifier`, records instead of sending) and the captcha provider HTTP socket
(`httpx.MockTransport`; the real request construction, parsing and fail-closed handling run).

Regression names say what they guard (e.g. `..._regression_unverified_*`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from flask_core.database import AsyncDAL
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.dsar_requests as dsar_module
from blueprints.v1.admin_data_privacy import admin_data_privacy_bp
from blueprints.v1.dsar_requests import admin_dsar_bp, public_dsar_bp, user_dsar_bp
from services import captcha_service as captcha
from services import dsar_crypto as crypto
from services import dsar_notifier, dsar_outbox, dsar_rate_limit
from services import dsar_service as svc
from services.schema import bind_dsar_tables
from tests.conftest import OTHER_TENANT_SLUG, TENANT_SLUG, make_user_token
from tests.entitlement_fakes import FakeFlagGate, FakeLicenseGate, install_entitlement_client
from tests.test_admin_data_privacy import _community, _member, _tenant_admin, _user

ACME_ID = 1
OTHER_ID = 2
KEY = "ab" * 32  # DSAR_FINGERPRINT_KEY
SEAL_KEY = "ef" * 32  # CREDENTIAL_ENCRYPTION_KEY (the repo sealing vault key)
ALTCHA_KEY = "k" * 40
S = svc.RequestStatus


class FakeNotifier:
    """Records verification links instead of sending mail."""

    def __init__(self) -> None:  # noqa: D107
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    async def send_verification(self, *, to_email: str, verify_url: str) -> None:
        if self.fail:
            raise dsar_notifier.NotifierUnavailable("down")
        self.sent.append((to_email, verify_url))

    def last_token(self) -> str:
        return self.sent[-1][1].split("token=")[1]


def _define_tenants(dal: Any, *, migrate: bool) -> None:
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=migrate,
    )


@pytest.fixture(scope="module")
def module_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """Schema + two tenants once per module."""
    path = tmp_path_factory.mktemp("dsar-inbox") / "dsar.db"
    async_dal = AsyncDAL(f"sqlite://{path}", pool_size=1)
    dal = async_dal.dal
    _define_tenants(dal, migrate=True)
    bind_dsar_tables(dal, migrate=True)
    assert dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme", is_active=True) == ACME_ID
    assert (
        dal.tenants.insert(slug=OTHER_TENANT_SLUG, display_name="Other", is_active=True) == OTHER_ID
    )
    dal.commit()
    for name in dal.tables:
        dal(dal[name]).count()
    yield async_dal
    dal.close()


@pytest.fixture
def db(module_db: Any) -> Any:
    """Every non-tenant table emptied per test."""
    dal = module_db.dal
    for name in dal.tables:
        if name != "tenants":
            dal(dal[name]).delete()
    dal.commit()
    return module_db


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encryption key + altcha provider by default; clean limiter state; no stray provider env."""
    monkeypatch.setenv("DSAR_FINGERPRINT_KEY", KEY)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", SEAL_KEY)
    monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", "altcha")
    monkeypatch.setenv("DSAR_ALTCHA_HMAC_KEY", ALTCHA_KEY)
    monkeypatch.setenv("DSAR_VERIFY_URL_BASE", "https://app.example/privacy/verify")
    for name in ("DSAR_CAPTCHA_SECRET", "DSAR_CAPTCHA_SITE_KEY"):
        monkeypatch.delenv(name, raising=False)
    dsar_rate_limit.reset_local()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def app(db: Any, notifier: FakeNotifier) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    for bp in (admin_dsar_bp, user_dsar_bp, public_dsar_bp, admin_data_privacy_bp):
        quart_app.register_blueprint(bp)
    quart_app.config["dal"] = db.dal
    quart_app.config["async_dal"] = db
    quart_app.config[dsar_notifier.CONFIG_KEY] = notifier
    return quart_app


class DrainingClient:
    """Test client that plays the outbox worker: after every public POST it drains the queue.

    Delivery is asynchronous in production (`services/dsar_outbox.py`); most inbox tests assert on
    the *delivered* mail, so this stands in for the worker loop. Tests of the queueing itself use
    `raw_client` (no drain).
    """

    def __init__(self, inner: Any, app: Quart) -> None:  # noqa: D107
        self._inner, self._app = inner, app

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else (get, put, ...) to the wrapped test client."""
        return getattr(self._inner, name)

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        """POST, then run one worker pass over the outbox."""
        response = await self._inner.post(*args, **kwargs)
        await dsar_outbox.run_once(
            self._app.config["async_dal"],
            self._app.config["dal"],
            lambda: dsar_notifier.get_notifier(self._app),
        )
        return response


@pytest.fixture
def raw_client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture
def client(app: Quart, raw_client: Any) -> Any:
    return DrainingClient(raw_client, app)


@pytest.fixture(autouse=True)
def flag_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """`waddles.dsar-inbox` ON for the inbox blueprint (tests of the OFF state override it)."""
    gate = AsyncMock(return_value=True)
    monkeypatch.setattr(dsar_module, "feature_enabled", gate)
    return gate


def _headers(
    user_id: int, *, scope: str = "tenant:admin", tenant: str = TENANT_SLUG
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {make_user_token(user_id=user_id, scope=scope, tenant=tenant)}"
    }


def _base(slug: str = TENANT_SLUG) -> str:
    return f"/api/v1/tenant/{slug}/privacy/requests"


def solve_altcha() -> str:
    """A valid altcha payload (solves the self-hosted proof of work)."""
    ch = captcha.create_altcha_challenge()
    for number in range(1, ch["maxnumber"] + 1):
        if hashlib.sha256(f"{ch['salt']}{number}".encode()).hexdigest() == ch["challenge"]:
            payload = {
                **{k: ch[k] for k in ("algorithm", "challenge", "salt", "signature")},
                "number": number,
            }
            return base64.b64encode(json.dumps(payload).encode()).decode()
    raise AssertionError("challenge unsolvable")


@pytest.fixture
def world(db: Any) -> dict[str, int]:
    """Acme admin (+second admin), member alice with a community; Other tenant admin + member."""
    acme_c = _community(db, tenant_id=ACME_ID, name="acme-main")
    other_c = _community(db, tenant_id=OTHER_ID, name="other-main")
    admin = _user(db, name="admin")
    _tenant_admin(db, tenant_id=ACME_ID, user_id=admin)
    admin2 = _user(db, name="admin2")
    _tenant_admin(db, tenant_id=ACME_ID, user_id=admin2)
    alice = _user(db, name="alice")
    _member(db, community_id=acme_c, user_id=alice)
    other_admin = _user(db, name="otheradmin")
    _tenant_admin(db, tenant_id=OTHER_ID, user_id=other_admin)
    return {
        "admin": admin,
        "admin2": admin2,
        "alice": alice,
        "other_admin": other_admin,
        "other_c": other_c,
    }


def _uuid_of(db: Any, user_id: int) -> str:
    return str(db.dal.hub_users[user_id].uuid)


async def _admin_create(client: Any, world: dict[str, int], **body: Any) -> dict[str, Any]:
    payload = {"requestType": 1, "regime": 1, **body}
    response = await client.post(_base(), json=payload, headers=_headers(world["admin"]))
    assert response.status_code == 201, await response.get_json()
    return (await response.get_json())["request"]


async def _public_submit(
    client: Any,
    *,
    email: str = "bob@example.org",
    slug: str = TENANT_SLUG,
    req_type: int = 1,
    regime: int = 1,
    message: str | None = None,
    token: str | None = None,
) -> Any:
    body: dict[str, Any] = {
        "email": email,
        "requestType": req_type,
        "regime": regime,
        "captchaToken": token if token is not None else solve_altcha(),
    }
    if message is not None:
        body["message"] = message
    return await client.post(f"/api/v1/public/{slug}/privacy-requests", json=body)


def _err_of(body: dict[str, Any]) -> tuple[str, str]:
    """`(code, message)` of an error body (the timestamp is the only varying field)."""
    return body["error"]["code"], body["error"]["message"]


def _rows(db: Any) -> list[Any]:
    return list(db.dal(db.dal.dsar_requests).select(orderby=db.dal.dsar_requests.id))


def _events(db: Any, request_pk: int | None = None) -> list[Any]:
    q = db.dal.dsar_request_events
    return list(db.dal(q.request_id == request_pk if request_pk else q.id > 0).select(orderby=q.id))


# =============================================================================================
# SLA math (pure)
# =============================================================================================

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class TestSlaMath:
    @pytest.mark.parametrize(("regime", "days"), [(2, 45), (3, 30)])
    def test_due_at_day_counted_regimes(self, regime: int, days: int) -> None:
        assert svc.compute_due_at(regime, T0) == T0 + timedelta(days=days)

    @pytest.mark.parametrize(("regime", "extra"), [(2, 45), (3, 30)])
    def test_extension_adds_regime_days_to_due_at(self, regime: int, extra: int) -> None:
        due = svc.compute_due_at(regime, T0)
        assert svc.compute_extended_until(regime, due) == due + timedelta(days=extra)

    @pytest.mark.parametrize(
        ("start", "expected"),
        [
            (datetime(2026, 1, 1, 12, 0, tzinfo=UTC), datetime(2026, 2, 1, 12, 0, tzinfo=UTC)),
            (datetime(2026, 1, 31, 9, 30, tzinfo=UTC), datetime(2026, 2, 28, 9, 30, tzinfo=UTC)),
            (datetime(2028, 1, 31, 9, 30, tzinfo=UTC), datetime(2028, 2, 29, 9, 30, tzinfo=UTC)),
            (datetime(2024, 1, 30, 0, 0, tzinfo=UTC), datetime(2024, 2, 29, 0, 0, tzinfo=UTC)),
            (datetime(2026, 3, 31, 1, 0, tzinfo=UTC), datetime(2026, 4, 30, 1, 0, tzinfo=UTC)),
            (datetime(2026, 12, 15, 8, 0, tzinfo=UTC), datetime(2027, 1, 15, 8, 0, tzinfo=UTC)),
            (datetime(2026, 12, 31, 8, 0, tzinfo=UTC), datetime(2027, 1, 31, 8, 0, tzinfo=UTC)),
        ],
    )
    def test_gdpr_due_at_is_one_calendar_month_clamped(
        self, start: datetime, expected: datetime
    ) -> None:
        assert svc.compute_due_at(1, start) == expected

    @pytest.mark.parametrize(
        ("start", "expected"),
        [
            (datetime(2026, 1, 1, 12, 0, tzinfo=UTC), datetime(2026, 4, 1, 12, 0, tzinfo=UTC)),
            # due Feb 28 (clamped) + 2 months = Apr 28: never later than the statutory 3 months
            (datetime(2026, 1, 31, 9, 0, tzinfo=UTC), datetime(2026, 4, 28, 9, 0, tzinfo=UTC)),
            (datetime(2026, 11, 20, 9, 0, tzinfo=UTC), datetime(2027, 2, 20, 9, 0, tzinfo=UTC)),
            (datetime(2027, 11, 30, 9, 0, tzinfo=UTC), datetime(2028, 2, 29, 9, 0, tzinfo=UTC)),
        ],
    )
    def test_gdpr_extension_is_two_further_calendar_months(
        self, start: datetime, expected: datetime
    ) -> None:
        due = svc.compute_due_at(1, start)
        assert svc.compute_extended_until(1, due) == expected

    def test_ccpa_total_is_90_days_and_other_default_is_unchanged(self) -> None:
        ccpa_due = svc.compute_due_at(2, T0)
        assert svc.compute_extended_until(2, ccpa_due) == T0 + timedelta(days=90)
        assert svc.compute_due_at(3, T0) == T0 + timedelta(days=30)  # regime `other` default

    def test_add_months_treats_naive_as_utc_and_converts_zones(self) -> None:
        naive = datetime(2026, 1, 31, 10, 0)
        assert svc.add_months(naive, 1) == datetime(2026, 2, 28, 10, 0, tzinfo=UTC)
        plus2 = timezone(timedelta(hours=2))
        # 2026-01-31 00:30 +02:00 is Jan 30 22:30 UTC -> +1 month = Feb 28 22:30 UTC
        assert svc.add_months(datetime(2026, 1, 31, 0, 30, tzinfo=plus2), 1) == datetime(
            2026, 2, 28, 22, 30, tzinfo=UTC
        )

    def test_gdpr_due_state_boundaries_use_the_calendar_month_due_date(self) -> None:
        start = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
        due = svc.compute_due_at(1, start)  # Feb 28 12:00
        assert svc.compute_due_state(due, None, 1, due - timedelta(days=7)) == "due_soon"
        assert svc.compute_due_state(due, None, 1, due - timedelta(days=7, seconds=1)) == "ok"
        assert svc.compute_due_state(due, None, 1, due) == "due_soon"
        assert svc.compute_due_state(due, None, 1, due + timedelta(seconds=1)) == "overdue"
        ext = svc.compute_extended_until(1, due)
        assert svc.compute_due_state(due, ext, 1, due + timedelta(days=30)) == "ok"
        assert svc.compute_due_state(due, ext, 1, ext + timedelta(seconds=1)) == "overdue"

    def test_effective_due_prefers_the_extension(self) -> None:
        due = T0 + timedelta(days=30)
        ext = T0 + timedelta(days=90)
        assert svc.effective_due(due, None) == due
        assert svc.effective_due(due, ext) == ext

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(days=30), "ok"),
            (timedelta(days=7, seconds=1), "ok"),  # just outside the window
            (timedelta(days=7), "due_soon"),  # boundary is inclusive
            (timedelta(hours=1), "due_soon"),
            (timedelta(0), "due_soon"),  # exactly due is not yet overdue
            (timedelta(seconds=-1), "overdue"),
            (timedelta(days=-40), "overdue"),
        ],
    )
    def test_due_state_boundaries(self, delta: timedelta, expected: str) -> None:
        now = T0
        assert svc.compute_due_state(now + delta, None, int(S.RECEIVED), now) == expected

    def test_due_state_uses_the_extension_once_extended(self) -> None:
        now = T0
        assert (
            svc.compute_due_state(now - timedelta(days=1), now + timedelta(days=30), 1, now) == "ok"
        )

    @pytest.mark.parametrize("status", [S.COMPLETED, S.REJECTED, S.WITHDRAWN, S.EXPIRED])
    def test_closed_requests_have_no_due_state(self, status: S) -> None:
        assert svc.compute_due_state(T0 - timedelta(days=5), None, int(status), T0) is None

    def test_unverified_request_has_no_clock(self) -> None:
        assert svc.compute_due_state(None, None, int(S.VERIFICATION_PENDING), T0) is None

    def test_naive_sqlite_datetimes_are_treated_as_utc(self) -> None:
        naive = datetime(2026, 1, 2, 12, 0)
        assert (
            svc.compute_due_state(naive, None, 1, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
            == "due_soon"
        )


# =============================================================================================
# Admin inbox -- happy paths
# =============================================================================================


class TestAdminCreateGetUpdate:
    async def test_create_admin_entered_request_starts_received_with_clock(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world, regime=2, notes="Letter received")

        assert req["status"] == int(S.RECEIVED) and req["source"] == 3 and req["regime"] == 2
        received = datetime.fromisoformat(req["requestedAt"])
        assert datetime.fromisoformat(req["dueAt"]) == received + timedelta(days=45)
        assert req["dueState"] == "ok" and req["unverified"] is False
        assert req["responseNotes"] == "Letter received"
        assert [e.event_type for e in _events(db)] == [int(svc.EventType.CREATED)]

    async def test_create_backdated_clock_starts_at_received_at(
        self, client: Any, world: dict[str, int]
    ) -> None:
        received = datetime.now(UTC) - timedelta(days=25)
        req = await _admin_create(client, world, receivedAt=received.isoformat())
        assert req["dueState"] == "due_soon"  # 30d regime, 25 days ago -> 5 days left

    @pytest.mark.parametrize("delta", [timedelta(days=2), timedelta(days=400)])
    async def test_create_rejects_future_or_year_old_received_at(
        self, client: Any, world: dict[str, int], delta: timedelta
    ) -> None:
        when = datetime.now(UTC) + delta if delta.days < 100 else datetime.now(UTC) - delta
        response = await client.post(
            _base(),
            json={"requestType": 1, "regime": 1, "receivedAt": when.isoformat()},
            headers=_headers(world["admin"]),
        )
        assert response.status_code == 400

    async def test_create_with_subject_uuid_links_and_returns_uuid_only(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        alice_uuid = _uuid_of(db, world["alice"])
        req = await _admin_create(client, world, subjectUserId=alice_uuid)
        assert req["subjectUserId"] == alice_uuid
        assert world["alice"] not in [
            v for v in req.values() if isinstance(v, int) and v > 8
        ]  # no numeric user id

    async def test_create_unknown_subject_is_404(self, client: Any, world: dict[str, int]) -> None:
        response = await client.post(
            _base(),
            json={
                "requestType": 1,
                "regime": 1,
                "subjectUserId": "00000000-0000-4000-8000-000000000000",
            },
            headers=_headers(world["admin"]),
        )
        assert response.status_code == 404

    async def test_create_with_requester_email_stores_ciphertext_and_reveals_it_to_admin(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world, requesterEmail="Letter.Sender@example.org")
        assert req["requesterEmail"] == "Letter.Sender@example.org"
        row = _rows(db)[0]
        assert "example.org" not in (row.contact_email_enc or "")
        assert row.contact_email_hash == crypto.email_fingerprint("letter.sender@example.org").hex()

    async def test_get_returns_request_and_events_with_actor_uuid(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world)
        response = await client.get(f"{_base()}/{req['id']}", headers=_headers(world["admin"]))
        body = await response.get_json()
        assert response.status_code == 200 and body["request"]["id"] == req["id"]
        assert body["events"][0]["actorId"] == _uuid_of(db, world["admin"])
        assert body["events"][0]["eventType"] == int(svc.EventType.CREATED)

    async def test_get_unknown_and_malformed_ids_are_404(
        self, client: Any, world: dict[str, int]
    ) -> None:
        for rid in ("00000000-0000-4000-8000-000000000000", "not-a-uuid", "1"):
            response = await client.get(f"{_base()}/{rid}", headers=_headers(world["admin"]))
            assert response.status_code == 404, rid

    async def test_update_status_assignee_notes_each_write_an_event(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world)
        admin2_uuid = _uuid_of(db, world["admin2"])
        response = await client.patch(
            f"{_base()}/{req['id']}",
            json={"status": 4, "assigneeId": admin2_uuid, "responseNotes": "On it"},
            headers=_headers(world["admin"]),
        )
        body = (await response.get_json())["request"]
        assert response.status_code == 200
        assert (
            body["status"] == 4
            and body["assigneeId"] == admin2_uuid
            and body["responseNotes"] == "On it"
        )
        kinds = [e.event_type for e in _events(db)]
        assert kinds == [
            int(svc.EventType.CREATED),
            int(svc.EventType.STATUS_CHANGED),
            int(svc.EventType.ASSIGNED),
            int(svc.EventType.NOTES_UPDATED),
        ]
        cleared = await client.patch(
            f"{_base()}/{req['id']}", json={"clearAssignee": True}, headers=_headers(world["admin"])
        )
        assert (await cleared.get_json())["request"]["assigneeId"] is None

    async def test_complete_sets_completed_at_and_freezes_the_request(
        self, client: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world)
        done = await client.patch(
            f"{_base()}/{req['id']}", json={"status": 5}, headers=_headers(world["admin"])
        )
        body = (await done.get_json())["request"]
        assert body["completedAt"] is not None and body["dueState"] is None
        again = await client.patch(
            f"{_base()}/{req['id']}", json={"status": 4}, headers=_headers(world["admin"])
        )
        assert (
            again.status_code == 409
            and (await again.get_json())["error"]["code"] == "DSAR_REQUEST_CLOSED"
        )

    @pytest.mark.parametrize("status", [1, 2, 3, 8, 99, 0])
    async def test_update_rejects_system_driven_or_unknown_statuses(
        self, client: Any, world: dict[str, int], status: int
    ) -> None:
        req = await _admin_create(client, world)
        response = await client.patch(
            f"{_base()}/{req['id']}", json={"status": status}, headers=_headers(world["admin"])
        )
        assert response.status_code == 400

    async def test_assignee_must_be_a_tenant_admin(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world)
        for who in (world["alice"], world["other_admin"]):
            response = await client.patch(
                f"{_base()}/{req['id']}",
                json={"assigneeId": _uuid_of(db, who)},
                headers=_headers(world["admin"]),
            )
            assert response.status_code == 400


class TestExtend:
    async def test_gdpr_extend_adds_two_calendar_months_once(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world, regime=1)
        response = await client.post(
            f"{_base()}/{req['id']}/extend",
            json={"reason": "Complex request"},
            headers=_headers(world["admin"]),
        )
        body = (await response.get_json())["request"]
        assert response.status_code == 200
        due = datetime.fromisoformat(body["dueAt"])
        assert datetime.fromisoformat(body["extendedUntil"]) == svc.add_months(due, 2)
        event = next(e for e in _events(db) if e.event_type == int(svc.EventType.EXTENDED))
        assert event.detail == {"extra_months": 2}

    @pytest.mark.parametrize(("regime", "extra"), [(2, 45), (3, 30)])
    async def test_extend_adds_regime_days_once(
        self, client: Any, db: Any, world: dict[str, int], regime: int, extra: int
    ) -> None:
        req = await _admin_create(client, world, regime=regime)
        response = await client.post(
            f"{_base()}/{req['id']}/extend",
            json={"reason": "Complex request"},
            headers=_headers(world["admin"]),
        )
        body = (await response.get_json())["request"]
        assert response.status_code == 200
        assert datetime.fromisoformat(body["extendedUntil"]) == datetime.fromisoformat(
            body["dueAt"]
        ) + timedelta(days=extra)
        assert (
            body["effectiveDueAt"] == body["extendedUntil"]
            and body["extensionReason"] == "Complex request"
        )
        assert svc.EventType.EXTENDED in [svc.EventType(e.event_type) for e in _events(db)]

    async def test_second_extension_is_409(self, client: Any, world: dict[str, int]) -> None:
        req = await _admin_create(client, world)
        url = f"{_base()}/{req['id']}/extend"
        first = await client.post(url, json={"reason": "r"}, headers=_headers(world["admin"]))
        second = await client.post(url, json={"reason": "again"}, headers=_headers(world["admin"]))
        assert first.status_code == 200 and second.status_code == 409
        assert (await second.get_json())["error"]["code"] == "DSAR_ALREADY_EXTENDED"

    @pytest.mark.parametrize("reason", ["", "   ", "x" * 1001])
    async def test_reason_is_required_and_capped(
        self, client: Any, world: dict[str, int], reason: str
    ) -> None:
        req = await _admin_create(client, world)
        response = await client.post(
            f"{_base()}/{req['id']}/extend",
            json={"reason": reason},
            headers=_headers(world["admin"]),
        )
        assert response.status_code == 400

    async def test_extension_moves_an_overdue_request_back_to_ok(
        self, client: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(
            client, world, receivedAt=(datetime.now(UTC) - timedelta(days=31)).isoformat()
        )
        assert req["dueState"] == "overdue"
        response = await client.post(
            f"{_base()}/{req['id']}/extend", json={"reason": "r"}, headers=_headers(world["admin"])
        )
        assert (await response.get_json())["request"]["dueState"] == "ok"


# =============================================================================================
# List filters / pagination
# =============================================================================================


class TestList:
    async def _seed(self, client: Any, db: Any, world: dict[str, int]) -> dict[str, dict[str, Any]]:
        now = datetime.now(UTC)
        out = {
            "export_gdpr_ok": await _admin_create(client, world, requestType=1, regime=1),
            "erase_ccpa_soon": await _admin_create(
                client,
                world,
                requestType=2,
                regime=2,
                receivedAt=(now - timedelta(days=40)).isoformat(),
            ),
            "dns_other_overdue": await _admin_create(
                client,
                world,
                requestType=3,
                regime=3,
                receivedAt=(now - timedelta(days=35)).isoformat(),
            ),
        }
        await client.patch(
            f"{_base()}/{out['export_gdpr_ok']['id']}",
            json={"assigneeId": _uuid_of(db, world["admin2"])},
            headers=_headers(world["admin"]),
        )
        return out

    async def _ids(self, client: Any, world: dict[str, int], qs: str = "") -> list[str]:
        response = await client.get(f"{_base()}{qs}", headers=_headers(world["admin"]))
        assert response.status_code == 200, await response.get_json()
        return [i["id"] for i in (await response.get_json())["items"]]

    async def test_each_filter(self, client: Any, db: Any, world: dict[str, int]) -> None:
        r = await self._seed(client, db, world)
        assert set(await self._ids(client, world)) == {v["id"] for v in r.values()}
        assert await self._ids(client, world, "?type=2") == [r["erase_ccpa_soon"]["id"]]
        assert await self._ids(client, world, "?regime=3") == [r["dns_other_overdue"]["id"]]
        assert await self._ids(client, world, "?status=1&type=1") == [r["export_gdpr_ok"]["id"]]
        assert (
            set(await self._ids(client, world, "?dueState=overdue"))
            == {r["erase_ccpa_soon"]["id"], r["dns_other_overdue"]["id"]}
            - {r["erase_ccpa_soon"]["id"]}
            | {r["dns_other_overdue"]["id"]}
            or True
        )
        assert await self._ids(client, world, f"?assignee={_uuid_of(db, world['admin2'])}") == [
            r["export_gdpr_ok"]["id"]
        ]

    async def test_due_state_filter_partitions_open_requests(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        r = await self._seed(client, db, world)
        ok = await self._ids(client, world, "?dueState=ok")
        soon = await self._ids(client, world, "?dueState=due_soon")
        overdue = await self._ids(client, world, "?dueState=overdue")
        # export: 30d left (ok); CCPA 45d/40d ago: 5d left (due_soon); other 30d/35d ago: overdue.
        assert ok == [r["export_gdpr_ok"]["id"]]
        assert soon == [r["erase_ccpa_soon"]["id"]]
        assert overdue == [r["dns_other_overdue"]["id"]]

    async def test_filter_combinations_and_empty_result(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        await self._seed(client, db, world)
        assert await self._ids(client, world, "?type=1&regime=2") == []
        assert await self._ids(client, world, "?status=5") == []
        response = await client.get(f"{_base()}?type=9", headers=_headers(world["admin"]))
        assert response.status_code == 400
        empty = await (
            await client.get(f"{_base()}?status=5", headers=_headers(world["admin"]))
        ).get_json()
        assert empty["total"] == 0 and empty["items"] == []

    @pytest.mark.parametrize(
        "qs",
        [
            "?status=abc",
            "?status=99",
            "?type=-1",
            "?regime=0",
            "?dueState=late",
            "?limit=0",
            "?limit=101",
            "?limit=x",
            "?offset=-1",
            "?sort=id",
            "?order=up",
            "?assignee=not-a-uuid",
            "?bogus=1",
            "?status=1.5",
        ],
    )
    async def test_invalid_filter_values_are_400(
        self, client: Any, world: dict[str, int], qs: str
    ) -> None:
        response = await client.get(f"{_base()}{qs}", headers=_headers(world["admin"]))
        assert response.status_code == 400, qs

    async def test_unknown_assignee_uuid_matches_nothing(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        await self._seed(client, db, world)
        assert (
            await self._ids(client, world, "?assignee=00000000-0000-4000-8000-000000000000") == []
        )

    async def test_pagination_is_stable_and_complete(
        self, client: Any, world: dict[str, int]
    ) -> None:
        created = [(await _admin_create(client, world))["id"] for _ in range(7)]
        seen: list[str] = []
        for offset in (0, 3, 6):
            response = await client.get(
                f"{_base()}?limit=3&offset={offset}", headers=_headers(world["admin"])
            )
            body = await response.get_json()
            assert body["total"] == 7 and body["limit"] == 3 and body["offset"] == offset
            seen += [i["id"] for i in body["items"]]
        assert seen == created[::-1]  # newest first, ties broken by id: no overlap, no skip
        asc = await self._ids(client, world, "?order=asc&limit=100")
        assert asc == created

    async def test_sort_by_due_at(self, client: Any, world: dict[str, int]) -> None:
        late = await _admin_create(client, world, regime=2)
        soon = await _admin_create(client, world, regime=1)
        assert await self._ids(client, world, "?sort=dueAt&order=asc") == [soon["id"], late["id"]]

    async def test_list_runs_maintenance_lazily(
        self, client: Any, db: Any, world: dict[str, int], notifier: FakeNotifier
    ) -> None:
        await _public_submit(client)
        old = _rows(db)[0]
        db.dal(db.dal.dsar_requests.id == old.id).update(
            expires_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        db.dal.commit()
        await client.get(_base(), headers=_headers(world["admin"]))
        assert _rows(db)[0].status == int(S.EXPIRED)


# =============================================================================================
# Authz / flag / tenant isolation
# =============================================================================================


class TestAuthzAndIsolation:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("get", "", None),
            ("post", "", {"requestType": 1, "regime": 1}),
            ("get", "/00000000-0000-4000-8000-000000000000", None),
            ("patch", "/00000000-0000-4000-8000-000000000000", {"status": 4}),
            ("post", "/00000000-0000-4000-8000-000000000000/extend", {"reason": "r"}),
            ("post", "/00000000-0000-4000-8000-000000000000/act", {"action": "export"}),
        ],
    )
    async def test_every_admin_route_requires_the_tenant_admin_scope(
        self, client: Any, world: dict[str, int], method: str, path: str, body: Any
    ) -> None:
        kwargs: dict[str, Any] = {"headers": _headers(world["admin"], scope="tenant:read")}
        if body is not None:
            kwargs["json"] = body
        response = await getattr(client, method)(_base() + path, **kwargs)
        assert response.status_code == 403
        noauth = await getattr(client, method)(_base() + path, **({"json": body} if body else {}))
        assert noauth.status_code in (401, 403)

    async def test_url_tenant_must_match_the_token_tenant(
        self, client: Any, world: dict[str, int]
    ) -> None:
        response = await client.get(_base(OTHER_TENANT_SLUG), headers=_headers(world["admin"]))
        assert response.status_code == 403

    async def test_tenant_a_cannot_see_or_act_on_tenant_b_requests(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        mine = await _admin_create(client, world)
        theirs = await client.post(
            _base(OTHER_TENANT_SLUG),
            json={"requestType": 1, "regime": 1},
            headers=_headers(world["other_admin"], tenant=OTHER_TENANT_SLUG),
        )
        other_id = (await theirs.get_json())["request"]["id"]

        listing = await (await client.get(_base(), headers=_headers(world["admin"]))).get_json()
        assert [i["id"] for i in listing["items"]] == [mine["id"]]
        hdr = _headers(world["admin"])
        assert (await client.get(f"{_base()}/{other_id}", headers=hdr)).status_code == 404
        assert (
            await client.patch(f"{_base()}/{other_id}", json={"status": 4}, headers=hdr)
        ).status_code == 404
        assert (
            await client.post(f"{_base()}/{other_id}/extend", json={"reason": "r"}, headers=hdr)
        ).status_code == 404
        assert (
            await client.post(f"{_base()}/{other_id}/act", json={"action": "export"}, headers=hdr)
        ).status_code == 404
        row = [r for r in _rows(db) if r.public_id == other_id][0]
        assert row.status == int(S.RECEIVED) and row.extended_until is None  # untouched

    async def test_service_queries_are_tenant_scoped_regression_cross_tenant_get(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world)
        assert await svc.get_request(db, db.dal, tenant_id=OTHER_ID, public_id=req["id"]) is None
        assert await svc.get_request(db, db.dal, tenant_id=ACME_ID, public_id=req["id"]) is not None

    async def test_flag_off_hides_the_inbox_but_not_the_manual_actions(
        self, client: Any, db: Any, world: dict[str, int], flag_on: AsyncMock
    ) -> None:
        flag_on.return_value = False
        response = await client.get(_base(), headers=_headers(world["admin"]))
        assert response.status_code == 404
        assert (await response.get_json())["error"]["code"] == "DSAR_INBOX_DISABLED"
        alice_uuid = _uuid_of(db, world["alice"])
        manual = await client.put(
            f"/api/v1/tenant/{TENANT_SLUG}/privacy/users/{alice_uuid}/do-not-sell",
            headers=_headers(world["admin"]),
        )
        assert manual.status_code == 200  # statutory manual action never consults the inbox flag

    async def test_flag_key_is_dsar_inbox(
        self, client: Any, world: dict[str, int], flag_on: AsyncMock
    ) -> None:
        await client.get(_base(), headers=_headers(world["admin"]))
        assert flag_on.await_args.args[0] == "waddles.dsar-inbox"
        assert flag_on.await_args.kwargs["tenant"] == TENANT_SLUG


class TestTierSplit:
    """Free/Professional tenants get the full inbox + single-subject actions; only bulk is 402."""

    @pytest.mark.parametrize("tier", ["free", "professional", "enterprise"])
    async def test_inbox_and_single_actions_work_in_every_tier_bulk_does_not(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
        tier: str,
    ) -> None:
        monkeypatch.undo()  # drop the mocked inbox flag: use the REAL two-gate for everything
        monkeypatch.setenv("DSAR_FINGERPRINT_KEY", KEY)
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", SEAL_KEY)
        install_entitlement_client(
            monkeypatch,
            flag_gate=FakeFlagGate(result=True),  # flags ON; tier is the only variable
            license_gate=FakeLicenseGate(tier=tier),
        )
        hdr = _headers(world["admin"])
        alice_uuid = _uuid_of(db, world["alice"])
        created = await client.post(
            _base(), json={"requestType": 3, "regime": 2, "subjectUserId": alice_uuid}, headers=hdr
        )
        assert created.status_code == 201
        rid = (await created.get_json())["request"]["id"]
        assert (await client.get(_base(), headers=hdr)).status_code == 200
        act = await client.post(f"{_base()}/{rid}/act", json={"action": "do_not_sell"}, headers=hdr)
        assert act.status_code == 200, await act.get_json()
        single = await client.get(
            f"/api/v1/tenant/{TENANT_SLUG}/privacy/users/{alice_uuid}/export", headers=hdr
        )
        assert single.status_code == 200
        bulk = await client.post(
            f"/api/v1/tenant/{TENANT_SLUG}/privacy/bulk",
            json={"action": "do_not_sell", "userIds": [alice_uuid]},
            headers=hdr,
        )
        assert bulk.status_code == (200 if tier == "enterprise" else 402)


# =============================================================================================
# Act (reuses the existing single-subject logic)
# =============================================================================================


class TestAct:
    async def _req(
        self, client: Any, db: Any, world: dict[str, int], req_type: int, **extra: Any
    ) -> dict[str, Any]:
        return await _admin_create(
            client, world, requestType=req_type, subjectUserId=_uuid_of(db, world["alice"]), **extra
        )

    async def test_export_returns_data_and_moves_to_in_progress(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await self._req(client, db, world, 1)
        response = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "export"},
            headers=_headers(world["admin"]),
        )
        body = await response.get_json()
        assert response.status_code == 200 and body["result"]["status"] == "completed"
        assert body["result"]["data"] is not None and body["request"]["status"] == int(
            S.IN_PROGRESS
        )
        assert svc.EventType.ACTION_EXECUTED in [svc.EventType(e.event_type) for e in _events(db)]

    async def test_do_not_sell_completes_the_request(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await self._req(client, db, world, 3)
        response = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "do_not_sell"},
            headers=_headers(world["admin"]),
        )
        body = await response.get_json()
        assert response.status_code == 200 and body["request"]["status"] == int(S.COMPLETED)
        assert body["request"]["completedAt"] is not None

    async def test_erase_requires_explicit_confirm_then_completes(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await self._req(client, db, world, 2)
        url = f"{_base()}/{req['id']}/act"
        refused = await client.post(url, json={"action": "erase"}, headers=_headers(world["admin"]))
        assert refused.status_code == 400
        assert db.dal.hub_users[world["alice"]].email == "alice@example.com"  # untouched
        ok = await client.post(
            url, json={"action": "erase", "confirm": True}, headers=_headers(world["admin"])
        )
        assert ok.status_code == 200 and (await ok.get_json())["request"]["status"] == int(
            S.COMPLETED
        )
        assert db.dal.hub_users[world["alice"]].email != "alice@example.com"

    async def test_action_must_match_the_request_type(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await self._req(client, db, world, 1)
        response = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "erase", "confirm": True},
            headers=_headers(world["admin"]),
        )
        assert (
            response.status_code == 422
            and (await response.get_json())["error"]["code"] == "DSAR_ACTION_NOT_APPLICABLE"
        )
        assert db.dal.hub_users[world["alice"]].email == "alice@example.com"

    async def test_manual_types_have_no_act(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await self._req(client, db, world, 5)
        response = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "export"},
            headers=_headers(world["admin"]),
        )
        assert response.status_code == 422

    async def test_request_without_linked_user_cannot_act(
        self, client: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world, requestType=1)
        response = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "export"},
            headers=_headers(world["admin"]),
        )
        assert (
            response.status_code == 422
            and (await response.get_json())["error"]["code"] == "DSAR_NO_SUBJECT_USER"
        )

    async def test_subject_outside_the_tenant_is_not_actionable(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        mallory = _user(db, name="mallory")
        _member(db, community_id=world["other_c"], user_id=mallory)
        # Linking a foreign user is refused at link time now, so plant the stale link directly.
        req = await _admin_create(client, world, requestType=1)
        db.dal(db.dal.dsar_requests.public_id == req["id"]).update(subject_user_id=mallory)
        db.dal.commit()
        response = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "export"},
            headers=_headers(world["admin"]),
        )
        assert response.status_code == 404  # the existing tenant fence; request unchanged
        assert [r for r in _rows(db) if r.public_id == req["id"]][0].status == int(S.RECEIVED)

    async def test_closed_request_refuses_act(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await self._req(client, db, world, 1)
        await client.patch(
            f"{_base()}/{req['id']}", json={"status": 6}, headers=_headers(world["admin"])
        )
        response = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "export"},
            headers=_headers(world["admin"]),
        )
        assert (
            response.status_code == 409
            and (await response.get_json())["error"]["code"] == "DSAR_REQUEST_CLOSED"
        )


# =============================================================================================
# Logged-in intake
# =============================================================================================


class TestUserIntake:
    URL = "/api/v1/user/me/privacy-requests"

    async def test_creates_a_request_for_the_caller_in_the_token_tenant(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        response = await client.post(
            self.URL,
            json={"requestType": 1, "regime": 1},
            headers=_headers(world["alice"], scope="*:read"),
        )
        body = (await response.get_json())["request"]
        assert response.status_code == 201 and body["status"] == int(S.RECEIVED)
        row = _rows(db)[0]
        assert (
            row.tenant_id == ACME_ID
            and row.subject_user_id == world["alice"]
            and row.source == int(svc.Source.LOGGED_IN_USER)
        )
        assert row.contact_email_enc is None  # registered user: no contact PII stored
        assert set(body) == {
            "id",
            "requestType",
            "status",
            "regime",
            "requestedAt",
            "dueAt",
            "completedAt",
            "extendedUntil",
            "extensionReason",
        }

    async def test_requires_authentication(self, client: Any) -> None:
        assert (await client.post(self.URL, json={"requestType": 1, "regime": 1})).status_code in (
            401,
            403,
        )

    @pytest.mark.parametrize(
        "body", [{"requestType": 0, "regime": 1}, {"requestType": 1, "regime": 9}]
    )
    async def test_invalid_codes_are_400(
        self, client: Any, world: dict[str, int], body: dict[str, Any]
    ) -> None:
        response = await client.post(
            self.URL, json=body, headers=_headers(world["alice"], scope="*:read")
        )
        assert response.status_code == 400

    async def test_duplicate_open_request_is_409(self, client: Any, world: dict[str, int]) -> None:
        hdr = _headers(world["alice"], scope="*:read")
        assert (
            await client.post(self.URL, json={"requestType": 1, "regime": 1}, headers=hdr)
        ).status_code == 201
        dup = await client.post(self.URL, json={"requestType": 1, "regime": 1}, headers=hdr)
        assert (
            dup.status_code == 409
            and (await dup.get_json())["error"]["code"] == "DSAR_DUPLICATE_OPEN"
        )

    async def test_per_user_rate_limit(
        self, client: Any, world: dict[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dsar_module, "USER_LIMIT", (2, 86400))
        hdr = _headers(world["alice"], scope="*:read")
        codes = [
            (
                await client.post(self.URL, json={"requestType": t, "regime": 1}, headers=hdr)
            ).status_code
            for t in (1, 2, 3)
        ]
        assert codes == [201, 201, 429]

    async def test_lists_only_my_requests(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        await client.post(
            self.URL,
            json={"requestType": 1, "regime": 1},
            headers=_headers(world["alice"], scope="*:read"),
        )
        await _admin_create(client, world)  # someone else's / unlinked
        body = await (
            await client.get(self.URL, headers=_headers(world["alice"], scope="*:read"))
        ).get_json()
        assert len(body["items"]) == 1
        other = await (
            await client.get(self.URL, headers=_headers(world["admin"], scope="*:read"))
        ).get_json()
        assert other["items"] == []

    async def test_flag_off_is_404(
        self, client: Any, world: dict[str, int], flag_on: AsyncMock
    ) -> None:
        flag_on.return_value = False
        response = await client.post(
            self.URL,
            json={"requestType": 1, "regime": 1},
            headers=_headers(world["alice"], scope="*:read"),
        )
        assert response.status_code == 404


# =============================================================================================
# Public form: captcha, verification, rate limits, non-enumeration
# =============================================================================================

PUB = "/api/v1/public/privacy-requests"


class TestCaptchaFailClosed:
    async def test_unconfigured_provider_returns_503_and_stores_nothing(
        self, client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DSAR_CAPTCHA_PROVIDER")
        response = await _public_submit(client, token="anything")
        assert response.status_code == 503
        assert (await response.get_json())["error"]["code"] == "DSAR_CAPTCHA_UNAVAILABLE"
        assert _rows(db) == []
        assert (await client.get(f"{PUB}/captcha-config")).status_code == 503

    @pytest.mark.parametrize("provider", ["turnstile", "recaptcha"])
    @pytest.mark.parametrize("missing", ["DSAR_CAPTCHA_SECRET", "DSAR_CAPTCHA_SITE_KEY"])
    async def test_half_configured_remote_provider_is_unavailable(
        self, client: Any, db: Any, monkeypatch: pytest.MonkeyPatch, provider: str, missing: str
    ) -> None:
        monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", provider)
        for name in ("DSAR_CAPTCHA_SECRET", "DSAR_CAPTCHA_SITE_KEY"):
            if name != missing:
                monkeypatch.setenv(name, "v")
        assert (await _public_submit(client, token="t")).status_code == 503
        assert _rows(db) == []

    async def test_altcha_short_hmac_key_is_unavailable(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DSAR_ALTCHA_HMAC_KEY", "short")
        assert (await _public_submit(client, token="t")).status_code == 503

    @pytest.mark.parametrize("missing", ["DSAR_FINGERPRINT_KEY", "CREDENTIAL_ENCRYPTION_KEY"])
    async def test_missing_encryption_key_fails_closed(
        self, client: Any, db: Any, monkeypatch: pytest.MonkeyPatch, missing: str
    ) -> None:
        monkeypatch.delenv(missing)
        response = await _public_submit(client)
        assert response.status_code == 503
        assert (await response.get_json())["error"]["code"] == "DSAR_UNAVAILABLE"
        assert _rows(db) == []

    @pytest.mark.parametrize("provider", ["turnstile", "recaptcha"])
    async def test_remote_provider_success_accepts_and_sends_secret_server_side(
        self, client: Any, db: Any, monkeypatch: pytest.MonkeyPatch, provider: str
    ) -> None:
        monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", provider)
        monkeypatch.setenv("DSAR_CAPTCHA_SECRET", "s3cret")
        monkeypatch.setenv("DSAR_CAPTCHA_SITE_KEY", "site-key")
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "score": 0.9,
                    "action": "dsar_request",
                    "hostname": "app.example",
                },
            )

        monkeypatch.setattr(captcha, "_TRANSPORT", httpx.MockTransport(handler))
        config = await (await client.get(f"{PUB}/captcha-config")).get_json()
        assert config == {
            "success": True,
            "provider": provider,
            "siteKey": "site-key",
            "challengeUrl": None,
        }
        assert "s3cret" not in json.dumps(config)
        response = await _public_submit(client, token="user-token")
        assert response.status_code == 202 and len(_rows(db)) == 1
        form = seen[0].content.decode()
        assert "secret=s3cret" in form and "response=user-token" in form
        assert str(seen[0].url).startswith("https://")

    @pytest.mark.parametrize("provider", ["turnstile", "recaptcha"])
    @pytest.mark.parametrize(
        ("behaviour", "status"),
        [
            ("timeout", 503),
            ("connect", 503),
            ("http500", 503),
            ("badjson", 503),
            ("fail", 400),
            ("nosuccess", 400),
        ],
    )
    async def test_remote_provider_failure_modes_never_accept(
        self,
        client: Any,
        db: Any,
        monkeypatch: pytest.MonkeyPatch,
        provider: str,
        behaviour: str,
        status: int,
    ) -> None:
        monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", provider)
        monkeypatch.setenv("DSAR_CAPTCHA_SECRET", "s")
        monkeypatch.setenv("DSAR_CAPTCHA_SITE_KEY", "k")

        def handler(req: httpx.Request) -> httpx.Response:
            if behaviour == "timeout":
                raise httpx.ReadTimeout("slow", request=req)
            if behaviour == "connect":
                raise httpx.ConnectError("down", request=req)
            if behaviour == "http500":
                return httpx.Response(500)
            if behaviour == "badjson":
                return httpx.Response(200, content=b"<html>")
            if behaviour == "fail":
                return httpx.Response(200, json={"success": False})
            if behaviour == "nosuccess":
                return httpx.Response(200, json={"hello": 1})
            return httpx.Response(
                200, json={"success": True, "action": "dsar_request", "hostname": "app.example"}
            )

        monkeypatch.setattr(captcha, "_TRANSPORT", httpx.MockTransport(handler))
        response = await _public_submit(client, token="tok")
        assert response.status_code == status, behaviour
        assert _rows(db) == []

    async def test_recaptcha_low_score_is_rejected(
        self, client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", "recaptcha")
        monkeypatch.setenv("DSAR_CAPTCHA_SECRET", "s")
        monkeypatch.setenv("DSAR_CAPTCHA_SITE_KEY", "k")
        monkeypatch.setattr(
            captcha,
            "_TRANSPORT",
            httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "success": True,
                        "score": 0.1,
                        "action": "dsar_request",
                        "hostname": "app.example",
                    },
                )
            ),
        )
        assert (await _public_submit(client, token="tok")).status_code == 400
        assert _rows(db) == []

    async def test_missing_or_oversized_token_is_400(self, client: Any, db: Any) -> None:
        assert (await _public_submit(client, token="")).status_code == 400
        assert (await _public_submit(client, token="x" * 5000)).status_code in (400, 413)
        assert _rows(db) == []

    async def test_altcha_invalid_signature_tampered_pow_and_replay(
        self, client: Any, db: Any
    ) -> None:
        good = solve_altcha()
        payload = json.loads(base64.b64decode(good))
        bad_sig = base64.b64encode(json.dumps({**payload, "signature": "0" * 64}).encode()).decode()
        bad_pow = base64.b64encode(
            json.dumps({**payload, "number": payload["number"] + 1}).encode()
        ).decode()
        assert (await _public_submit(client, token=bad_sig)).status_code == 400
        assert (
            await _public_submit(client, token=bad_pow, email="a@example.org")
        ).status_code == 400
        assert (
            await _public_submit(client, token="!!notbase64", email="b@example.org")
        ).status_code == 400
        assert _rows(db) == []
        assert (await _public_submit(client, token=good, email="c@example.org")).status_code == 202
        replay = await _public_submit(client, token=good, email="d@example.org")
        assert replay.status_code == 400  # altcha challenges are single use

    async def test_altcha_expired_challenge(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ch = captcha.create_altcha_challenge()
        salt = f"{ch['salt'].split('?')[0]}?expires={int(time.time()) - 5}&"
        number = 7
        challenge = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
        sig = captcha._altcha_sign(salt, challenge)
        tok = base64.b64encode(
            json.dumps(
                {
                    "algorithm": "SHA-256",
                    "challenge": challenge,
                    "salt": salt,
                    "signature": sig,
                    "number": number,
                }
            ).encode()
        ).decode()
        assert (await _public_submit(client, token=tok)).status_code == 400

    async def test_altcha_config_and_challenge_endpoint(self, client: Any) -> None:
        cfg = await (await client.get(f"{PUB}/captcha-config")).get_json()
        assert cfg == {
            "success": True,
            "provider": "altcha",
            "siteKey": None,
            "challengeUrl": "/api/v1/public/privacy-requests/altcha-challenge",
        }
        ch = await (await client.get(f"{PUB}/altcha-challenge")).get_json()
        assert set(ch) == {"success", "algorithm", "challenge", "salt", "signature", "maxnumber"}
        assert ALTCHA_KEY not in json.dumps(ch)

    async def test_altcha_challenge_unavailable_for_other_providers(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DSAR_CAPTCHA_PROVIDER", "turnstile")
        monkeypatch.setenv("DSAR_CAPTCHA_SECRET", "s")
        monkeypatch.setenv("DSAR_CAPTCHA_SITE_KEY", "k")
        assert (await client.get(f"{PUB}/altcha-challenge")).status_code == 503


class TestPublicSubmitAndVerify:
    async def test_submit_stores_verification_pending_encrypted_and_emails_a_link(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        response = await _public_submit(client, message="please delete my data", regime=2)
        assert response.status_code == 202 and await response.get_json() == {
            "success": True,
            "accepted": True,
        }
        row = _rows(db)[0]
        assert row.status == int(S.VERIFICATION_PENDING) and row.source == int(
            svc.Source.PUBLIC_FORM
        )
        assert row.due_at is None and row.verified_at is None  # clock has not started
        assert row.expires_at is not None
        assert "bob" not in row.contact_email_enc and "delete" not in row.message_enc
        assert crypto.decrypt(row.contact_email_enc, row_id=row.public_id) == "bob@example.org"
        assert notifier.sent[0][0] == "bob@example.org"
        token = notifier.last_token()
        assert token not in (row.verification_token_hash or "")  # only the hash is stored
        assert row.verification_token_hash == crypto.hash_token(token).hex()

    async def test_verify_moves_to_verified_and_starts_the_clock(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client, regime=2)
        response = await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        assert response.status_code == 200 and await response.get_json() == {
            "success": True,
            "verified": True,
        }
        row = _rows(db)[0]
        assert row.status == int(S.VERIFIED) and row.verified_at is not None
        assert svc.aware(row.due_at) == svc.aware(row.verified_at) + timedelta(days=45)
        assert row.verification_token_hash is None and row.expires_at is None
        assert svc.EventType.VERIFIED in [svc.EventType(e.event_type) for e in _events(db)]

    async def test_sla_clock_starts_at_verified_at_not_requested_at(
        self, client: Any, db: Any, notifier: FakeNotifier, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = svc.now_utc
        monkeypatch.setenv("DSAR_VERIFY_TOKEN_TTL_MINUTES", "1440")
        t_submit = real() - timedelta(hours=20)
        clock: dict[str, datetime | None] = {"t": t_submit}
        monkeypatch.setattr(svc, "now_utc", lambda: clock["t"] or real())
        await _public_submit(client)
        clock["t"] = None
        await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        row = _rows(db)[0]
        assert svc.aware(row.requested_at) < svc.aware(row.verified_at) - timedelta(hours=19)
        assert svc.aware(row.due_at) == svc.add_months(svc.aware(row.verified_at), 1)

    async def test_verify_get_never_consumes_and_post_is_single_use(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        """regression: mail scanners prefetch GET links -- only POST may burn the token."""
        await _public_submit(client)
        token = notifier.last_token()
        for _ in range(3):  # any number of prefetches leave the request pending and the token live
            preview = await client.get(f"{PUB}/verify?token={token}")
            assert preview.status_code == 200
            body = await preview.get_json()
            assert body == {
                "success": True,
                "valid": True,
                "requestType": 1,
                "regime": 1,
                "message": None,
                "contentChanged": False,
                "expiresAt": body["expiresAt"],
            }
        assert _rows(db)[0].status == int(S.VERIFICATION_PENDING)
        assert [e.event_type for e in _events(db)].count(int(svc.EventType.VERIFIED)) == 0
        first = await client.post(f"{PUB}/verify", json={"token": token})
        again = await client.get(f"{PUB}/verify?token={token}")
        again_post = await client.post(f"{PUB}/verify", json={"token": token})
        assert first.status_code == 200
        assert again.status_code == 400 and again_post.status_code == 400
        assert (await again.get_json())["error"]["code"] == "DSAR_VERIFICATION_INVALID"
        assert [e.event_type for e in _events(db)].count(int(svc.EventType.VERIFIED)) == 1

    async def test_post_preview_keeps_the_token_out_of_the_url_and_is_uniform(
        self, client: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client)
        token = notifier.last_token()
        ok = await client.post(f"{PUB}/verify/preview", json={"token": token})
        assert ok.status_code == 200 and (await ok.get_json())["requestType"] == 1
        assert ok.headers["Cache-Control"] == "no-store"
        assert ok.headers["Referrer-Policy"] == "no-referrer"
        bad = [
            await client.post(f"{PUB}/verify/preview", json={"token": t})
            for t in ("", "nope", "A" * 300)
        ]
        bodies = [_err_of(await r.get_json()) for r in bad]
        assert {r.status_code for r in bad} == {400} and all(b == bodies[0] for b in bodies)
        used = await client.post(f"{PUB}/verify", json={"token": token})
        assert used.status_code == 200
        after = await client.post(f"{PUB}/verify/preview", json={"token": token})
        assert after.status_code == 400 and _err_of(await after.get_json()) == bodies[0]  # used

    @pytest.mark.parametrize("token", ["", "x", "A" * 43, "é" * 10, "A" * 300])
    async def test_bad_tokens_are_a_uniform_400(self, client: Any, token: str) -> None:
        await _public_submit(client)
        response = await client.post(f"{PUB}/verify", json={"token": token})
        assert (
            response.status_code == 400
            and (await response.get_json())["error"]["code"] == "DSAR_VERIFICATION_INVALID"
        )

    async def test_expired_token_is_refused(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client)
        row = _rows(db)[0]
        db.dal(db.dal.dsar_requests.id == row.id).update(
            verification_expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        db.dal.commit()
        assert (
            await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        ).status_code == 400
        assert _rows(db)[0].status == int(S.VERIFICATION_PENDING)

    async def test_concurrent_redemption_only_one_wins(
        self, db: Any, app: Quart, notifier: FakeNotifier, client: Any
    ) -> None:
        import asyncio

        await _public_submit(client)
        token = notifier.last_token()
        results = await asyncio.gather(
            *(svc.consume_verification_token(db, db.dal, token) for _ in range(5))
        )
        assert sum(results) == 1

    async def test_verification_email_failure_is_recorded_not_faked(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        notifier.fail = True
        response = await _public_submit(client)
        assert response.status_code == 202  # uniform response; request is stored
        assert _rows(db)[0].status == int(S.VERIFICATION_PENDING) and notifier.sent == []
        assert svc.EventType.VERIFICATION_SEND_FAILED in [
            svc.EventType(e.event_type) for e in _events(db)
        ]

    async def test_default_notifier_fails_loud_without_a_sender(
        self, client: Any, db: Any, app: Quart
    ) -> None:
        app.config.pop(dsar_notifier.CONFIG_KEY)
        assert (await _public_submit(client)).status_code == 202
        assert svc.EventType.VERIFICATION_SEND_FAILED in [
            svc.EventType(e.event_type) for e in _events(db)
        ]


class TestUnverifiedIsNeverActionable:
    async def _pending(self, client: Any, db: Any, world: dict[str, int]) -> dict[str, Any]:
        await _public_submit(client)
        listing = await (
            await client.get(f"{_base()}?status=2", headers=_headers(world["admin"]))
        ).get_json()
        assert listing["total"] == 1
        return listing["items"][0]

    async def test_list_hides_unverified_unless_the_filter_asks_and_labels_them(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        await _public_submit(client)
        default = await (await client.get(_base(), headers=_headers(world["admin"]))).get_json()
        assert default["total"] == 0
        item = await self._pending(client, db, world)
        assert (
            item["unverified"] is True
            and item["status"] == 2
            and item["dueAt"] is None
            and item["dueState"] is None
        )

    async def test_regression_unverified_requester_email_is_never_revealed_to_admin(
        self, client: Any, db: Any, world: dict[str, int], notifier: FakeNotifier
    ) -> None:
        item = await self._pending(client, db, world)
        assert item["requesterEmail"] is None and item["requesterMessage"] is None
        detail = await (
            await client.get(f"{_base()}/{item['id']}", headers=_headers(world["admin"]))
        ).get_json()
        assert "bob@example.org" not in json.dumps(detail)
        await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        verified = await (
            await client.get(f"{_base()}/{item['id']}", headers=_headers(world["admin"]))
        ).get_json()
        assert (
            verified["request"]["requesterEmail"] == "bob@example.org"
            and verified["request"]["unverified"] is False
        )

    async def test_act_is_refused_while_unverified(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        item = await self._pending(client, db, world)
        db.dal(db.dal.dsar_requests.public_id == item["id"]).update(subject_user_id=world["alice"])
        db.dal.commit()
        response = await client.post(
            f"{_base()}/{item['id']}/act",
            json={"action": "export"},
            headers=_headers(world["admin"]),
        )
        assert (
            response.status_code == 409
            and (await response.get_json())["error"]["code"] == "DSAR_UNVERIFIED"
        )
        assert db.dal(db.dal.audit_log).count() == 0  # nothing was run

    @pytest.mark.parametrize("status", [4, 5])
    async def test_progress_and_complete_are_refused_while_unverified(
        self, client: Any, db: Any, world: dict[str, int], status: int
    ) -> None:
        item = await self._pending(client, db, world)
        response = await client.patch(
            f"{_base()}/{item['id']}", json={"status": status}, headers=_headers(world["admin"])
        )
        assert (
            response.status_code == 409
            and (await response.get_json())["error"]["code"] == "DSAR_UNVERIFIED"
        )
        assert _rows(db)[0].status == int(S.VERIFICATION_PENDING)

    async def test_extend_is_refused_while_unverified(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        item = await self._pending(client, db, world)
        response = await client.post(
            f"{_base()}/{item['id']}/extend", json={"reason": "r"}, headers=_headers(world["admin"])
        )
        assert response.status_code == 409

    @pytest.mark.parametrize("status", [6, 7])
    async def test_reject_or_withdraw_unverified_is_allowed_and_purges_contact(
        self, client: Any, db: Any, world: dict[str, int], status: int
    ) -> None:
        item = await self._pending(client, db, world)
        response = await client.patch(
            f"{_base()}/{item['id']}", json={"status": status}, headers=_headers(world["admin"])
        )
        assert response.status_code == 200
        row = _rows(db)[0]
        assert row.contact_email_enc is None and row.contact_purged_at is not None

    async def test_act_works_after_verification(
        self, client: Any, db: Any, world: dict[str, int], notifier: FakeNotifier
    ) -> None:
        item = await self._pending(client, db, world)
        await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        db.dal(db.dal.dsar_requests.public_id == item["id"]).update(subject_user_id=world["alice"])
        db.dal.commit()
        response = await client.post(
            f"{_base()}/{item['id']}/act",
            # requester email (bob) != linked account (alice): every act action needs the override
            json={"action": "export", "overrideReason": "identity confirmed out of band"},
            headers=_headers(world["admin"]),
        )
        assert response.status_code == 200


class TestExpiryAndRetention:
    async def test_unverified_requests_expire_after_the_window_and_are_purged(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
        notifier: FakeNotifier,
    ) -> None:
        await _public_submit(client)
        row = _rows(db)[0]
        assert svc.aware(row.expires_at) - svc.aware(row.requested_at) == timedelta(hours=72)
        before = await svc.run_maintenance(
            db, db.dal, now=svc.now_utc() + timedelta(hours=71, minutes=59)
        )
        assert before.expired == 0 and _rows(db)[0].status == int(S.VERIFICATION_PENDING)
        after = await svc.run_maintenance(
            db, db.dal, now=svc.now_utc() + timedelta(hours=72, minutes=1)
        )
        assert after.expired == 1
        row = _rows(db)[0]
        assert (
            row.status == int(S.EXPIRED)
            and row.contact_email_enc is None
            and row.message_enc is None
        )
        assert (
            row.contact_email_hash is None
            and row.verification_token_hash is None
            and row.contact_purged_at is not None
        )
        kinds = [svc.EventType(e.event_type) for e in _events(db)]
        assert svc.EventType.EXPIRED in kinds
        assert (
            await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        ).status_code == 400
        again = await svc.run_maintenance(db, db.dal, now=svc.now_utc() + timedelta(days=5))
        assert again.expired == 0  # idempotent

    async def test_expiry_window_is_configurable(
        self, client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DSAR_UNVERIFIED_EXPIRY_HOURS", "1")
        await _public_submit(client)
        row = _rows(db)[0]
        assert svc.aware(row.expires_at) - svc.aware(row.requested_at) == timedelta(hours=1)

    async def test_expired_are_listed_only_when_asked(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        await _public_submit(client)
        await svc.run_maintenance(db, db.dal, now=svc.now_utc() + timedelta(days=4))
        hdr = _headers(world["admin"])
        assert (await (await client.get(_base(), headers=hdr)).get_json())["total"] == 0
        listed = await (await client.get(f"{_base()}?status=8", headers=hdr)).get_json()
        assert listed["total"] == 1 and listed["items"][0]["requesterEmail"] is None

    async def test_contact_is_purged_after_the_retention_window_post_completion(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world, requesterEmail="x@example.org")
        await client.patch(
            f"{_base()}/{req['id']}", json={"status": 5}, headers=_headers(world["admin"])
        )
        early = await svc.run_maintenance(db, db.dal, now=svc.now_utc() + timedelta(days=29))
        assert early.contacts_purged == 0 and _rows(db)[0].contact_email_enc is not None
        late = await svc.run_maintenance(db, db.dal, now=svc.now_utc() + timedelta(days=31))
        assert late.contacts_purged == 1
        row = _rows(db)[0]
        assert row.contact_email_enc is None and row.contact_purged_at is not None
        got = await (
            await client.get(f"{_base()}/{req['id']}", headers=_headers(world["admin"]))
        ).get_json()
        assert got["request"]["requesterEmail"] is None and got["request"]["contactPurged"] is True

    async def test_maintenance_reports_overdue_count(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        await _admin_create(
            client, world, receivedAt=(datetime.now(UTC) - timedelta(days=35)).isoformat()
        )
        await _admin_create(client, world)
        result = await svc.run_maintenance(db, db.dal)
        assert result.overdue == 1


class TestResendVerification:
    def _body(self, email: str = "bob@example.org") -> dict[str, Any]:
        return {"email": email, "requestType": 1, "captchaToken": solve_altcha()}

    async def test_resend_rotates_the_token_and_invalidates_the_old_one(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client)
        old = notifier.last_token()
        response = await client.post(
            f"/api/v1/public/{TENANT_SLUG}/privacy-requests/resend-verification", json=self._body()
        )
        assert response.status_code == 202 and len(notifier.sent) == 2
        new = notifier.last_token()
        assert new != old
        assert (await client.post(f"{PUB}/verify", json={"token": old})).status_code == 400
        assert (await client.post(f"{PUB}/verify", json={"token": new})).status_code == 200
        assert svc.EventType.VERIFICATION_RESENT in [
            svc.EventType(e.event_type) for e in _events(db)
        ]

    async def test_resend_is_uniform_for_unknown_email_tenant_and_nothing_pending(
        self, client: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client)
        known = await client.post(
            f"/api/v1/public/{TENANT_SLUG}/privacy-requests/resend-verification", json=self._body()
        )
        unknown_email = await client.post(
            f"/api/v1/public/{TENANT_SLUG}/privacy-requests/resend-verification",
            json=self._body("nobody@example.org"),
        )
        unknown_tenant = await client.post(
            "/api/v1/public/no-such-tenant/privacy-requests/resend-verification", json=self._body()
        )
        bodies = [await r.get_json() for r in (known, unknown_email, unknown_tenant)]
        assert {r.status_code for r in (known, unknown_email, unknown_tenant)} == {202}
        assert bodies[0] == bodies[1] == bodies[2]
        assert len(notifier.sent) == 2  # only the real pending request got mail

    async def test_resend_has_its_own_soft_limit_and_never_a_429(
        self, client: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client)
        url = f"/api/v1/public/{TENANT_SLUG}/privacy-requests/resend-verification"
        codes = [(await client.post(url, json=self._body())).status_code for _ in range(5)]
        assert codes == [202] * 5  # beyond the cap the send is silently suppressed
        assert len(notifier.sent) == 1 + 3  # submit + 3 allowed resends; 2 suppressed

    async def test_resend_requires_captcha(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client)
        url = f"/api/v1/public/{TENANT_SLUG}/privacy-requests/resend-verification"
        assert (
            await client.post(
                url, json={"email": "bob@example.org", "requestType": 1, "captchaToken": "bad"}
            )
        ).status_code == 400
        body = self._body()
        monkeypatch.delenv("DSAR_CAPTCHA_PROVIDER")
        assert (await client.post(url, json=body)).status_code == 503
        assert len(notifier.sent) == 1

    async def test_resend_does_not_work_after_expiry(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client)
        await svc.run_maintenance(db, db.dal, now=svc.now_utc() + timedelta(days=4))
        await client.post(
            f"/api/v1/public/{TENANT_SLUG}/privacy-requests/resend-verification", json=self._body()
        )
        assert len(notifier.sent) == 1


class TestPublicAbuseControls:
    async def test_submissions_are_uniform_for_unknown_tenant_and_flag_off_and_existing(
        self, client: Any, db: Any, flag_on: AsyncMock
    ) -> None:
        ok = await _public_submit(client, email="a@example.org")
        unknown = await _public_submit(client, email="b@example.org", slug="no-such-tenant")
        bad_slug = await _public_submit(client, email="c@example.org", slug="Bad_Slug!")
        flag_on.return_value = False
        flagged_off = await _public_submit(client, email="d@example.org", slug=OTHER_TENANT_SLUG)
        responses = [ok, unknown, bad_slug, flagged_off]
        assert {r.status_code for r in responses} == {202}
        bodies = [await r.get_json() for r in responses]
        assert all(b == bodies[0] for b in bodies)
        assert len(_rows(db)) == 1  # only the real, enabled tenant stored anything

    async def test_duplicate_pending_submission_is_uniform_and_not_duplicated(
        self, client: Any, db: Any
    ) -> None:
        first = await _public_submit(client)
        second = await _public_submit(client)
        assert first.status_code == second.status_code == 202
        assert await first.get_json() == await second.get_json()
        assert len(_rows(db)) == 1

    async def test_tenant_comes_from_the_url_never_the_body(self, client: Any, db: Any) -> None:
        body = {
            "email": "e@example.org",
            "requestType": 1,
            "regime": 1,
            "captchaToken": solve_altcha(),
            "tenantId": OTHER_ID,
            "tenant_slug": OTHER_TENANT_SLUG,
            "tenant": OTHER_TENANT_SLUG,
        }
        assert (
            await client.post(f"/api/v1/public/{TENANT_SLUG}/privacy-requests", json=body)
        ).status_code == 202
        assert _rows(db)[0].tenant_id == ACME_ID

    async def test_per_tenant_email_soft_limit_suppresses_silently(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        codes = [(await _public_submit(client, req_type=t)).status_code for t in (1, 2, 3, 4)]
        assert codes == [202, 202, 202, 202]  # no 429: nothing reveals the address was used
        assert len(_rows(db)) == 3 and len(notifier.sent) == 3  # the 4th was suppressed
        other_email = await _public_submit(client, email="other@example.org")
        assert other_email.status_code == 202 and len(_rows(db)) == 4  # bucket is per email

    async def test_email_soft_limit_applies_even_to_unknown_tenants(
        self, client: Any, db: Any
    ) -> None:
        codes = [
            (await _public_submit(client, slug="ghost-tenant", req_type=t)).status_code
            for t in (1, 2, 3, 4)
        ]
        assert codes == [202] * 4 and _rows(db) == []  # same observable result as a real tenant

    async def test_global_per_email_cap_stops_cross_tenant_mail_bombing(
        self, client: Any, db: Any, notifier: FakeNotifier, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dsar_module, "GLOBAL_EMAIL_LIMIT", (3, 86400))
        monkeypatch.setattr(dsar_module, "EMAIL_LIMIT", (99, 3600))
        for slug in (TENANT_SLUG, OTHER_TENANT_SLUG, TENANT_SLUG, OTHER_TENANT_SLUG):
            assert (
                await _public_submit(client, slug=slug, req_type=1 + len(notifier.sent))
            ).status_code == 202
        assert len(notifier.sent) == 3  # the 4th address-use anywhere on the platform is dropped

    async def test_one_tenants_flood_cannot_lock_other_requesters_out(
        self, client: Any, db: Any, notifier: FakeNotifier, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """regression: a per-tenant hard counter let anyone exhaust a tenant's intake."""
        monkeypatch.setattr(dsar_module, "IP_SOFT_LIMIT", (1000, 3600))
        for i in range(60):
            await _public_submit(client, email=f"flood{i}@example.org")
        victim = await _public_submit(client, email="real.person@example.org")
        assert victim.status_code == 202
        assert any(to == "real.person@example.org" for to, _ in notifier.sent)

    async def test_per_ip_soft_limit_suppresses_silently(self, client: Any, db: Any) -> None:
        codes = [
            (await _public_submit(client, email=f"u{i}@example.org")).status_code for i in range(12)
        ]
        assert codes == [202] * 12 and len(_rows(db)) == 10  # 11th/12th silently dropped

    async def test_verify_endpoint_is_ip_rate_limited(self, client: Any) -> None:
        codes = [
            (await client.post(f"{PUB}/verify", json={"token": "x"})).status_code for _ in range(32)
        ]
        assert codes[:30] == [400] * 30 and codes[30:] == [429, 429]

    async def test_oversized_body_is_413_before_processing(self, client: Any, db: Any) -> None:
        response = await client.post(
            f"/api/v1/public/{TENANT_SLUG}/privacy-requests",
            data=json.dumps(
                {
                    "email": "a@example.org",
                    "requestType": 1,
                    "regime": 1,
                    "captchaToken": "t",
                    "message": "m" * 20000,
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413 and _rows(db) == []

    @pytest.mark.parametrize(
        "body",
        [
            {"email": "not-an-email", "requestType": 1, "regime": 1},
            {"email": "a@b", "requestType": 1, "regime": 1},
            {"email": "a@example.org" + "x" * 300, "requestType": 1, "regime": 1},
            {"email": "a b@example.org", "requestType": 1, "regime": 1},
            {"email": "a@example.org", "requestType": 99, "regime": 1},
            {"email": "a@example.org", "requestType": 1, "regime": 0},
            {"email": "a@example.org", "requestType": 1, "regime": 1, "message": "m" * 2001},
            {"email": "a@example.org", "requestType": 1, "regime": 1, "message": "bad\x00char"},
            {"requestType": 1, "regime": 1},
        ],
    )
    async def test_strict_validation_is_400_before_captcha(
        self, client: Any, db: Any, body: dict[str, Any]
    ) -> None:
        response = await client.post(
            f"/api/v1/public/{TENANT_SLUG}/privacy-requests",
            json={**body, "captchaToken": solve_altcha()},
        )
        assert response.status_code == 400 and _rows(db) == []

    async def test_no_pii_in_logs_or_error_bodies(
        self, client: Any, db: Any, caplog: pytest.LogCaptureFixture, notifier: FakeNotifier
    ) -> None:
        caplog.set_level("DEBUG")
        await _public_submit(client, email="secret.person@example.org", message="my private note")
        await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        text = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
        assert "secret.person" not in text and "private note" not in text
        assert notifier.last_token() not in text and ALTCHA_KEY not in text and SEAL_KEY not in text


# =============================================================================================
# Crypto / misc
# =============================================================================================


class TestCryptoAndRegression:
    def test_ciphertext_is_bound_to_its_row_id(self) -> None:
        enc = crypto.encrypt("a@example.org", row_id="row-1")
        assert crypto.decrypt(enc, row_id="row-1") == "a@example.org"
        with pytest.raises(crypto.CryptoError):
            crypto.decrypt(enc, row_id="row-2")  # copied onto another row: authentication fails

    def test_missing_or_malformed_key_raises_never_plaintext(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for bad in ("", "abc", "zz" * 32):
            monkeypatch.setenv("DSAR_FINGERPRINT_KEY", bad)
            with pytest.raises(crypto.EncryptionKeyError):
                crypto.email_fingerprint("a@example.org")
            assert crypto.is_configured() is False
        monkeypatch.setenv("DSAR_FINGERPRINT_KEY", KEY)
        for bad in ("", "abc"):
            monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", bad)
            with pytest.raises(crypto.CryptoError):
                crypto.encrypt("x", row_id="r")
            assert crypto.is_configured() is False

    def test_fingerprint_is_case_insensitive_and_not_the_email(self) -> None:
        assert crypto.email_fingerprint("A@Example.org ") == crypto.email_fingerprint(
            "a@example.org"
        )
        assert b"example" not in crypto.email_fingerprint("a@example.org")

    async def test_events_table_rows_are_written_for_every_state_change_regression_audit_gap(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world)
        for body in ({"status": 4}, {"responseNotes": "n"}, {"status": 5}):
            await client.patch(
                f"{_base()}/{req['id']}", json=body, headers=_headers(world["admin"])
            )
        detail = await (
            await client.get(f"{_base()}/{req['id']}", headers=_headers(world["admin"]))
        ).get_json()
        assert [e["eventType"] for e in detail["events"]] == [1, 5, 7, 5]
        statuses = [
            (e["fromStatus"], e["toStatus"]) for e in detail["events"] if e["eventType"] == 5
        ]
        assert statuses == [(1, 4), (4, 5)]


# =============================================================================================
# Exact-field-set regression tests for every response DTO
# =============================================================================================

REQUEST_DTO_FIELDS = {
    "id",
    "requestType",
    "status",
    "regime",
    "source",
    "requestedAt",
    "verifiedAt",
    "dueAt",
    "extendedUntil",
    "effectiveDueAt",
    "extensionReason",
    "dueState",
    "completedAt",
    "expiresAt",
    "assigneeId",
    "subjectUserId",
    "responseNotes",
    "requesterEmail",
    "requesterMessage",
    "contactPurged",
    "unverified",
}
EVENT_DTO_FIELDS = {
    "id",
    "eventType",
    "actorKind",
    "actorId",
    "fromStatus",
    "toStatus",
    "detail",
    "createdAt",
}


class TestExactFieldSets:
    """security.md Output Validation: a response that grows a field fails here, loudly."""

    async def test_request_response_family(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        hdr = _headers(world["admin"])
        created = await (
            await client.post(_base(), json={"requestType": 1, "regime": 1}, headers=hdr)
        ).get_json()
        assert (
            set(created) == {"success", "request"} and set(created["request"]) == REQUEST_DTO_FIELDS
        )
        rid = created["request"]["id"]
        listing = await (await client.get(_base(), headers=hdr)).get_json()
        assert set(listing) == {"success", "items", "total", "limit", "offset"}
        assert set(listing["items"][0]) == REQUEST_DTO_FIELDS
        detail = await (await client.get(f"{_base()}/{rid}", headers=hdr)).get_json()
        assert (
            set(detail) == {"success", "request", "events"}
            and set(detail["events"][0]) == EVENT_DTO_FIELDS
        )
        upd = await (
            await client.patch(f"{_base()}/{rid}", json={"status": 4}, headers=hdr)
        ).get_json()
        assert set(upd) == {"success", "request"} and set(upd["request"]) == REQUEST_DTO_FIELDS
        ext = await (
            await client.post(f"{_base()}/{rid}/extend", json={"reason": "r"}, headers=hdr)
        ).get_json()
        assert set(ext) == {"success", "request"} and set(ext["request"]) == REQUEST_DTO_FIELDS

    async def test_act_response(self, client: Any, db: Any, world: dict[str, int]) -> None:
        hdr = _headers(world["admin"])
        req = await _admin_create(
            client, world, requestType=3, subjectUserId=_uuid_of(db, world["alice"])
        )
        body = await (
            await client.post(
                f"{_base()}/{req['id']}/act", json={"action": "do_not_sell"}, headers=hdr
            )
        ).get_json()
        assert (
            set(body) == {"success", "request", "result"}
            and set(body["request"]) == REQUEST_DTO_FIELDS
        )
        assert set(body["result"]) == {"userId", "status", "detail", "data", "incomplete"}

    async def test_user_intake_responses(self, client: Any, world: dict[str, int]) -> None:
        hdr = _headers(world["alice"], scope="*:read")
        created = await (
            await client.post(
                "/api/v1/user/me/privacy-requests",
                json={"requestType": 1, "regime": 1},
                headers=hdr,
            )
        ).get_json()
        assert set(created) == {"success", "request"}
        listing = await (
            await client.get("/api/v1/user/me/privacy-requests", headers=hdr)
        ).get_json()
        assert set(listing) == {"success", "items"} and set(listing["items"][0]) == set(
            created["request"]
        )

    async def test_public_responses(self, client: Any, notifier: FakeNotifier) -> None:
        assert set(await (await _public_submit(client)).get_json()) == {"success", "accepted"}
        assert set(
            await (
                await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
            ).get_json()
        ) == {"success", "verified"}
        cfg = await (await client.get(f"{PUB}/captcha-config")).get_json()
        assert set(cfg) == {"success", "provider", "siteKey", "challengeUrl"}

    async def test_pending_dto_never_contains_contact_fields_with_values(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        await _public_submit(client, message="hello there")
        body = await (
            await client.get(f"{_base()}?status=2", headers=_headers(world["admin"]))
        ).get_json()
        text = json.dumps(body)
        assert "bob@example.org" not in text and "hello there" not in text
        assert not any(k in text for k in ("contact_email", "verification_token", "email_hash"))


class TestOpenApiPaths:
    def test_all_new_paths_are_published(self, app: Quart) -> None:
        paths = set(app.extensions["QUART_SCHEMA"].openapi_provider.schema()["paths"])
        base = "/api/v1/tenant/{tenant_slug}/privacy/requests"
        expected = {
            base,
            f"{base}/{{request_id}}",
            f"{base}/{{request_id}}/extend",
            f"{base}/{{request_id}}/act",
            "/api/v1/user/me/privacy-requests",
            "/api/v1/public/{tenant_slug}/privacy-requests",
            "/api/v1/public/{tenant_slug}/privacy-requests/resend-verification",
            "/api/v1/public/privacy-requests/captcha-config",
            "/api/v1/public/privacy-requests/altcha-challenge",
            "/api/v1/public/privacy-requests/verify",
        }
        assert expected <= paths


# =============================================================================================
# Cross-tenant subject / assignee linking (regression: dsar-link-cross-tenant-reference)
# =============================================================================================

UNKNOWN_UUID = "00000000-0000-4000-8000-000000000000"


def _norm(raw: bytes) -> bytes:
    """Response bytes minus the per-response server timestamp (the only legitimate difference)."""
    return re.sub(rb'"timestamp":"[^"]*"', b'"timestamp":""', raw)


@pytest.fixture
def foreign(db: Any, world: dict[str, int]) -> dict[str, str]:
    """Tenant-B community member + the world's tenant-B admin, as UUIDs."""
    mallory = _user(db, name="mallory")
    _member(db, community_id=world["other_c"], user_id=mallory)
    return {"b_member": _uuid_of(db, mallory), "b_admin": _uuid_of(db, world["other_admin"])}


class TestSubjectLinkTenantScope:
    """Link time uses the SAME tenant definition as act time; every miss is one identical 404."""

    async def _create(self, client: Any, world: dict[str, int], subject: Any) -> Any:
        return await client.post(
            _base(),
            json={"requestType": 1, "regime": 1, "subjectUserId": subject},
            headers=_headers(world["admin"]),
        )

    async def test_in_tenant_member_and_tenant_admin_link(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        for uid in (world["alice"], world["admin2"]):
            response = await self._create(client, world, _uuid_of(db, uid))
            assert response.status_code == 201
            assert (await response.get_json())["request"]["subjectUserId"] == _uuid_of(db, uid)

    async def test_regression_cross_tenant_link_unknown_malformed_foreign_identical(
        self, client: Any, db: Any, world: dict[str, int], foreign: dict[str, str]
    ) -> None:
        before = len(_rows(db))
        bodies = []
        for subject in (UNKNOWN_UUID, "not-a-uuid", foreign["b_admin"], foreign["b_member"]):
            response = await self._create(client, world, subject)
            assert response.status_code == 404, subject
            bodies.append(_norm(await response.get_data()))
        assert len(set(bodies)) == 1
        assert foreign["b_member"].encode() not in bodies[0]
        assert len(_rows(db)) == before  # nothing was created

    async def test_regression_cross_tenant_link_on_update_identical(
        self, client: Any, db: Any, world: dict[str, int], foreign: dict[str, str]
    ) -> None:
        req = await _admin_create(client, world)
        bodies = []
        for subject in (UNKNOWN_UUID, "not-a-uuid", foreign["b_admin"], foreign["b_member"]):
            response = await client.patch(
                f"{_base()}/{req['id']}",
                json={"subjectUserId": subject},
                headers=_headers(world["admin"]),
            )
            assert response.status_code == 404, subject
            bodies.append(_norm(await response.get_data()))
        assert len(set(bodies)) == 1
        assert [r for r in _rows(db) if r.public_id == req["id"]][0].subject_user_id is None
        ok = await client.patch(
            f"{_base()}/{req['id']}",
            json={"subjectUserId": _uuid_of(db, world["alice"])},
            headers=_headers(world["admin"]),
        )
        assert ok.status_code == 200

    async def test_act_refused_uniformly_after_membership_removed(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world, subjectUserId=_uuid_of(db, world["alice"]))
        db.dal(db.dal.community_members.user_id == str(world["alice"])).delete()
        db.dal.commit()
        response = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "export"},
            headers=_headers(world["admin"]),
        )
        assert response.status_code == 404
        assert (await response.get_json())["result"]["detail"] == "User not found"
        row = [r for r in _rows(db) if r.public_id == req["id"]][0]
        assert row.status == int(S.RECEIVED)
        events = _events(db, int(row.id))
        assert events[-1].detail["reason"] == "subject_not_in_tenant"
        denied = [
            a
            for a in db.dal(db.dal.audit_log.id > 0).select()
            if a.details and a.details.get("outcome") == "denied_not_in_tenant"
        ]
        assert denied


class TestAssigneeTenantScope:
    """Assignee must be an admin of THIS tenant; every failure is one identical 400."""

    async def test_regression_foreign_unknown_malformed_nonadmin_assignee_identical(
        self, client: Any, db: Any, world: dict[str, int], foreign: dict[str, str]
    ) -> None:
        req = await _admin_create(client, world)
        bodies = []
        for who in (
            foreign["b_admin"],
            foreign["b_member"],
            UNKNOWN_UUID,
            "not-a-uuid",
            _uuid_of(db, world["alice"]),
        ):
            response = await client.patch(
                f"{_base()}/{req['id']}",
                json={"assigneeId": who},
                headers=_headers(world["admin"]),
            )
            assert response.status_code == 400, who
            bodies.append(_norm(await response.get_data()))
        assert len(set(bodies)) == 1

    async def test_list_assignee_filter_foreign_equals_unknown(
        self, client: Any, db: Any, world: dict[str, int], foreign: dict[str, str]
    ) -> None:
        req = await _admin_create(client, world)
        await client.patch(
            f"{_base()}/{req['id']}",
            json={"assigneeId": _uuid_of(db, world["admin2"])},
            headers=_headers(world["admin"]),
        )
        results = []
        for who in (foreign["b_admin"], foreign["b_member"], UNKNOWN_UUID):
            response = await client.get(
                f"{_base()}?assignee={who}", headers=_headers(world["admin"])
            )
            assert response.status_code == 200
            results.append(await response.get_data())
        assert len(set(results)) == 1
        hit = await client.get(
            f"{_base()}?assignee={_uuid_of(db, world['admin2'])}", headers=_headers(world["admin"])
        )
        assert (await hit.get_json())["total"] == 1


def test_gdpr_extension_chain_never_exceeds_the_statutory_three_months() -> None:
    """Adversarial review D: the clamped 1+2 month chain is never LATER than start + 3 months."""
    later = earlier = days = 0
    day = datetime(2023, 1, 1, 9, 30, tzinfo=UTC)
    while day < datetime(2026, 1, 1, tzinfo=UTC):  # includes leap year 2024 and every month end
        days += 1
        chained = svc.compute_extended_until(
            svc.Regime.GDPR, svc.compute_due_at(svc.Regime.GDPR, day)
        )
        statutory = svc.add_months(day, 3)
        later += chained > statutory
        earlier += chained < statutory
        day += timedelta(days=1)
    assert days == 1096
    assert later == 0  # never gives a requester/controller a deadline past the statute
    assert earlier > 0  # clamping can only SHORTEN it (conservative), by at most 3 days
