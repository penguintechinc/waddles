"""Regression tests for the external adversarial review of the DSAR inbox (areas A and C).

Each class names the finding it guards. Real JWTs, a real sqlite database (plus a hand-built
partial unique index mirroring alembic 0124; the real-Postgres proof lives in
`alembic/tests/test_0124_dsar_open_public_unique.py` and the PG integration file) and the real
service/blueprint code; only outbound mail is faked.
"""

# ruff: noqa: F811, F401, E501 - reused fixtures re-declared as args

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from blueprints.v1 import dsar_requests as dsar_module
from services import admin_data_privacy_service as admin_svc
from services import dsar_outbox
from services import dsar_service as svc
from tests.conftest import TENANT_SLUG
from tests.test_dsar_inbox import (  # noqa: F401 - fixtures reused as-is
    ACME_ID,
    PUB,
    FakeNotifier,
    _admin_create,
    _base,
    _env,
    _err_of,
    _events,
    _headers,
    _public_submit,
    _rows,
    _uuid_of,
    app,
    client,
    db,
    flag_on,
    module_db,
    notifier,
    raw_client,
    solve_altcha,
    world,
)

S = svc.RequestStatus
E = svc.EventType
INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_dsar_requests_open_public "
    "ON dsar_requests (tenant_id, contact_email_hash, request_type) "
    "WHERE status = 2 AND contact_email_hash IS NOT NULL"
)
RESEND_URL = f"/api/v1/public/{TENANT_SLUG}/privacy-requests/resend-verification"


@pytest.fixture
def open_unique_index(db: Any) -> Any:
    """The alembic 0124 partial unique index (sqlite supports partial indexes)."""
    db.dal.executesql(INDEX_SQL)
    db.dal.commit()
    yield
    db.dal.executesql("DROP INDEX IF EXISTS ux_dsar_requests_open_public")
    db.dal.commit()


async def _resend(client: Any, *, email: str, req_type: int) -> Any:
    return await client.post(
        RESEND_URL,
        json={"email": email, "requestType": req_type, "captchaToken": solve_altcha()},
    )


async def _verified_public(
    client: Any, notifier: FakeNotifier, *, email: str, req_type: int = 1
) -> None:
    await _public_submit(client, email=email, req_type=req_type)
    token = notifier.last_token()
    assert (await client.post(f"{PUB}/verify", json={"token": token})).status_code == 200


class TestAtomicIntake:
    async def test_enqueue_failure_rolls_the_request_back_and_a_retry_still_mails(
        self,
        client: Any,
        db: Any,
        notifier: FakeNotifier,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """regression: request/token and mail were separate writes; a crash lost the mail."""
        real = dsar_outbox.enqueue_in_transaction

        def boom(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("crash between request write and enqueue")

        monkeypatch.setattr(dsar_outbox, "enqueue_in_transaction", boom)
        failed = await _public_submit(client)
        assert failed.status_code >= 500
        assert _rows(db) == [] and notifier.sent == []  # nothing half-written
        monkeypatch.setattr(dsar_outbox, "enqueue_in_transaction", real)
        retry = await _public_submit(client)
        assert retry.status_code == 202
        assert len(_rows(db)) == 1 and len(notifier.sent) == 1  # the retry DOES get a mail

    async def test_request_row_and_outbox_row_commit_together(
        self, raw_client: Any, db: Any
    ) -> None:
        assert (await _public_submit(raw_client)).status_code == 202
        outbox = list(db.dal(db.dal.dsar_mail_outbox.id > 0).select())
        assert len(_rows(db)) == 1 and len(outbox) == 1
        assert int(outbox[0].request_id) == int(_rows(db)[0].id)

    async def test_resend_rotation_and_mail_are_one_transaction(
        self,
        client: Any,
        db: Any,
        notifier: FakeNotifier,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _public_submit(client)
        old_hash = _rows(db)[0].verification_token_hash
        real = dsar_outbox.enqueue_in_transaction

        def boom(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("crash")

        monkeypatch.setattr(dsar_outbox, "enqueue_in_transaction", boom)
        assert (await _resend(client, email="bob@example.org", req_type=1)).status_code >= 500
        # rotation rolled back with the failed enqueue: the mailed (old) token is still the live one
        assert _rows(db)[0].verification_token_hash == old_hash
        monkeypatch.setattr(dsar_outbox, "enqueue_in_transaction", real)
        assert (await _resend(client, email="bob@example.org", req_type=1)).status_code == 202
        assert _rows(db)[0].verification_token_hash != old_hash and len(notifier.sent) == 2


class TestDuplicateRaceAndPreSubmit:
    async def test_unique_index_rejects_a_second_open_request_and_is_detected(
        self, db: Any, open_unique_index: Any
    ) -> None:
        kwargs: dict[str, Any] = {
            "tenant_id": ACME_ID,
            "request_type": svc.RequestType.EXPORT,
            "regime": svc.Regime.GDPR,
            "email": "bob@example.org",
            "message": None,
        }
        svc.create_public_request_sync(db.dal, **kwargs)
        db.dal.commit()
        with pytest.raises(Exception) as caught:
            svc.create_public_request_sync(db.dal, **kwargs)
        db.dal.rollback()
        assert svc.is_integrity_violation(caught.value)
        assert len(_rows(db)) == 1

    async def test_lost_select_race_falls_back_to_rotate_and_still_202s_with_mail(
        self,
        client: Any,
        db: Any,
        notifier: FakeNotifier,
        open_unique_index: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two submits both see 'nothing pending'; the unique index makes the loser re-verify."""
        await _public_submit(client)
        real = svc.rotate_pending_public_sync
        calls = {"n": 0}

        def blind_first(dal: Any, **kw: Any) -> Any:
            calls["n"] += 1
            return None if calls["n"] == 1 else real(dal, **kw)

        monkeypatch.setattr(svc, "rotate_pending_public_sync", blind_first)
        second = await _public_submit(client)
        assert second.status_code == 202 and calls["n"] == 2
        assert len(_rows(db)) == 1  # never two open requests
        assert len(notifier.sent) == 2  # loser's submit still produced a mail for the owner

    async def test_pre_submitted_victim_email_still_gets_a_fresh_mail_on_their_own_submit(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        """An attacker pre-submits the victim's address; the victim's submit must still mail them."""
        await _public_submit(client, email="victim@example.org", message="attacker text")
        attacker_token = notifier.last_token()
        await _public_submit(client, email="victim@example.org")  # the real owner
        victim_token = notifier.last_token()
        assert len(_rows(db)) == 1 and len(notifier.sent) == 2
        assert victim_token != attacker_token
        # the rotation superseded the earlier token; the owner's fresh link works
        assert (
            await client.post(f"{PUB}/verify", json={"token": attacker_token})
        ).status_code == 400
        assert (await client.post(f"{PUB}/verify", json={"token": victim_token})).status_code == 200

    async def test_duplicate_and_fresh_submits_have_the_same_uniform_response(
        self, client: Any
    ) -> None:
        a = await _public_submit(client)
        b = await _public_submit(client)
        assert (a.status_code, (await a.get_json())) == (b.status_code, (await b.get_json()))


class TestResendRequiresMatchingType:
    async def test_resend_only_rotates_the_matching_pending_request(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        """regression: resend rotated the NEWEST pending request, whatever its type."""
        await _public_submit(client, email="v@example.org", req_type=1)  # victim's own export
        await _public_submit(client, email="v@example.org", req_type=2)  # attacker's ERASE (newest)
        sent_before = len(notifier.sent)
        assert (await _resend(client, email="v@example.org", req_type=1)).status_code == 202
        token = notifier.last_token()
        assert len(notifier.sent) == sent_before + 1
        assert (await client.post(f"{PUB}/verify", json={"token": token})).status_code == 200
        by_type = {int(r.request_type): int(r.status) for r in _rows(db)}
        assert by_type == {1: int(S.VERIFIED), 2: int(S.VERIFICATION_PENDING)}

    async def test_resend_for_a_type_with_nothing_pending_mails_nobody_and_is_uniform(
        self, client: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client, email="v@example.org", req_type=2)
        sent = len(notifier.sent)
        res = await _resend(client, email="v@example.org", req_type=1)
        assert res.status_code == 202 and len(notifier.sent) == sent

    async def test_missing_request_type_is_a_400(self, client: Any) -> None:
        res = await client.post(
            RESEND_URL, json={"email": "v@example.org", "captchaToken": solve_altcha()}
        )
        assert res.status_code == 400


class TestUserRoutesAuthz:
    URL = "/api/v1/user/me/privacy-requests"

    async def test_both_routes_require_the_self_service_scope(
        self, client: Any, world: dict[str, int]
    ) -> None:
        hdr = _headers(world["alice"], scope="")
        post = await client.post(self.URL, json={"requestType": 1, "regime": 1}, headers=hdr)
        get = await client.get(self.URL, headers=hdr)
        assert post.status_code == 403 and get.status_code == 403

    async def test_list_honours_the_rollout_flag(
        self, client: Any, world: dict[str, int], flag_on: Any
    ) -> None:
        flag_on.return_value = False
        res = await client.get(self.URL, headers=_headers(world["alice"], scope="*:read"))
        assert res.status_code == 404

    @pytest.mark.parametrize("bad_id", [0, -5])
    async def test_non_positive_user_ids_are_refused_and_match_no_null_rows(
        self, client: Any, db: Any, world: dict[str, int], bad_id: int
    ) -> None:
        await _admin_create(client, world)  # a row with subject_user_id NULL exists
        hdr = _headers(bad_id, scope="*:read")
        post = await client.post(self.URL, json={"requestType": 1, "regime": 1}, headers=hdr)
        get = await client.get(self.URL, headers=hdr)
        assert post.status_code in (401, 403) and get.status_code in (401, 403)
        assert all(r.subject_user_id != bad_id for r in _rows(db))  # NULL rows never match


class TestSubjectLinkAuthz:
    async def test_logged_in_requests_subject_is_never_retargeted(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        mine = await client.post(
            "/api/v1/user/me/privacy-requests",
            json={"requestType": 2, "regime": 1},
            headers=_headers(world["alice"], scope="*:read"),
        )
        req_id = (await mine.get_json())["request"]["id"]
        res = await client.patch(
            f"{_base()}/{req_id}",
            json={
                "subjectUserId": _uuid_of(db, world["admin2"]),
                "subjectOverrideReason": "trust me",
            },
            headers=_headers(world["admin"]),
        )
        assert res.status_code == 409
        assert (await res.get_json())["error"]["code"] == "DSAR_SUBJECT_LOCKED"
        assert _rows(db)[0].subject_user_id == world["alice"]

    async def test_changing_a_linked_subject_needs_an_audited_override(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world, subjectUserId=_uuid_of(db, world["alice"]))
        url = f"{_base()}/{req['id']}"
        target = _uuid_of(db, world["admin2"])
        refused = await client.patch(
            url, json={"subjectUserId": target}, headers=_headers(world["admin"])
        )
        assert refused.status_code == 409
        assert (await refused.get_json())["error"]["code"] == "DSAR_SUBJECT_OVERRIDE_REQUIRED"
        ok = await client.patch(
            url,
            json={"subjectUserId": target, "subjectOverrideReason": "linked wrong account"},
            headers=_headers(world["admin"]),
        )
        assert ok.status_code == 200
        link = [e for e in _events(db) if e.event_type == int(E.SUBJECT_LINKED)][-1]
        assert link.detail == {"override": True, "reason": "linked wrong account"}

    async def test_relinking_the_same_subject_is_a_silent_noop(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world, subjectUserId=_uuid_of(db, world["alice"]))
        before = len(_events(db))
        res = await client.patch(
            f"{_base()}/{req['id']}",
            json={"subjectUserId": _uuid_of(db, world["alice"])},
            headers=_headers(world["admin"]),
        )
        assert res.status_code == 200 and len(_events(db)) == before

    async def test_linking_an_unlinked_public_request_needs_no_override(
        self, client: Any, db: Any, world: dict[str, int], notifier: FakeNotifier
    ) -> None:
        await _verified_public(client, notifier, email="alice@example.com")
        req_id = str(_rows(db)[0].public_id)
        res = await client.patch(
            f"{_base()}/{req_id}",
            json={"subjectUserId": _uuid_of(db, world["alice"])},
            headers=_headers(world["admin"]),
        )
        assert res.status_code == 200


class TestEraseOnPublicRequestNeedsEmailMatchOrOverride:
    async def _public_erase(
        self, client: Any, db: Any, world: dict[str, int], notifier: FakeNotifier, email: str
    ) -> str:
        await _verified_public(client, notifier, email=email, req_type=2)
        req_id = str(_rows(db)[0].public_id)
        link = await client.patch(
            f"{_base()}/{req_id}",
            json={"subjectUserId": _uuid_of(db, world["alice"])},
            headers=_headers(world["admin"]),
        )
        assert link.status_code == 200
        return req_id

    async def test_mismatched_email_refuses_erase_and_touches_nothing(
        self, client: Any, db: Any, world: dict[str, int], notifier: FakeNotifier
    ) -> None:
        req_id = await self._public_erase(client, db, world, notifier, "attacker@example.org")
        res = await client.post(
            f"{_base()}/{req_id}/act",
            json={"action": "erase", "confirm": True},
            headers=_headers(world["admin"]),
        )
        body = await res.get_json()
        assert res.status_code == 422 and body["error"]["code"] == "DSAR_SUBJECT_EMAIL_MISMATCH"
        assert "alice@example.com" not in str(body) and "attacker" not in str(
            body
        )  # no emails leak
        assert db.dal.hub_users[world["alice"]].email == "alice@example.com"  # not erased
        assert _rows(db)[0].status == int(S.VERIFIED)

    async def test_matching_email_erases_without_override(
        self, client: Any, db: Any, world: dict[str, int], notifier: FakeNotifier
    ) -> None:
        req_id = await self._public_erase(client, db, world, notifier, "Alice@Example.com")
        res = await client.post(
            f"{_base()}/{req_id}/act",
            json={"action": "erase", "confirm": True},
            headers=_headers(world["admin"]),
        )
        assert res.status_code == 200
        assert db.dal.hub_users[world["alice"]].email != "alice@example.com"

    async def test_explicit_override_with_reason_is_recorded(
        self, client: Any, db: Any, world: dict[str, int], notifier: FakeNotifier
    ) -> None:
        req_id = await self._public_erase(client, db, world, notifier, "other.mailbox@example.org")
        res = await client.post(
            f"{_base()}/{req_id}/act",
            json={"action": "erase", "confirm": True, "overrideReason": "ID verified by phone"},
            headers=_headers(world["admin"]),
        )
        assert res.status_code == 200
        acted = [e for e in _events(db) if e.event_type == int(E.ACTION_EXECUTED)][-1]
        assert acted.detail["email_override"] is True
        assert acted.detail["override_reason"] == "ID verified by phone"

    async def test_logged_in_and_admin_requests_are_not_subject_to_the_email_rule(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(
            client, world, requestType=2, subjectUserId=_uuid_of(db, world["alice"])
        )
        res = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "erase", "confirm": True},
            headers=_headers(world["admin"]),
        )
        assert res.status_code == 200


class TestEveryActActionOnPublicRequestNeedsEmailMatchOrOverride:
    """regression: only ERASE was email-gated; export/do-not-sell on a mismatched link were open."""

    @pytest.mark.parametrize(("req_type", "action"), [(1, "export"), (3, "do_not_sell")])
    async def test_mismatched_email_refuses_and_override_is_audited(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        notifier: FakeNotifier,
        req_type: int,
        action: str,
    ) -> None:
        await _verified_public(client, notifier, email="attacker@example.org", req_type=req_type)
        req_id = str(_rows(db)[0].public_id)
        link = await client.patch(
            f"{_base()}/{req_id}",
            json={"subjectUserId": _uuid_of(db, world["alice"])},
            headers=_headers(world["admin"]),
        )
        assert link.status_code == 200
        res = await client.post(
            f"{_base()}/{req_id}/act", json={"action": action}, headers=_headers(world["admin"])
        )
        body = await res.get_json()
        assert res.status_code == 422 and body["error"]["code"] == "DSAR_SUBJECT_EMAIL_MISMATCH"
        assert "alice@example.com" not in str(body)  # no account data in the refusal
        assert _rows(db)[0].status == int(S.VERIFIED)
        ok = await client.post(
            f"{_base()}/{req_id}/act",
            json={"action": action, "overrideReason": "ID verified by phone"},
            headers=_headers(world["admin"]),
        )
        assert ok.status_code == 200
        acted = [e for e in _events(db) if e.event_type == int(E.ACTION_EXECUTED)][-1]
        assert acted.detail["email_override"] is True
        assert acted.detail["override_reason"] == "ID verified by phone"

    async def test_matching_email_exports_without_override(
        self, client: Any, db: Any, world: dict[str, int], notifier: FakeNotifier
    ) -> None:
        await _verified_public(client, notifier, email="Alice@Example.com", req_type=1)
        req_id = str(_rows(db)[0].public_id)
        await client.patch(
            f"{_base()}/{req_id}",
            json={"subjectUserId": _uuid_of(db, world["alice"])},
            headers=_headers(world["admin"]),
        )
        res = await client.post(
            f"{_base()}/{req_id}/act", json={"action": "export"}, headers=_headers(world["admin"])
        )
        assert res.status_code == 200


class TestResubmitContentIsDraftUntilLatestTokenConsumed:
    """regression: a resubmit rewrote the committed message/regime of a pending request.

    Only the mailbox owner redeeming the LATEST token may commit new content; an unauthenticated
    resubmit alone changes nothing that admins or the row's committed columns hold.
    """

    @staticmethod
    def _plain(row: Any) -> str | None:
        if not row.message_enc:
            return None
        return str(svc.crypto.decrypt(row.message_enc, row_id=row.public_id))

    async def test_resubmit_leaves_committed_content_and_stores_a_draft(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client, email="v@example.org", message="victim words", regime=1)
        await _public_submit(client, email="v@example.org", message="attacker text", regime=2)
        row = _rows(db)[0]
        assert len(_rows(db)) == 1
        assert self._plain(row) == "victim words" and int(row.regime) == 1  # unchanged
        assert int(row.pending_regime) == 2
        assert svc.crypto.decrypt(row.pending_message_enc, row_id=row.public_id) == "attacker text"
        assert not [e for e in _events(db) if e.event_type == int(E.CONTENT_REPLACED)]
        assert svc.decrypt_contact(row) == (None, None)  # pending: admins see nothing

    async def test_old_token_is_dead_and_latest_token_commits_the_draft(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client, email="v@example.org", message="victim words", regime=1)
        first = notifier.last_token()
        await _public_submit(client, email="v@example.org", message="second text", regime=2)
        latest = notifier.last_token()
        assert (await client.post(f"{PUB}/verify", json={"token": first})).status_code == 400
        row = _rows(db)[0]
        assert self._plain(row) == "victim words" and int(row.status) == int(S.VERIFICATION_PENDING)
        assert (await client.post(f"{PUB}/verify", json={"token": latest})).status_code == 200
        row = _rows(db)[0]
        assert self._plain(row) == "second text" and int(row.regime) == 2
        assert row.pending_message_enc is None and row.pending_regime is None
        evt = [e for e in _events(db) if e.event_type == int(E.CONTENT_REPLACED)]
        assert len(evt) == 1
        assert evt[0].detail == {"fields": ["message", "regime"], "message_present": True}
        assert "second text" not in str(evt[0].detail)

    async def test_draft_without_message_clears_the_committed_message_on_consume(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client, email="v@example.org", message="planted")
        await _public_submit(client, email="v@example.org")
        assert self._plain(_rows(db)[0]) == "planted"  # not yet committed
        await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        row = _rows(db)[0]
        assert row.message_enc is None and svc.decrypt_contact(row)[1] is None

    async def test_plain_verify_without_resubmit_is_unchanged_and_emits_no_content_event(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client, email="v@example.org", message="orig", regime=1)
        await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        row = _rows(db)[0]
        assert self._plain(row) == "orig" and int(row.status) == int(S.VERIFIED)
        assert not [e for e in _events(db) if e.event_type == int(E.CONTENT_REPLACED)]

    async def test_identical_resubmit_commits_without_a_content_event(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client, email="v@example.org", message="same", regime=1)
        await _public_submit(client, email="v@example.org", message="same", regime=1)
        await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        assert not [e for e in _events(db) if e.event_type == int(E.CONTENT_REPLACED)]
        assert self._plain(_rows(db)[0]) == "same"

    async def test_bare_resend_clears_the_draft_so_it_never_commits(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        """regression: a resend kept an attacker's draft that the owner's resend link then committed."""
        await _public_submit(client, email="v@example.org", message="orig", regime=1)
        await _public_submit(client, email="v@example.org", message="attacker", regime=2)
        assert (await _resend(client, email="v@example.org", req_type=1)).status_code == 202
        row = _rows(db)[0]
        assert self._plain(row) == "orig"
        assert row.pending_message_enc is None and row.pending_regime is None
        assert row.pending_token_hash is None
        assert (
            await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        ).status_code == 200
        row = _rows(db)[0]
        assert self._plain(row) == "orig" and int(row.regime) == 1
        assert not [e for e in _events(db) if e.event_type == int(E.CONTENT_REPLACED)]

    async def test_draft_is_bound_to_the_token_it_was_created_under(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client, email="v@example.org", message="orig", regime=1)
        await _public_submit(client, email="v@example.org", message="draft", regime=2)
        row = _rows(db)[0]
        token = notifier.last_token()
        assert row.pending_token_hash == svc.crypto.hash_token(token).hex()
        assert row.pending_token_hash == row.verification_token_hash

    async def test_draft_with_a_different_token_hash_is_discarded_not_committed(
        self, client: Any, db: Any, notifier: FakeNotifier
    ) -> None:
        await _public_submit(client, email="v@example.org", message="orig", regime=1)
        await _public_submit(client, email="v@example.org", message="draft", regime=2)
        db.dal(db.dal.dsar_requests.id == _rows(db)[0].id).update(pending_token_hash="0" * 64)
        db.dal.commit()
        assert (
            await client.post(f"{PUB}/verify", json={"token": notifier.last_token()})
        ).status_code == 200
        row = _rows(db)[0]
        assert self._plain(row) == "orig" and int(row.regime) == 1
        assert row.pending_message_enc is None and row.pending_regime is None
        assert row.pending_token_hash is None
        assert not [e for e in _events(db) if e.event_type == int(E.CONTENT_REPLACED)]

    async def test_closing_an_unverified_request_purges_the_draft(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        await _public_submit(client, email="v@example.org", message="orig")
        await _public_submit(client, email="v@example.org", message="draft", regime=2)
        res = await client.patch(
            f"{_base()}/{_rows(db)[0].public_id}",
            json={"status": int(S.REJECTED)},
            headers=_headers(world["admin"]),
        )
        assert res.status_code == 200
        row = _rows(db)[0]
        assert row.pending_message_enc is None and row.pending_regime is None
        assert row.pending_token_hash is None
        assert row.message_enc is None


class TestConcurrency:
    async def test_two_admins_acting_at_once_execute_exactly_once(
        self, client: Any, db: Any, world: dict[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = await _admin_create(
            client, world, requestType=2, subjectUserId=_uuid_of(db, world["alice"])
        )
        runs = {"n": 0}
        real = admin_svc.run_single_dsar

        async def counting(*a: Any, **k: Any) -> Any:
            runs["n"] += 1
            await asyncio.sleep(0.05)  # widen the window between the claim and the result
            return await real(*a, **k)

        monkeypatch.setattr(admin_svc, "run_single_dsar", counting)
        url = f"{_base()}/{req['id']}/act"
        body = {"action": "erase", "confirm": True}
        a, b = await asyncio.gather(
            client.post(url, json=body, headers=_headers(world["admin"])),
            client.post(url, json=body, headers=_headers(world["admin2"])),
        )
        assert sorted((a.status_code, b.status_code)) == [200, 409]
        assert runs["n"] == 1
        assert [e.event_type for e in _events(db)].count(int(E.ACTION_EXECUTED)) == 1

    async def test_stale_update_loses_with_409_and_writes_nothing(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world)
        row = await svc.get_request(db, db.dal, tenant_id=ACME_ID, public_id=req["id"])
        other = await client.patch(
            f"{_base()}/{req['id']}",
            json={"responseNotes": "admin2 was first"},
            headers=_headers(world["admin2"]),
        )
        assert other.status_code == 200
        with pytest.raises(svc.ApiError) as exc:
            await svc.update_request(
                db,
                db.dal,
                tenant_id=ACME_ID,
                row=row,
                admin_user_id=world["admin"],
                status=int(S.REJECTED),
            )
        assert exc.value.status_code == 409 and exc.value.code == "DSAR_CONFLICT"
        current = _rows(db)[0]
        assert current.status == int(S.RECEIVED) and current.response_notes == "admin2 was first"

    async def test_stale_extension_and_status_race_are_conflicts(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world)
        row = await svc.get_request(db, db.dal, tenant_id=ACME_ID, public_id=req["id"])
        await client.patch(
            f"{_base()}/{req['id']}",
            json={"status": int(S.IN_PROGRESS)},
            headers=_headers(world["admin2"]),
        )
        with pytest.raises(svc.ApiError) as exc:
            await svc.extend_request(
                db, db.dal, tenant_id=ACME_ID, row=row, admin_user_id=world["admin"], reason="why"
            )
        assert exc.value.code == "DSAR_CONFLICT"
        assert _rows(db)[0].extended_until is None


class TestPartialAndFailedActResults:
    async def _export_req(self, client: Any, db: Any, world: dict[str, int]) -> dict[str, Any]:
        return await _admin_create(
            client, world, requestType=1, subjectUserId=_uuid_of(db, world["alice"])
        )

    async def test_partial_export_keeps_the_request_open_and_reports_not_success(
        self, client: Any, db: Any, world: dict[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        req = await self._export_req(client, db, world)

        async def partial(*_a: Any, **_k: Any) -> Any:
            return admin_svc.DsarResult(
                user_uuid=_uuid_of(db, world["alice"]),
                status=admin_svc.DsarStatus.PARTIAL,
                detail="Export incomplete",
                data={},
                incomplete=[{"source": "x", "reason": "failed"}],
            )

        monkeypatch.setattr(admin_svc, "run_single_dsar", partial)
        res = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "export"},
            headers=_headers(world["admin"]),
        )
        body = await res.get_json()
        assert res.status_code == 200 and body["success"] is False
        assert body["result"]["status"] == "partial" and body["result"]["incomplete"]
        assert body["request"]["status"] == int(S.IN_PROGRESS)  # surfaced, never completed
        assert body["request"]["completedAt"] is None
        acted = [e for e in _events(db) if e.event_type == int(E.ACTION_EXECUTED)][-1]
        assert acted.detail["outcome"] == "partial"

    @pytest.mark.parametrize("failed", ["failed", "conflict", "forbidden", "not_found"])
    async def test_failed_erase_never_completes_the_request(
        self,
        client: Any,
        db: Any,
        world: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
        failed: str,
    ) -> None:
        req = await _admin_create(
            client, world, requestType=2, subjectUserId=_uuid_of(db, world["alice"])
        )

        async def bad(*_a: Any, **_k: Any) -> Any:
            return admin_svc.DsarResult(
                user_uuid=_uuid_of(db, world["alice"]), status=admin_svc.DsarStatus(failed)
            )

        monkeypatch.setattr(admin_svc, "run_single_dsar", bad)
        res = await client.post(
            f"{_base()}/{req['id']}/act",
            json={"action": "erase", "confirm": True},
            headers=_headers(world["admin"]),
        )
        body = await res.get_json()
        assert body["success"] is False and body["request"]["status"] == int(S.RECEIVED)
        assert body["request"]["completedAt"] is None


class TestRequesterVisibleExtensionAndNotesPurge:
    async def test_requester_sees_the_extension_without_internal_notes(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        mine = await client.post(
            "/api/v1/user/me/privacy-requests",
            json={"requestType": 1, "regime": 1},
            headers=_headers(world["alice"], scope="*:read"),
        )
        req_id = (await mine.get_json())["request"]["id"]
        await client.patch(
            f"{_base()}/{req_id}",
            json={"responseNotes": "INTERNAL: suspect fraud"},
            headers=_headers(world["admin"]),
        )
        ext = await client.post(
            f"{_base()}/{req_id}/extend",
            json={"reason": "complex: three systems, ask legal"},
            headers=_headers(world["admin"]),
        )
        assert ext.status_code == 200
        listing = await client.get(
            "/api/v1/user/me/privacy-requests", headers=_headers(world["alice"], scope="*:read")
        )
        item = (await listing.get_json())["items"][0]
        assert item["extendedUntil"] and item["extensionReason"] == "statutory_extension"
        assert "INTERNAL" not in str(item) and "legal" not in str(item)

    async def test_admin_notes_are_purged_with_the_contact_data(
        self, client: Any, db: Any, world: dict[str, int]
    ) -> None:
        req = await _admin_create(client, world, requesterEmail="req@example.org")
        await client.patch(
            f"{_base()}/{req['id']}",
            json={"responseNotes": "called req@example.org, mentioned diagnosis"},
            headers=_headers(world["admin"]),
        )
        await client.patch(
            f"{_base()}/{req['id']}",
            json={"status": int(S.COMPLETED)},
            headers=_headers(world["admin"]),
        )
        assert _rows(db)[0].response_notes  # still there during the retention window
        await svc.run_maintenance(db, db.dal, now=svc.now_utc() + timedelta(days=31))
        row = _rows(db)[0]
        assert row.contact_purged_at is not None and row.response_notes is None
