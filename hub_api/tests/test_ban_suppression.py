"""Ban-evasion suppression -- erasure branching, return re-catch, expiry/lift, tenant isolation.

Real pydal against a sqlite file, real HKDF/HMAC, real SQL triggers for fault injection (no mock
of the transaction boundary or of the key derivation). The properties under test:

- **benign erasure keeps nothing**: no `suppression_records` row, no reputation, no identity link,
  no member row -- and it works with NO master key configured (it never touches it);
- **banned erasure keeps exactly one keyed code per (tenant, identity)**: hmac + closed enum +
  expiry, never the platform id, written in the SAME transaction as the deletes (a failure
  anywhere rolls the row back too; a missing key aborts the whole erasure);
- **a return is re-caught by recomputing the code** (same platform id, new hub account), a benign
  return gets a fresh start, an expired or lifted row never matches;
- **per tenant only**: the same platform id in two tenants has different codes and never
  cross-matches;
- **not reversible without the key**.

Mutation-verified: see the "Fail-first proofs" section at the bottom of this docstring's twin in
`docs/compliance/suppression-LIA.md` (each mutation applied to the production code in-process,
this file re-run, reverted to green).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services import ban_suppression as bs
from services import data_privacy_service as privacy
from services import suppression_keys as keys
from services.ban_suppression import RestrictionReason
from tests.test_data_privacy_erasure import (  # noqa: F401 - fixtures re-exported for this module
    TRIGGER_MESSAGE,
    OtelSink,
    _ledger,
    _link_identity,
    _member,
    _member_row,
    _now,
    _seed_reputation,
    _user,
    block_anonymization,
    db,
    module_db,
    otel,
)

MASTER_HEX = "ab" * 32
OTHER_HEX = "cd" * 32
PUID = "123456789012345678"
EMAIL = "gone@example.com"


@pytest.fixture(autouse=True)
def master_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """The platform KEK every banned-erasure / lookup needs; rotation + ceiling vars cleared."""
    monkeypatch.setenv(keys.ENV_CURRENT, MASTER_HEX)
    monkeypatch.delenv(keys.ENV_PREVIOUS, raising=False)
    monkeypatch.delenv(bs.ENV_MAX_RETENTION_DAYS, raising=False)
    return MASTER_HEX


@pytest.fixture(autouse=True)
def fresh_instruments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebind the suppression instruments to whichever meter the test installs (see `otel`)."""
    monkeypatch.setattr(bs, "_INSTRUMENTS", None)


def _source(current: str = MASTER_HEX, previous: str | None = None) -> keys.PlatformKekKeySource:
    env = {keys.ENV_CURRENT: current}
    if previous:
        env[keys.ENV_PREVIOUS] = previous
    return keys.PlatformKekKeySource(env)


def _tenant(db: Any, slug: str) -> int:
    row = db.dal(db.dal.tenants.slug == slug).select().first()
    if row is None:
        row = db.dal.tenants.insert(slug=slug, display_name=slug, is_active=True)
        db.dal.commit()
        return int(row)
    db.dal.commit()
    return int(row.id)


def _community(db: Any, *, tenant_id: int | None, name: str = "c") -> int:
    community_id = db.dal.communities.insert(
        name=f"{name}-{tenant_id}-{_now().timestamp()}", tenant_id=tenant_id, member_count=3
    )
    db.dal.commit()
    return int(community_id)


def _ban(db: Any, member_id: int, *, role: str = "banned") -> None:
    db.dal(db.dal.community_members.id == member_id).update(role=role, is_active=False)
    db.dal.commit()


def _banned_member(
    db: Any,
    *,
    community_id: int,
    user_id: int | None,
    platform: str = "discord",
    platform_user_id: str | None = PUID,
) -> int:
    member_id = _member(
        db,
        community_id=community_id,
        user_id=user_id,
        platform=platform,
        platform_user_id=platform_user_id,
        reputation=310,
    )
    _ban(db, member_id)
    return member_id


def _rows(db: Any) -> list[Any]:
    rows = db.dal(db.dal.suppression_records.id > 0).select(orderby=db.dal.suppression_records.id)
    db.dal.commit()
    return list(rows)


def _insert_row(
    db: Any,
    *,
    tenant_id: int,
    platform_user_id: str = PUID,
    platform: str = "discord",
    expires_at: datetime | None = None,
    reason: str = "ban",
    source: keys.SuppressionKeySource | None = None,
) -> keys.SuppressionCode:
    code = keys.current_code(source or _source(), tenant_id, platform, platform_user_id)
    db.dal.suppression_records.insert(
        tenant_id=tenant_id,
        platform=platform,
        key_ver=code.key_ver,
        hmac=code.hmac,
        reason_enum=reason,
        expires_at=expires_at,
        created_at=_now(),
    )
    db.dal.commit()
    return code


async def _erase(db: Any, user_id: int, **kwargs: Any) -> None:
    await privacy.anonymize_user_data(db, db.dal, user_id=user_id, email=EMAIL, **kwargs)


async def _check(db: Any, *, community_id: int, user_id: int) -> bs.SuppressionMatch | None:
    return await bs.check_and_ban_membership(
        db, db.dal, community_id=community_id, hub_user_id=user_id, keys=_source()
    )


def _count(db: Any, query: Any) -> int:
    count = db.dal(query).count()
    db.dal.commit()
    return int(count)


def _footprint(db: Any, user_id: int) -> dict[str, int]:
    dal = db.dal
    counts = {
        "members": dal(dal.community_members.user_id == str(user_id)).count(),
        "identities": dal(dal.hub_user_identities.hub_user_id == user_id).count(),
        "rep_events": dal(dal.reputation_events.hub_user_id == user_id).count(),
        "rep_tenant": dal(dal.reputation_tenant.hub_user_id == user_id).count(),
        "suppression": dal(dal.suppression_records.id > 0).count(),
    }
    dal.commit()
    return counts


class TestErasureBranching:
    """Benign -> delete everything, keep nothing. Banned -> keep one keyed code, delete the rest."""

    async def test_benign_erasure_deletes_everything_including_reputation(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        community = _community(db, tenant_id=tenant)
        user = _user(db, name="benign")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        member = _member(db, community_id=community, user_id=user, platform_user_id=PUID)
        _seed_reputation(db, user_id=user, platform="discord", platform_user_id=PUID)
        assert _member_row(db, member).reputation == 742  # reputation exists before erasure

        await _erase(db, user)

        assert _footprint(db, user) == dict.fromkeys(
            ("members", "identities", "rep_events", "rep_tenant", "suppression"), 0
        )
        assert _member_row(db, member) is None  # no tombstone row carrying the score
        [ledger] = _ledger(db, user)
        assert ledger.deletion_scope["suppression_records_written"] == 0

    async def test_benign_erasure_never_touches_the_master_key(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)
        user = _user(db, name="nokey")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _member(db, community_id=_community(db, tenant_id=_tenant(db, "acme")), user_id=user)

        await _erase(db, user)  # would raise SuppressionKeyError if the key were read

        assert [r.status for r in _ledger(db, user)] == ["completed"]

    async def test_banned_erasure_creates_exactly_one_suppression_row(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        community = _community(db, tenant_id=tenant)
        user = _user(db, name="banned")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=community, user_id=user)
        before = _now()

        await _erase(db, user)

        [row] = _rows(db)
        expected = keys.current_code(_source(), tenant, "discord", PUID)
        assert (row.tenant_id, row.platform) == (tenant, "discord")
        assert (row.key_ver, row.hmac) == (expected.key_ver, expected.hmac)
        assert row.reason_enum == "ban"
        assert row.expires_at is None  # indefinite until lifted
        assert before - timedelta(seconds=5) <= row.created_at.replace(tzinfo=UTC)
        assert len(row.hmac) == 64
        [ledger] = _ledger(db, user)
        assert ledger.deletion_scope["suppression_records_written"] == 1

    async def test_the_row_holds_no_platform_id_or_person_identifier(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        user = _user(db, name="opaque")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=_community(db, tenant_id=tenant), user_id=user)

        await _erase(db, user)

        [row] = _rows(db)
        assert sorted(db.dal.suppression_records.fields) == sorted(
            [
                "id",
                "tenant_id",
                "platform",
                "key_ver",
                "hmac",
                "reason_enum",
                "expires_at",
                "created_at",
            ]
        )
        stored = repr(row.as_dict())
        for secret in (PUID, EMAIL, "opaque", hashlib.sha256(PUID.encode()).hexdigest()):
            assert secret not in stored

    async def test_banned_erasure_deletes_pii_and_reputation_like_a_benign_one(
        self, db: Any
    ) -> None:
        tenant = _tenant(db, "acme")
        community = _community(db, tenant_id=tenant)
        user = _user(db, name="banned2")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        member = _banned_member(db, community_id=community, user_id=user)
        _seed_reputation(db, user_id=user, platform="discord", platform_user_id=PUID)
        db.dal.hub_user_profiles.insert(hub_user_id=user, bio="about me")
        db.dal.commit()

        await _erase(db, user)

        footprint = _footprint(db, user)
        assert {k: v for k, v in footprint.items() if k != "suppression"} == dict.fromkeys(
            ("members", "identities", "rep_events", "rep_tenant"), 0
        )
        assert _member_row(db, member) is None
        assert _count(db, db.dal.hub_user_profiles.hub_user_id == user) == 0
        row = db.dal(db.dal.hub_users.id == user).select().first()
        db.dal.commit()
        assert (row.email, row.is_active, row.display_name) == (
            f"deleted_{user}@deleted.waddlebot",
            False,
            None,
        )
        assert footprint["suppression"] == 1

    async def test_auto_ban_is_classified_from_the_reputation_log(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        community = _community(db, tenant_id=tenant)
        user = _user(db, name="auto")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=community, user_id=user)
        db.dal.reputation_events.insert(
            community_id=community,
            hub_user_id=user,
            platform="discord",
            platform_user_id=PUID,
            event_type="auto_ban",
            score_before=320,
            score_after=320,
            reason="Auto-ban: Score 320 below threshold 350",
            created_at=_now(),
        )
        db.dal.commit()

        await _erase(db, user)

        [row] = _rows(db)
        assert row.reason_enum == "auto_ban"
        assert "Auto-ban" not in repr(row.as_dict())  # the free-text reason is never copied

    async def test_a_manual_ban_outranks_an_auto_ban_for_the_same_identity(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        auto_c = _community(db, tenant_id=tenant, name="auto")
        manual_c = _community(db, tenant_id=tenant, name="manual")
        user = _user(db, name="mixed")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=auto_c, user_id=user)
        _banned_member(db, community_id=manual_c, user_id=user)  # same identity, no auto_ban event
        db.dal.reputation_events.insert(
            community_id=auto_c,
            hub_user_id=user,
            platform="discord",
            platform_user_id=PUID,
            event_type="auto_ban",
            score_before=1,
            score_after=1,
            created_at=_now(),
        )
        db.dal.commit()

        await _erase(db, user)

        assert {r.reason_enum for r in _rows(db)} == {"ban"}
        assert len(_rows(db)) == 1  # same tenant + identity -> one row, not two

    async def test_a_hub_linked_ban_suppresses_every_linked_identity_in_that_tenant(
        self, db: Any
    ) -> None:
        tenant = _tenant(db, "acme")
        user = _user(db, name="multi")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _link_identity(db, user_id=user, platform="twitch", platform_user_id="777")
        _banned_member(
            db, community_id=_community(db, tenant_id=tenant), user_id=user, platform_user_id=None
        )

        await _erase(db, user)

        rows = _rows(db)
        assert sorted(r.platform for r in rows) == ["discord", "twitch"]
        assert {r.tenant_id for r in rows} == {tenant}

    async def test_a_platform_keyed_ban_suppresses_only_that_account(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        user = _user(db, name="keyed")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _link_identity(db, user_id=user, platform="twitch", platform_user_id="777")
        _banned_member(db, community_id=_community(db, tenant_id=tenant), user_id=None)

        await _erase(db, user)

        [row] = _rows(db)
        assert row.platform == "discord"  # the twitch account was never banned

    async def test_a_ban_in_one_tenant_writes_no_row_for_another(self, db: Any) -> None:
        acme = _tenant(db, "acme")
        beta = _tenant(db, "beta")
        user = _user(db, name="two-tenants")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=_community(db, tenant_id=acme), user_id=user)
        _member(db, community_id=_community(db, tenant_id=beta), user_id=user)  # good standing

        await _erase(db, user)

        assert [r.tenant_id for r in _rows(db)] == [acme]

    async def test_each_tenant_with_a_ban_gets_its_own_row_with_a_different_code(
        self, db: Any
    ) -> None:
        acme = _tenant(db, "acme")
        beta = _tenant(db, "beta")
        user = _user(db, name="both")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=_community(db, tenant_id=acme), user_id=user)
        _banned_member(db, community_id=_community(db, tenant_id=beta), user_id=user)

        await _erase(db, user)

        rows = {r.tenant_id: r for r in _rows(db)}
        assert set(rows) == {acme, beta}
        assert rows[acme].hmac != rows[beta].hmac  # same platform id, per-tenant key

    async def test_only_the_role_banned_counts_as_an_active_restriction(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        user = _user(db, name="mod")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        community = _community(db, tenant_id=tenant)
        for role in ("moderator", "member", "community-admin", "vip"):
            _member(
                db, community_id=community, user_id=user, platform_user_id=f"id-{role}", role=role
            )

        await _erase(db, user)

        assert _rows(db) == []

    async def test_a_ban_row_in_a_community_with_no_tenant_writes_nothing_and_warns(
        self, db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        user = _user(db, name="orphanc")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=_community(db, tenant_id=None), user_id=user)

        with caplog.at_level("WARNING", logger=bs.logger.name):
            await _erase(db, user)

        assert _rows(db) == []  # a code with no tenant would be unscoped: never written
        assert "suppression.community_without_tenant" in caplog.text

    async def test_a_ban_without_any_usable_identity_writes_nothing(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        user = _user(db, name="noid")
        _banned_member(
            db,
            community_id=_community(db, tenant_id=tenant),
            user_id=user,
            platform_user_id=None,
        )  # hub-linked ban, but the hub user has no linked identity to code

        await _erase(db, user)

        assert _rows(db) == []

    async def test_a_stored_identity_that_cannot_be_coded_is_skipped_with_a_warning(
        self, db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        tenant = _tenant(db, "acme")
        user = _user(db, name="badplat")
        _link_identity(db, user_id=user, platform="Not A Platform!", platform_user_id=PUID)
        _link_identity(db, user_id=user, platform="twitch", platform_user_id="42")
        _banned_member(
            db, community_id=_community(db, tenant_id=tenant), user_id=user, platform_user_id=None
        )

        with caplog.at_level("WARNING", logger=bs.logger.name):
            await _erase(db, user)

        assert [r.platform for r in _rows(db)] == ["twitch"]
        assert "suppression.identity_skipped" in caplog.text

    async def test_the_ledger_discloses_the_count_but_never_the_code_or_the_id(
        self, db: Any
    ) -> None:
        tenant = _tenant(db, "acme")
        user = _user(db, name="ledger")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=_community(db, tenant_id=tenant), user_id=user)

        await _erase(db, user)

        [ledger] = _ledger(db, user)
        [row] = _rows(db)
        text = repr(dict(ledger.deletion_scope)) + str(ledger.error_detail)
        assert ledger.deletion_scope["suppression_records_written"] == 1
        assert PUID not in text and row.hmac not in text


class TestFailLoud:
    """No silent fallback: a broken key or a mid-way failure leaves the subject exactly as found."""

    async def _banned_subject(self, db: Any, name: str) -> tuple[int, int, int]:
        tenant = _tenant(db, "acme")
        user = _user(db, name=name)
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        member = _banned_member(db, community_id=_community(db, tenant_id=tenant), user_id=user)
        _seed_reputation(db, user_id=user, platform="discord", platform_user_id=PUID)
        return tenant, user, member

    async def test_missing_master_key_with_an_active_ban_aborts_and_rolls_back_everything(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, user, member = await self._banned_subject(db, "nokey2")
        before = _footprint(db, user)
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)

        with pytest.raises(keys.SuppressionKeyError):
            await _erase(db, user)

        assert _footprint(db, user) == before  # nothing deleted, nothing recorded
        assert _member_row(db, member) is not None
        assert db.dal(db.dal.hub_users.id == user).select().first().email == "nokey2@example.com"
        db.dal.commit()
        assert [r.status for r in _ledger(db, user)] == ["failed"]
        assert _rows(db) == []

    async def test_a_malformed_master_key_aborts_without_echoing_it(
        self, db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _, user, _ = await self._banned_subject(db, "badkey")
        monkeypatch.setenv(keys.ENV_CURRENT, "zz" * 32)

        with caplog.at_level("DEBUG"), pytest.raises(keys.SuppressionKeyError):
            await _erase(db, user)

        assert "zzzz" not in caplog.text
        [ledger] = _ledger(db, user)
        assert "zzzz" not in str(ledger.error_detail)

    async def test_a_failure_after_the_row_was_written_rolls_the_row_back_too(
        self, db: Any, block_anonymization: None
    ) -> None:
        _, user, member = await self._banned_subject(db, "latefail")
        before = _footprint(db, user)

        with pytest.raises(Exception, match=TRIGGER_MESSAGE):
            await _erase(db, user)

        assert _rows(db) == []  # the suppression row shares the erasure's transaction
        assert _footprint(db, user) == before
        assert _member_row(db, member) is not None

    async def test_a_retry_after_the_key_is_fixed_completes_with_one_row(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, user, _ = await self._banned_subject(db, "retry")
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)
        with pytest.raises(keys.SuppressionKeyError):
            await _erase(db, user)
        monkeypatch.setenv(keys.ENV_CURRENT, MASTER_HEX)

        await _erase(db, user)

        assert [r.status for r in _ledger(db, user)] == ["failed", "completed"]
        assert len(_rows(db)) == 1

    async def test_the_key_seam_is_injectable(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant, user, _ = await self._banned_subject(db, "seam")
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)  # the env default would fail

        await _erase(db, user, suppression_keys=_source(OTHER_HEX))

        [row] = _rows(db)
        assert row.hmac == keys.current_code(_source(OTHER_HEX), tenant, "discord", PUID).hmac

    async def test_retention_ceiling_caps_an_indefinite_ban(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(bs.ENV_MAX_RETENTION_DAYS, "30")
        _, user, _ = await self._banned_subject(db, "ceiling")

        await _erase(db, user)

        [row] = _rows(db)
        delta = row.expires_at.replace(tzinfo=UTC) - _now()
        assert timedelta(days=29, hours=23) < delta <= timedelta(days=30)

    @pytest.mark.parametrize("raw", ["abc", "0", "-3", "99999", "1.5"])
    async def test_a_malformed_retention_ceiling_aborts_the_erasure(
        self, db: Any, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(bs.ENV_MAX_RETENTION_DAYS, raw)
        _, user, member = await self._banned_subject(db, f"cfg{abs(hash(raw)) % 1000}")

        with pytest.raises(bs.SuppressionConfigError):
            await _erase(db, user)

        assert _member_row(db, member) is not None
        assert _rows(db) == []

    async def test_a_benign_erasure_ignores_a_malformed_retention_ceiling(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(bs.ENV_MAX_RETENTION_DAYS, "garbage")
        user = _user(db, name="benigncfg")

        await _erase(db, user)

        assert [r.status for r in _ledger(db, user)] == ["completed"]


class TestRetentionCeilingParsing:
    def test_unset_means_until_lifted(self) -> None:
        assert bs.max_retention_days({}) is None
        assert bs.max_retention_days({bs.ENV_MAX_RETENTION_DAYS: "  "}) is None

    def test_a_valid_value_is_returned(self) -> None:
        assert bs.max_retention_days({bs.ENV_MAX_RETENTION_DAYS: " 365 "}) == 365

    def test_reads_the_process_environment_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(bs.ENV_MAX_RETENTION_DAYS, "7")
        assert bs.max_retention_days() == 7


class TestReturnRecatch:
    """A return recomputes the per-tenant code; a live match re-applies the restriction."""

    async def _erased_banned_user(self, db: Any, tenant: int) -> None:
        old = _user(db, name=f"old-{tenant}")
        _link_identity(db, user_id=old, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=_community(db, tenant_id=tenant), user_id=old)
        await _erase(db, old)

    async def test_a_returning_banned_user_with_the_same_platform_id_is_recaught(
        self, db: Any
    ) -> None:
        tenant = _tenant(db, "acme")
        await self._erased_banned_user(db, tenant)
        assert len(_rows(db)) == 1
        target = _community(db, tenant_id=tenant, name="other-community")
        returning = _user(db, name="returning")
        _link_identity(db, user_id=returning, platform="discord", platform_user_id=PUID)

        match = await _check(db, community_id=target, user_id=returning)

        assert match is not None
        assert (match.tenant_id, match.reason, match.expires_at) == (
            tenant,
            RestrictionReason.BAN,
            None,
        )
        row = (
            db.dal(
                (db.dal.community_members.community_id == target)
                & (db.dal.community_members.user_id == str(returning))
            )
            .select()
            .first()
        )
        db.dal.commit()
        assert (row.role, row.is_active) == ("banned", False)  # the restriction is re-applied

    async def test_recatch_upgrades_an_existing_membership_instead_of_adding_a_second(
        self, db: Any
    ) -> None:
        tenant = _tenant(db, "acme")
        await self._erased_banned_user(db, tenant)
        target = _community(db, tenant_id=tenant, name="existing")
        returning = _user(db, name="existing-member")
        _link_identity(db, user_id=returning, platform="discord", platform_user_id=PUID)
        _member(db, community_id=target, user_id=returning, role="member")

        assert await _check(db, community_id=target, user_id=returning) is not None

        assert _count(db, db.dal.community_members.user_id == str(returning)) == 1
        assert _count(db, db.dal.community_members.role == "banned") >= 1

    async def test_a_returning_benign_user_gets_a_fresh_start(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        benign = _user(db, name="benign-return")
        _link_identity(db, user_id=benign, platform="discord", platform_user_id=PUID)
        _member(db, community_id=_community(db, tenant_id=tenant), user_id=benign)
        await _erase(db, benign)
        assert _rows(db) == []  # nothing retained about them
        target = _community(db, tenant_id=tenant, name="fresh")
        returning = _user(db, name="benign-back")
        _link_identity(db, user_id=returning, platform="discord", platform_user_id=PUID)

        match = await _check(db, community_id=target, user_id=returning)

        assert match is None
        assert _count(db, db.dal.community_members.community_id == target) == 0  # nothing written
        assert _count(db, db.dal.reputation_tenant.hub_user_id == returning) == 0  # fresh score

    async def test_a_different_platform_account_is_not_caught(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        await self._erased_banned_user(db, tenant)
        other = _user(db, name="other-id")
        _link_identity(db, user_id=other, platform="discord", platform_user_id="999999999999999999")
        _link_identity(
            db, user_id=other, platform="twitch", platform_user_id=PUID
        )  # same id, other platform

        assert (
            await _check(db, community_id=_community(db, tenant_id=tenant), user_id=other) is None
        )

    async def test_the_same_platform_id_in_another_tenant_never_matches(self, db: Any) -> None:
        acme = _tenant(db, "acme")
        beta = _tenant(db, "beta")
        await self._erased_banned_user(db, acme)
        returning = _user(db, name="cross")
        _link_identity(db, user_id=returning, platform="discord", platform_user_id=PUID)
        beta_community = _community(db, tenant_id=beta)

        assert await _check(db, community_id=beta_community, user_id=returning) is None

        assert _count(db, db.dal.community_members.community_id == beta_community) == 0
        # ...and the same person IS caught in the tenant that banned them.
        acme_community = _community(db, tenant_id=acme, name="home")
        assert await _check(db, community_id=acme_community, user_id=returning) is not None

    async def test_two_tenants_hold_different_codes_for_one_platform_id(self, db: Any) -> None:
        acme = _tenant(db, "acme")
        beta = _tenant(db, "beta")
        await self._erased_banned_user(db, acme)
        await self._erased_banned_user(db, beta)

        rows = {r.tenant_id: r.hmac for r in _rows(db)}
        assert set(rows) == {acme, beta} and rows[acme] != rows[beta]

    async def test_an_expired_row_never_matches_even_before_the_purge_runs(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant, expires_at=_now() - timedelta(minutes=1))
        returning = _user(db, name="expired")
        _link_identity(db, user_id=returning, platform="discord", platform_user_id=PUID)

        match = await _check(db, community_id=_community(db, tenant_id=tenant), user_id=returning)

        assert match is None
        assert len(_rows(db)) == 1  # not deleted by a lookup; purge is a separate job

    async def test_a_dated_row_matches_until_it_expires(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        soon = _now() + timedelta(days=3)
        _insert_row(db, tenant_id=tenant, expires_at=soon, reason="auto_ban")
        returning = _user(db, name="dated")
        _link_identity(db, user_id=returning, platform="discord", platform_user_id=PUID)

        match = await _check(db, community_id=_community(db, tenant_id=tenant), user_id=returning)

        assert match is not None and match.reason is RestrictionReason.AUTO_BAN
        assert abs(match.expires_at.replace(tzinfo=UTC) - soon) < timedelta(seconds=1)

    async def test_purge_deletes_expired_rows_and_keeps_live_and_indefinite_ones(
        self, db: Any
    ) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(
            db,
            tenant_id=tenant,
            platform_user_id="expired-1",
            expires_at=_now() - timedelta(days=1),
        )
        _insert_row(
            db,
            tenant_id=tenant,
            platform_user_id="expired-2",
            expires_at=_now() - timedelta(seconds=1),
        )
        live = _insert_row(
            db, tenant_id=tenant, platform_user_id="live", expires_at=_now() + timedelta(days=1)
        )
        forever = _insert_row(db, tenant_id=tenant, platform_user_id="forever", expires_at=None)

        deleted = await bs.purge_expired_async(db, db.dal)

        assert deleted == 2
        assert {r.hmac for r in _rows(db)} == {live.hmac, forever.hmac}

    async def test_lift_removes_the_row_and_the_user_is_no_longer_caught(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        await self._erased_banned_user(db, tenant)
        returning = _user(db, name="lifted")
        _link_identity(db, user_id=returning, platform="discord", platform_user_id=PUID)
        community = _community(db, tenant_id=tenant)
        assert await _check(db, community_id=community, user_id=returning) is not None

        deleted = await bs.lift_suppression_async(
            db,
            db.dal,
            tenant_id=tenant,
            platform="Discord",
            platform_user_id=f" {PUID} ",
            keys=_source(),
        )

        assert deleted == 1 and _rows(db) == []
        fresh = _user(db, name="lifted-again")
        _link_identity(db, user_id=fresh, platform="discord", platform_user_id=PUID)
        assert await _check(db, community_id=community, user_id=fresh) is None

    async def test_lift_is_tenant_scoped(self, db: Any) -> None:
        acme = _tenant(db, "acme")
        beta = _tenant(db, "beta")
        await self._erased_banned_user(db, acme)
        await self._erased_banned_user(db, beta)

        deleted = await bs.lift_suppression_async(
            db, db.dal, tenant_id=acme, platform="discord", platform_user_id=PUID, keys=_source()
        )

        assert deleted == 1
        assert [r.tenant_id for r in _rows(db)] == [beta]  # beta's row is untouched

    async def test_lifting_something_that_was_never_recorded_deletes_nothing(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant, platform_user_id="someone-else")

        deleted = await bs.lift_suppression_async(
            db, db.dal, tenant_id=tenant, platform="discord", platform_user_id=PUID, keys=_source()
        )

        assert deleted == 0 and len(_rows(db)) == 1

    async def test_lift_with_no_rows_needs_no_key(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)
        deleted = await bs.lift_suppression_async(
            db, db.dal, tenant_id=_tenant(db, "acme"), platform="discord", platform_user_id=PUID
        )
        assert deleted == 0

    async def test_lift_with_rows_present_but_no_key_fails_loudly(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)

        with pytest.raises(keys.SuppressionKeyError):
            await bs.lift_suppression_async(
                db, db.dal, tenant_id=tenant, platform="discord", platform_user_id=PUID
            )

        assert len(_rows(db)) == 1  # never reports success while leaving the row behind

    async def test_a_row_written_under_the_previous_key_still_matches_after_rotation(
        self, db: Any
    ) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant, source=_source(OTHER_HEX))  # written before rotation
        returning = _user(db, name="rotated")
        _link_identity(db, user_id=returning, platform="discord", platform_user_id=PUID)
        community = _community(db, tenant_id=tenant)

        rotated = _source(MASTER_HEX, previous=OTHER_HEX)
        match = await bs.check_and_ban_membership(
            db, db.dal, community_id=community, hub_user_id=returning, keys=rotated
        )
        assert match is not None

    async def test_dropping_the_previous_key_makes_the_old_row_unmatchable(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant, source=_source(OTHER_HEX))
        returning = _user(db, name="orphaned-key")
        _link_identity(db, user_id=returning, platform="discord", platform_user_id=PUID)

        match = await bs.check_and_ban_membership(
            db,
            db.dal,
            community_id=_community(db, tenant_id=tenant),
            hub_user_id=returning,
            keys=_source(MASTER_HEX),
        )

        assert match is None  # documented: rotate with PREVIOUS set until old rows have expired

    async def test_a_lookup_needs_no_key_when_the_tenant_has_no_live_rows(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=_tenant(db, "beta"))  # another tenant's row only
        _insert_row(db, tenant_id=tenant, expires_at=_now() - timedelta(days=1))  # expired only
        user = _user(db, name="keyless")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)

        match = await bs.check_and_ban_membership(
            db, db.dal, community_id=_community(db, tenant_id=tenant), hub_user_id=user
        )

        assert match is None  # no live row for this tenant -> the key is never read

    async def test_a_lookup_with_live_rows_but_no_key_fails_closed(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)
        user = _user(db, name="closed")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        community = _community(db, tenant_id=tenant)

        with pytest.raises(keys.SuppressionKeyError):
            await bs.check_and_ban_membership(db, db.dal, community_id=community, hub_user_id=user)

        assert _count(db, db.dal.community_members.community_id == community) == 0

    async def test_a_community_without_a_tenant_is_never_matched(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        user = _user(db, name="tenantless")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)

        assert await _check(db, community_id=_community(db, tenant_id=None), user_id=user) is None

    async def test_a_user_with_no_identities_is_never_matched(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        user = _user(db, name="emailonly")

        assert await _check(db, community_id=_community(db, tenant_id=tenant), user_id=user) is None

    async def test_one_matching_identity_among_several_is_enough(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant, platform="twitch", platform_user_id="4242")
        user = _user(db, name="several")
        _link_identity(db, user_id=user, platform="discord", platform_user_id="1")
        _link_identity(db, user_id=user, platform="twitch", platform_user_id="4242")

        assert (
            await _check(db, community_id=_community(db, tenant_id=tenant), user_id=user)
            is not None
        )


class TestReapplyAfterIdentityLink:
    """A banned person can join first and link the banned account later."""

    async def test_matching_memberships_in_the_banned_tenant_are_flipped_only(
        self, db: Any
    ) -> None:
        acme = _tenant(db, "acme")
        beta = _tenant(db, "beta")
        _insert_row(db, tenant_id=acme)
        user = _user(db, name="joiner")
        acme_member = _member(
            db, community_id=_community(db, tenant_id=acme), user_id=user, role="member"
        )
        beta_member = _member(
            db, community_id=_community(db, tenant_id=beta), user_id=user, role="member"
        )
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)

        banned = await bs.reapply_for_hub_user_async(db, db.dal, hub_user_id=user, keys=_source())

        assert banned == 1
        assert (_member_row(db, acme_member).role, _member_row(db, acme_member).is_active) == (
            "banned",
            False,
        )
        assert _member_row(db, beta_member).role == "member"  # per tenant only

    async def test_an_already_banned_membership_is_not_recounted(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        user = _user(db, name="twice")
        member = _member(db, community_id=_community(db, tenant_id=tenant), user_id=user)
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        assert (
            await bs.reapply_for_hub_user_async(db, db.dal, hub_user_id=user, keys=_source()) == 1
        )
        assert (
            await bs.reapply_for_hub_user_async(db, db.dal, hub_user_id=user, keys=_source()) == 0
        )
        assert _member_row(db, member).role == "banned"

    async def test_no_identity_or_no_membership_is_a_no_op(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        bare = _user(db, name="bare")
        assert (
            await bs.reapply_for_hub_user_async(db, db.dal, hub_user_id=bare, keys=_source()) == 0
        )
        linked = _user(db, name="linkedonly")
        _link_identity(db, user_id=linked, platform="discord", platform_user_id=PUID)
        assert (
            await bs.reapply_for_hub_user_async(db, db.dal, hub_user_id=linked, keys=_source()) == 0
        )

    async def test_a_non_matching_identity_leaves_memberships_alone(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant, platform_user_id="someone-else")
        user = _user(db, name="clean")
        member = _member(
            db, community_id=_community(db, tenant_id=tenant), user_id=user, role="member"
        )
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)

        assert (
            await bs.reapply_for_hub_user_async(db, db.dal, hub_user_id=user, keys=_source()) == 0
        )
        assert _member_row(db, member).role == "member"

    async def test_a_key_failure_with_live_rows_propagates(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        user = _user(db, name="keyfail")
        _member(db, community_id=_community(db, tenant_id=tenant), user_id=user)
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)

        with pytest.raises(keys.SuppressionKeyError):
            await bs.reapply_for_hub_user_async(db, db.dal, hub_user_id=user)


class TestNotReversibleWithoutTheKey:
    """The stored rows are useless to anyone who holds the database but not `K_master`."""

    async def test_a_database_thief_cannot_confirm_a_guess_without_the_key(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        user = _user(db, name="thief-target")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _banned_member(db, community_id=_community(db, tenant_id=tenant), user_id=user)
        await _erase(db, user)
        [row] = _rows(db)

        guesses = [str(10**17 + i) for i in range(1000)] + [PUID]
        unkeyed = {
            "sha256": lambda g: hashlib.sha256(g.encode()).hexdigest(),
            "sha256-tenant": lambda g: hashlib.sha256(f"{tenant}{g}".encode()).hexdigest(),
            "sha256-platform": lambda g: hashlib.sha256(f"discord{g}".encode()).hexdigest(),
        }
        for name, fn in unkeyed.items():
            assert [g for g in guesses if fn(g) == row.hmac] == [], name
        wrong_key = keys.PlatformKekKeySource({keys.ENV_CURRENT: OTHER_HEX})
        assert [
            g
            for g in guesses
            if keys.current_code(wrong_key, tenant, "discord", g).hmac == row.hmac
        ] == []
        right_key = _source()
        hits = [
            g
            for g in guesses
            if keys.current_code(right_key, tenant, "discord", g).hmac == row.hmac
        ]
        print(f"enumerated {len(guesses)} ids: wrong-key hits=0, right-key hits={len(hits)}")
        assert hits == [PUID]

    async def test_a_lookup_under_the_wrong_key_finds_nothing(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant, source=_source(MASTER_HEX))
        user = _user(db, name="wrongkey")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)

        match = await bs.check_and_ban_membership(
            db,
            db.dal,
            community_id=_community(db, tenant_id=tenant),
            hub_user_id=user,
            keys=_source(OTHER_HEX),
        )

        assert match is None


class TestStoreEdges:
    def test_record_with_nothing_to_record_is_a_no_op(self, db: Any) -> None:
        assert bs.record_suppressions(db.dal, [], keys=_source(), now=_now()) == 0
        assert _rows(db) == []

    def test_recording_the_same_identity_twice_extends_one_row(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        now = _now()
        dated = bs.ActiveRestriction(
            tenant, "discord", PUID, RestrictionReason.AUTO_BAN, now + timedelta(days=1)
        )
        forever = bs.ActiveRestriction(tenant, "discord", PUID, RestrictionReason.BAN, None)

        assert bs.record_suppressions(db.dal, [dated], keys=_source(), now=now) == 1
        assert bs.record_suppressions(db.dal, [forever], keys=_source(), now=now) == 1
        db.dal.commit()

        [row] = _rows(db)
        assert (row.reason_enum, row.expires_at) == ("ban", None)  # NULL (until lifted) wins

    def test_a_later_expiry_wins_and_a_shorter_one_does_not_shorten(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        now = _now()
        long_ = bs.ActiveRestriction(
            tenant, "discord", PUID, RestrictionReason.BAN, now + timedelta(days=10)
        )
        short = bs.ActiveRestriction(
            tenant, "discord", PUID, RestrictionReason.BAN, now + timedelta(days=1)
        )
        bs.record_suppressions(db.dal, [long_], keys=_source(), now=now)
        bs.record_suppressions(db.dal, [short], keys=_source(), now=now)
        db.dal.commit()

        [row] = _rows(db)
        assert abs(row.expires_at.replace(tzinfo=UTC) - (now + timedelta(days=10))) < timedelta(
            seconds=1
        )

    def test_the_ceiling_never_extends_a_shorter_expiry(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        now = _now()
        short = bs.ActiveRestriction(
            tenant, "discord", PUID, RestrictionReason.BAN, now + timedelta(days=2)
        )
        bs.record_suppressions(db.dal, [short], keys=_source(), now=now, retention_days=30)
        db.dal.commit()

        [row] = _rows(db)
        assert abs(row.expires_at.replace(tzinfo=UTC) - (now + timedelta(days=2))) < timedelta(
            seconds=1
        )

    def test_restriction_repr_hides_the_platform_id(self) -> None:
        item = bs.ActiveRestriction(1, "discord", PUID, RestrictionReason.BAN, None)
        assert PUID not in repr(item)

    def test_the_reason_vocabulary_is_closed(self) -> None:
        assert {r.value for r in RestrictionReason} == {"ban", "auto_ban"}

    def test_stronger_and_later_helpers(self) -> None:
        now = _now()
        assert (
            bs._stronger(RestrictionReason.AUTO_BAN, RestrictionReason.BAN) is RestrictionReason.BAN
        )
        assert (
            bs._stronger(RestrictionReason.AUTO_BAN, RestrictionReason.AUTO_BAN)
            is RestrictionReason.AUTO_BAN
        )
        assert bs._later(None, now) is None and bs._later(now, None) is None
        assert bs._later(now, now + timedelta(1)) == now + timedelta(1)
        assert bs._as_utc(datetime(2030, 1, 1)).tzinfo is UTC

    def test_banned_role_matching_is_exact_and_case_insensitive(self) -> None:
        assert bs._is_banned_role("banned") and bs._is_banned_role(" Banned ")
        assert not bs._is_banned_role("ban") and not bs._is_banned_role(None)
        assert not bs._is_banned_role("unbanned")

    def test_lookups_validate_their_inputs(self, db: Any) -> None:
        with pytest.raises(keys.SuppressionInputError):
            bs.find_suppression(
                db.dal,
                tenant_id=0,
                platform="discord",
                platform_user_id=PUID,
                keys=_source(),
                now=_now(),
            )
        with pytest.raises(keys.SuppressionInputError):
            bs.find_suppression(
                db.dal, tenant_id=1, platform="", platform_user_id=PUID, keys=_source(), now=_now()
            )

    def test_check_identities_skips_one_that_cannot_be_coded(
        self, db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        with caplog.at_level("WARNING", logger=bs.logger.name):
            match = bs.check_identities(
                db.dal,
                tenant_id=tenant,
                identities=[("Not A Platform!", "x"), ("discord", PUID)],
                keys=_source(),
                now=_now(),
            )
        assert match is not None
        assert "suppression.identity_skipped" in caplog.text


class TestStoreEdgesMore:
    def test_identities_of_collapses_duplicates_and_skips_blank_rows(self, db: Any) -> None:
        user = _user(db, name="dupes")
        for _ in range(2):
            _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)
        _link_identity(db, user_id=user, platform="twitch", platform_user_id="")
        _link_identity(db, user_id=user, platform="twitch", platform_user_id="42")

        pairs = bs.identities_of(db.dal, user)
        db.dal.commit()

        assert sorted(pairs) == [("discord", PUID), ("twitch", "42")]

    def test_tenants_of_with_no_communities_is_empty(self, db: Any) -> None:
        assert bs.tenants_of(db.dal, []) == {}
        assert bs.tenants_of(db.dal, [None]) == {}  # type: ignore[list-item]

    def test_rows_under_two_key_versions_merge_to_the_strongest_reason_and_latest_expiry(
        self, db: Any
    ) -> None:
        tenant = _tenant(db, "acme")
        soon, later = _now() + timedelta(days=1), _now() + timedelta(days=9)
        _insert_row(
            db, tenant_id=tenant, reason="auto_ban", expires_at=soon, source=_source(MASTER_HEX)
        )
        _insert_row(db, tenant_id=tenant, reason="ban", expires_at=later, source=_source(OTHER_HEX))

        match = bs.find_suppression(
            db.dal,
            tenant_id=tenant,
            platform="discord",
            platform_user_id=PUID,
            keys=_source(MASTER_HEX, previous=OTHER_HEX),  # both versions are loaded: both rows hit
            now=_now(),
        )
        db.dal.commit()

        assert match is not None
        assert match.reason is RestrictionReason.BAN
        assert abs(match.expires_at.replace(tzinfo=UTC) - later) < timedelta(seconds=1)


class TestObservability:
    """Logs, spans and metric labels carry counts and enums -- never an id, a code or an email."""

    async def test_a_recatch_is_traced_counted_and_pii_free(
        self, db: Any, otel: OtelSink, caplog: pytest.LogCaptureFixture
    ) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        stored = _rows(db)[0].hmac
        user = _user(db, name="observed")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)

        with caplog.at_level("DEBUG"):
            await _check(db, community_id=_community(db, tenant_id=tenant), user_id=user)

        [span] = [s for s in otel.spans.get_finished_spans() if s.name == "hub.suppression.check"]
        assert span.attributes["matched"] is True
        events = otel.points("waddles_hub_suppression_events_total")
        assert any(
            p.attributes == {"op": "check", "result": "matched", "reason": "ban"} for p in events
        )
        assert otel.points("waddles_hub_suppression_check_duration_ms")
        text = caplog.text + repr(
            [dict(s.attributes or {}) for s in otel.spans.get_finished_spans()]
        )
        text += repr([dict(p.attributes) for p in events])
        for secret in (PUID, stored, "observed", "@example.com"):
            assert secret not in text

    async def test_a_clear_check_is_counted_as_clear(self, db: Any, otel: OtelSink) -> None:
        user = _user(db, name="clear")
        await _check(db, community_id=_community(db, tenant_id=_tenant(db, "acme")), user_id=user)
        events = otel.points("waddles_hub_suppression_events_total")
        assert any(p.attributes == {"op": "check", "result": "clear"} for p in events)

    async def test_a_failed_check_is_counted_and_logged_with_a_traceback(
        self,
        db: Any,
        otel: OtelSink,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        monkeypatch.delenv(keys.ENV_CURRENT, raising=False)
        user = _user(db, name="failcount")
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)

        with (
            caplog.at_level("ERROR", logger=bs.logger.name),
            pytest.raises(keys.SuppressionKeyError),
        ):
            await bs.check_and_ban_membership(
                db, db.dal, community_id=_community(db, tenant_id=tenant), hub_user_id=user
            )

        assert any(
            p.attributes == {"op": "check", "result": "error"}
            for p in otel.points("waddles_hub_suppression_events_total")
        )
        assert "suppression.check_failed" in caplog.text
        assert any(
            r.exc_info for r in caplog.records
        )  # type + message + traceback, not only `extra`
        span = [s for s in otel.spans.get_finished_spans() if s.name == "hub.suppression.check"][0]
        assert span.status.description == "SuppressionKeyError"

    async def test_telemetry_failure_never_fails_the_check(
        self, db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom() -> None:
            raise RuntimeError("meter down")

        monkeypatch.setattr(bs, "_instruments", boom)
        user = _user(db, name="telefail")

        with caplog.at_level("ERROR", logger=bs.logger.name):
            match = await _check(
                db, community_id=_community(db, tenant_id=_tenant(db, "acme")), user_id=user
            )

        assert match is None
        assert "suppression.metrics_failed" in caplog.text

    async def test_a_lift_and_a_purge_are_counted(self, db: Any, otel: OtelSink) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(
            db, tenant_id=tenant, platform_user_id="gone", expires_at=_now() - timedelta(days=1)
        )
        _insert_row(db, tenant_id=tenant)
        await bs.purge_expired_async(db, db.dal)
        await bs.lift_suppression_async(
            db, db.dal, tenant_id=tenant, platform="discord", platform_user_id=PUID
        )

        events = [dict(p.attributes) for p in otel.points("waddles_hub_suppression_events_total")]
        assert {"op": "lift", "result": "deleted"} in events
        assert sum(p.value for p in otel.points("waddles_hub_suppression_purged_total")) == 1

    async def test_reapply_is_counted(self, db: Any, otel: OtelSink) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(db, tenant_id=tenant)
        user = _user(db, name="reapplied")
        _member(db, community_id=_community(db, tenant_id=tenant), user_id=user)
        _link_identity(db, user_id=user, platform="discord", platform_user_id=PUID)

        await bs.reapply_for_hub_user_async(db, db.dal, hub_user_id=user, keys=_source())

        events = [dict(p.attributes) for p in otel.points("waddles_hub_suppression_events_total")]
        assert {"op": "reapply", "result": "banned"} in events


class TestPurgeLoop:
    async def test_the_loop_deletes_expired_rows_and_stops_when_cancelled(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(
            db, tenant_id=tenant, platform_user_id="old", expires_at=_now() - timedelta(days=1)
        )
        keep = _insert_row(db, tenant_id=tenant)
        task = asyncio.create_task(bs.run_purge_loop(db, db.dal, interval_seconds=0.05))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(_rows(db)) == 1:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert [r.hmac for r in _rows(db)] == [keep.hmac]

    async def test_a_failed_pass_is_logged_and_the_next_pass_still_runs(
        self, db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(
            db, tenant_id=tenant, platform_user_id="old", expires_at=_now() - timedelta(days=1)
        )
        real = bs.purge_expired_async
        calls = {"n": 0}

        async def flaky(async_dal: Any, dal: Any) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db hiccup")
            return await real(async_dal, dal)

        monkeypatch.setattr(bs, "purge_expired_async", flaky)
        with caplog.at_level("ERROR", logger=bs.logger.name):
            task = asyncio.create_task(bs.run_purge_loop(db, db.dal, interval_seconds=0.05))
            for _ in range(100):
                await asyncio.sleep(0.05)
                if not _rows(db):
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls["n"] >= 2 and _rows(db) == []
        assert "suppression.purge_failed" in caplog.text

    async def test_the_loop_waits_for_schema_readiness(self, db: Any) -> None:
        tenant = _tenant(db, "acme")
        _insert_row(
            db, tenant_id=tenant, platform_user_id="old", expires_at=_now() - timedelta(days=1)
        )
        task = asyncio.create_task(
            bs.run_purge_loop(db, db.dal, interval_seconds=0.05, is_ready=lambda: False)
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(_rows(db)) == 1  # not ready -> nothing touched

    async def test_cancellation_during_a_pass_propagates(
        self, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = asyncio.Event()

        async def hang(async_dal: Any, dal: Any) -> int:
            started.set()
            await asyncio.sleep(30)
            return 0

        monkeypatch.setattr(bs, "purge_expired_async", hang)
        task = asyncio.create_task(bs.run_purge_loop(db, db.dal, interval_seconds=0.05))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_a_pass_that_deletes_nothing_is_silent(
        self, db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO", logger=bs.logger.name):
            assert await bs.purge_expired_async(db, db.dal) == 0
        assert "suppression.purged" not in caplog.text

    def test_purge_with_nothing_expired_deletes_nothing(self, db: Any) -> None:
        assert bs.purge_expired(db.dal, now=_now()) == 0
