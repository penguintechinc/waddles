"""Art. 17 end to end: `anonymize_user_data()` through the registry, benign AND banned.

Real pydal + real SQL on a sqlite schema that holds every registry table (built from the registry,
`tests/erasure_support.py`), driven through the production entry point. The properties:

- **benign = full delete**: the subject's rows in tables the old hand-listed erasure never knew
  (activity, SSO subject, economy ledger/balances, platform-keyed loyalty, labels, tokens, ...) are
  gone; another user's rows are untouched; the ledger reports non-zero counts for them;
- **banned = hash retained, nothing else**: exactly one `suppression_records` row is written and the
  SAME registry deletes run -- the suppression-only design (#804) is preserved, not weakened;
- **economy / KV / SeaweedFS / `sso_identities.subject`** are each erased on the production path;
- **external stores fail loud**: a failing object-store/KV delete aborts the whole erasure (database
  unchanged, `failed` ledger row) and the retry completes -- never "completed" over leftovers;
- **no tier gate**: the same erasure runs for a Free-tier tenant (there is no tier input at all);
- logs/spans from the registry carry table names and counts only, never a subject value.
"""

# ruff: noqa: F811, S608

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from services import data_privacy_service as privacy
from services import erasure_executor as executor
from services import suppression_keys as keys
from tests.erasure_support import FakeExternal, create_missing_registry_tables
from tests.test_ban_suppression import _banned_member, _community, _tenant
from tests.test_data_privacy_erasure import (  # noqa: F401 - fixtures re-exported for this module
    TRIGGER_MESSAGE,
    _chat,
    _ledger,
    _link_identity,
    _member,
    _now,
    _user,
    block_anonymization,
    db,
    module_db,
)

MASTER_HEX = "ab" * 32
HUB_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
MEMBER_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_UUID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PUID = "123456789012345678"
AVATAR = "https://assets.example/waddlebot-assets/avatars/mine.png"
BANNER = "https://assets.example/waddlebot-assets/avatars/mine-banner.png"
SUBJECT_SENTINEL = "idp-subject-sentinel-7f3a"


@pytest.fixture(scope="module", autouse=True)
def registry_schema(module_db: Any) -> Iterator[None]:
    """Give the shared erasure DB every registry table, plus the SSO `subject` column."""
    dal = module_db.dal
    create_missing_registry_tables(dal)
    columns = executor.table_catalog(dal)["sso_identities"]
    if "subject" not in columns:
        dal.executesql('ALTER TABLE "sso_identities" ADD COLUMN "subject" TEXT')
        dal.commit()
    yield


@pytest.fixture(autouse=True)
def master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform KEK a banned erasure needs."""
    monkeypatch.setenv(keys.ENV_CURRENT, MASTER_HEX)
    monkeypatch.delenv(keys.ENV_PREVIOUS, raising=False)


def _table(db: Any, name: str) -> Any:
    return executor._alias(db.dal, name)


def _sso_insert(db: Any, hub_user_id: int, subject: str) -> None:
    db.dal.executesql(
        "INSERT INTO sso_identities (hub_user_id, subject) VALUES (?, ?)", [hub_user_id, subject]
    )


def _sso_count(db: Any, **where: Any) -> int:
    column, value = next(iter(where.items()))
    [[n]] = db.dal.executesql(f"SELECT COUNT(*) FROM sso_identities WHERE {column} = ?", [value])
    db.dal.commit()
    return int(n)


def _count(db: Any, name: str, **where: Any) -> int:
    if name == "suppression_records":
        n = db.dal(db.dal.suppression_records.id > 0).count()
        db.dal.commit()
        return int(n)
    alias = _table(db, name)
    query = alias.id > 0
    for column, value in where.items():
        query &= alias[column] == value
    n = db.dal(query).count()
    db.dal.commit()
    return int(n)


def _seed(db: Any, *, user_id: int, hub_uuid: str, platform_user_id: str, sso_subject: str) -> None:
    """One row per class of table the old erasure never reached, all keyed to `user_id`."""
    dal = db.dal
    dal(dal.hub_users.id == user_id).update(uuid=hub_uuid)
    _table(db, "activity_logs").insert(user_id=user_id)
    _table(db, "user_labels").insert(user_id=user_id, created_by=user_id)
    _table(db, "user_access_tokens").insert(user_id=user_id)
    _table(db, "golden_ticket_holders").insert(user_id=user_id)
    _sso_insert(db, user_id, sso_subject)
    _table(db, "economy_balances").insert(user_uuid=hub_uuid)
    _table(db, "economy_ledger").insert(user_uuid=hub_uuid)
    _table(db, "economy_ledger").insert(user_uuid=OTHER_UUID, counterparty_uuid=hub_uuid)
    _table(db, "loyalty_balances").insert(platform="discord", platform_user_id=platform_user_id)
    _table(db, "bookings").insert(host_user_id=user_id)
    _table(db, "bookings").insert(
        host_user_id=999, guest_user_id=user_id, guest_name="Guest Name", guest_email="g@x.io"
    )
    _table(db, "announcements").insert(created_by=user_id, created_by_name="Subject Name")
    dal.hub_user_profiles.insert(hub_user_id=user_id, custom_avatar_url=AVATAR, banner_url=BANNER)
    dal.commit()


def _seed_decoys(db: Any) -> None:
    """Another user's rows in the same tables -- erasure must never touch them."""
    _table(db, "activity_logs").insert(user_id=555)
    _sso_insert(db, 555, "someone-elses-subject")
    _table(db, "economy_balances").insert(user_uuid=OTHER_UUID)
    _table(db, "loyalty_balances").insert(platform="discord", platform_user_id="decoy-id")
    _table(db, "user_labels").insert(user_id=555)
    db.dal.commit()


async def _erase(db: Any, user_id: int, external: FakeExternal | None = None) -> FakeExternal:
    ext = external or FakeExternal()
    await privacy.anonymize_user_data(
        db, db.dal, user_id=user_id, email="subject@example.com", external_stores=ext
    )
    return ext


class TestBenignErasure:
    """A user with no ban leaves nothing behind in ANY user-keyed table."""

    async def test_every_seeded_table_is_cleared_and_decoys_survive(self, db: Any) -> None:
        user = _user(db, name="benign")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )
        _seed_decoys(db)

        await _erase(db, user)

        for table in (
            "activity_logs",
            "user_labels",
            "user_access_tokens",
            "golden_ticket_holders",
        ):
            assert _count(db, table, user_id=user) == 0, table
        assert _sso_count(db, hub_user_id=user) == 0
        assert _count(db, "economy_balances", user_uuid=HUB_UUID) == 0
        assert _count(db, "economy_ledger", user_uuid=HUB_UUID) == 0
        assert _count(db, "loyalty_balances", platform_user_id=PUID) == 0
        assert _count(db, "bookings", host_user_id=user) == 0
        # decoys untouched
        assert _count(db, "activity_logs", user_id=555) == 1
        assert _sso_count(db, hub_user_id=555) == 1
        assert _count(db, "economy_balances", user_uuid=OTHER_UUID) == 1
        assert _count(db, "loyalty_balances", platform_user_id="decoy-id") == 1
        assert _count(db, "user_labels", user_id=555) == 1

    async def test_the_sso_subject_is_gone_everywhere(self, db: Any) -> None:
        user = _user(db, name="ssouser")
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )

        await _erase(db, user)

        assert _sso_count(db, subject=SUBJECT_SENTINEL) == 0

    async def test_economy_ledger_other_side_no_longer_names_the_subject(self, db: Any) -> None:
        user = _user(db, name="econ")
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )

        await _erase(db, user)

        [other_side] = db.dal(_table(db, "economy_ledger").user_uuid == OTHER_UUID).select()
        db.dal.commit()
        assert other_side.counterparty_uuid is None

    async def test_rows_kept_for_others_have_the_subjects_pii_blanked(self, db: Any) -> None:
        user = _user(db, name="scrubbed")
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )

        await _erase(db, user)

        guest = db.dal(_table(db, "bookings").guest_user_id == user).select().first()
        announcement = db.dal(_table(db, "announcements").created_by == user).select().first()
        db.dal.commit()
        assert (guest.guest_name, guest.guest_email) == (executor.registry.ERASED_PLACEHOLDER, None)
        assert announcement.created_by_name is None
        assert announcement.created_by == user  # pseudonymous reference to the anonymized row kept

    async def test_ledger_reports_the_new_tables_and_scrubs_are_not_deletes(self, db: Any) -> None:
        user = _user(db, name="counts")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )

        await _erase(db, user)

        [row] = _ledger(db, user)
        scope = dict(row.deletion_scope)
        assert row.status == "completed"
        assert scope["activity_logs"] == 1
        assert scope["sso_identities"] == 1
        assert scope["economy_ledger"] == 1
        assert scope["economy_balances"] == 1
        assert scope["loyalty_balances"] == 1
        assert scope[executor.scrubbed_key("bookings")] == 1
        assert scope["suppression_records_written"] == 0
        assert privacy._rows_deleted(scope) == sum(
            v for k, v in scope.items() if privacy._is_delete_count_key(k)
        )
        zeros = [k for k in executor.registry.executed_tables() if scope.get(k) == 0]
        assert not zeros  # only non-zero registry counts are written to the ledger

    async def test_no_suppression_record_is_written_for_a_benign_user(self, db: Any) -> None:
        user = _user(db, name="nokeep")
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )

        await _erase(db, user)

        assert _count(db, "suppression_records") == 0


class TestBannedErasure:
    """Suppression-only: the banned user is erased IDENTICALLY, plus exactly one keyed code."""

    async def test_banned_user_is_fully_erased_and_only_the_hash_is_retained(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        community = _community(db, tenant_id=tenant)
        user = _user(db, name="banned")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=community, user_id=user)
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )
        _seed_decoys(db)

        await _erase(db, user)

        # identical deletes to the benign path
        assert _count(db, "activity_logs", user_id=user) == 0
        assert _sso_count(db, hub_user_id=user) == 0
        assert _count(db, "economy_ledger", user_uuid=HUB_UUID) == 0
        assert _count(db, "loyalty_balances", platform_user_id=PUID) == 0
        assert _count(db, "activity_logs", user_id=555) == 1
        # ... and the ONE thing kept: a keyed one-way code, never the platform id
        [record] = db.dal(db.dal.suppression_records.id > 0).select()
        db.dal.commit()
        expected = keys.current_code(keys.PlatformKekKeySource(), tenant, "discord", PUID)
        assert record.hmac == expected.hmac
        assert PUID not in repr(record.as_dict())
        [row] = _ledger(db, user)
        assert row.deletion_scope["suppression_records_written"] == 1
        assert row.deletion_scope["activity_logs"] == 1

    async def test_a_missing_master_key_still_aborts_a_banned_erasure_whole(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)
        tenant = _tenant(db, "acme")
        user = _user(db, name="nokey")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=_community(db, tenant_id=tenant), user_id=user)
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )

        with pytest.raises(keys.SuppressionKeyError):
            await _erase(db, user)

        assert _count(db, "activity_logs", user_id=user) == 1  # nothing erased without suppressing


class TestExternalStores:
    """SeaweedFS objects + Valkey KV: collected before the rows go, failures abort the erasure."""

    async def test_object_urls_and_uuid_tokens_reach_the_external_stores(self, db: Any) -> None:
        user = _user(db, name="external")
        member = _member(
            db,
            community_id=_community(db, tenant_id=_tenant(db, "acme")),
            user_id=user,
            platform_user_id=PUID,
        )
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        db.dal(db.dal.community_members.id == member).update(user_uuid=MEMBER_UUID)
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )
        _chat(db, user_id=user, community_id=1, text="hello")

        ext = FakeExternal(counts={"objects_deleted": 2, "kv_keys_deleted": 3})
        await _erase(db, user, ext)

        [(urls, uuids)] = ext.calls
        assert (
            AVATAR in urls and BANNER in urls
        )  # read from hub_user_profiles BEFORE it was deleted
        assert "https://cdn.example/avatar.png" in urls  # chat sender avatar (filtered later)
        assert {HUB_UUID, MEMBER_UUID} <= set(uuids)
        [row] = _ledger(db, user)
        assert row.deletion_scope["objects_deleted"] == 2
        assert row.deletion_scope["kv_keys_deleted"] == 3

    async def test_a_failing_external_delete_aborts_the_whole_erasure(self, db: Any) -> None:
        user = _user(db, name="extfail")
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )

        with pytest.raises(ConnectionError):
            await _erase(db, user, FakeExternal(fail_with=ConnectionError("valkey down")))

        assert _count(db, "activity_logs", user_id=user) == 1  # database untouched
        assert _sso_count(db, hub_user_id=user) == 1
        assert [r.status for r in _ledger(db, user)] == ["failed"]
        assert db.dal(db.dal.hub_users.id == user).select().first().email == "extfail@example.com"
        db.dal.commit()

    async def test_the_retry_after_an_external_failure_completes(self, db: Any) -> None:
        user = _user(db, name="retry")
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )
        with pytest.raises(ConnectionError):
            await _erase(db, user, FakeExternal(fail_with=ConnectionError("s3 down")))

        await _erase(db, user, FakeExternal())

        assert _count(db, "activity_logs", user_id=user) == 0
        assert [r.status for r in _ledger(db, user)] == ["failed", "completed"]

    async def test_a_database_failure_after_the_external_delete_rolls_everything_back(
        self, db: Any, block_anonymization: None
    ) -> None:
        user = _user(db, name="dbfail")
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )

        with pytest.raises(Exception, match=TRIGGER_MESSAGE):
            await _erase(db, user)

        assert _count(db, "activity_logs", user_id=user) == 1
        assert _count(db, "economy_ledger", user_uuid=HUB_UUID) == 1
        assert _sso_count(db, hub_user_id=user) == 1


class TestNoTierGate:
    """Statutory right: the entry point takes no tier/license/flag input and consults none."""

    async def test_erasure_runs_with_no_license_or_flag_configuration(self, db: Any) -> None:
        user = _user(db, name="freetier")
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )

        await _erase(db, user)  # no license server, no PostHog, no tier: still a full erasure

        assert _count(db, "activity_logs", user_id=user) == 0
        assert [r.status for r in _ledger(db, user)] == ["completed"]

    def test_the_entry_point_takes_no_tier_license_or_flag_input(self) -> None:
        import inspect

        params = set(inspect.signature(privacy.anonymize_user_data).parameters)
        assert params == {
            "async_dal",
            "dal",
            "user_id",
            "email",
            "suppression_keys",
            "external_stores",
        }


class TestPiiFreeTelemetry:
    """Registry logs carry table names and counts only -- never a subject value."""

    async def test_logs_never_contain_subject_values(
        self, db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        user = _user(db, name="logsafe")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _seed(
            db, user_id=user, hub_uuid=HUB_UUID, platform_user_id=PUID, sso_subject=SUBJECT_SENTINEL
        )

        with caplog.at_level(logging.DEBUG):
            await _erase(db, user)

        text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        for secret in (SUBJECT_SENTINEL, PUID, HUB_UUID, "Subject Name", "Guest Name", AVATAR):
            assert secret not in text, secret
        assert any(r.message == "dsar.erasure_registry_applied" for r in caplog.records)
