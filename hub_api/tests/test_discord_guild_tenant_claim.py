"""A Discord guild belongs to exactly ONE tenant (`discord_guild_tenants`, alembic 0137).

`guild_tenant_pairings` stays N:M between a guild and communities WITHIN a tenant; across tenants
the guild is claimed atomically (`INSERT ... ON CONFLICT DO NOTHING RETURNING`, guild id is the
PRIMARY KEY) by whichever tenant pairs it first, and the claim is freed when that tenant's last
pairing for the guild is deleted.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from services import guild_pairing as pairing_svc
from services.errors import ApiError

GUILD = "123456789012345678"
OTHER_GUILD = "223456789012345678"


def _community(dal: Any, tenant_id: int, name: str) -> int:
    cid = int(dal.communities.insert(name=name, tenant_id=tenant_id))
    dal.commit()
    return cid


def _tenant(dal: Any, slug: str) -> int:
    tid = int(dal.tenants.insert(slug=slug, is_active=True, is_global=False))
    dal.commit()
    return tid


def _pair(dal: Any, community_id: int, guild: str = GUILD) -> pairing_svc.GuildPairing:
    return pairing_svc.create_pairing(
        dal,
        community_id,
        discord_guild_id=guild,
        direction="bidirectional",
        role_name_prefix="[T]",
        ownership_verified=True,
        actor_user_id=None,
    )


def _claims(dal: Any, guild: str = GUILD) -> list[int]:
    return [
        int(r.tenant_id)
        for r in dal(dal.discord_guild_tenants.discord_guild_id == guild).select(
            dal.discord_guild_tenants.tenant_id
        )
    ]


def test_first_pairing_claims_the_guild_for_its_tenant(guild_pairing_db: Any) -> None:
    dal, cid, tenant_id, _g = guild_pairing_db
    _pair(dal, cid)
    assert _claims(dal) == [tenant_id]


def test_two_communities_of_one_tenant_may_pair_the_same_guild(guild_pairing_db: Any) -> None:
    """N:M inside a tenant is unchanged (the second pairing is a no-op re-claim)."""
    dal, cid, tenant_id, _g = guild_pairing_db
    second = _community(dal, tenant_id, "second")
    _pair(dal, cid)
    _pair(dal, second)
    assert {p.community_id for p in pairing_svc.list_pairings(dal, cid)} == {cid}
    assert _claims(dal) == [tenant_id]
    assert len(pairing_svc.list_pairings(dal, second)) == 1


def test_a_guild_cannot_be_paired_into_a_second_tenant(guild_pairing_db: Any) -> None:
    dal, cid, tenant_id, _g = guild_pairing_db
    other_tenant = _tenant(dal, "other-corp")
    foreign = _community(dal, other_tenant, "foreign")
    _pair(dal, cid)
    with pytest.raises(ApiError) as exc:
        _pair(dal, foreign)
    assert exc.value.status_code == 409
    assert "different tenant" in exc.value.message
    assert pairing_svc.list_pairings(dal, foreign) == []
    assert _claims(dal) == [tenant_id], "the loser must not displace or duplicate the claim"


def test_a_failed_pairing_commit_rolls_the_claim_back(
    guild_pairing_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim shares the pairing's transaction: if it cannot commit, neither survives."""
    dal, cid, _tenant, _g = guild_pairing_db
    real_commit = dal.commit

    def boom() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(dal, "commit", boom)
    with pytest.raises(RuntimeError):
        _pair(dal, cid)
    monkeypatch.setattr(dal, "commit", real_commit)
    assert _claims(dal) == [], "a claim without a pairing would squat the guild"
    assert pairing_svc.list_pairings(dal, cid) == []


def test_two_tenants_racing_for_one_guild_exactly_one_wins(guild_pairing_db: Any) -> None:
    dal, cid, _t, _g = guild_pairing_db
    tenants = [_tenant(dal, f"racer-{i}") for i in range(2)]
    communities = [_community(dal, t, f"racer-community-{i}") for i, t in enumerate(tenants)]
    del cid
    barrier = threading.Barrier(2)

    def attempt(community_id: int) -> str:
        barrier.wait()
        try:
            _pair(dal, community_id)
        except ApiError as exc:
            return f"refused:{exc.status_code}"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, communities))
    assert outcomes == ["refused:409", "won"], outcomes
    assert len(_claims(dal)) == 1
    assert _claims(dal)[0] in tenants


def test_deleting_the_last_pairing_frees_the_guild(guild_pairing_db: Any) -> None:
    dal, cid, tenant_id, _g = guild_pairing_db
    second = _community(dal, tenant_id, "second")
    other_tenant = _tenant(dal, "other-corp")
    foreign = _community(dal, other_tenant, "foreign")
    first = _pair(dal, cid)
    other = _pair(dal, second)

    assert pairing_svc.delete_pairing(dal, cid, first.id) is True
    assert _claims(dal) == [tenant_id], "another community of the tenant still pairs the guild"
    with pytest.raises(ApiError):
        _pair(dal, foreign)

    assert pairing_svc.delete_pairing(dal, second, other.id) is True
    assert _claims(dal) == [], "the last pairing is gone, so the claim is released"
    _pair(dal, foreign)
    assert _claims(dal) == [other_tenant]


def test_release_never_touches_another_guild_or_tenant(guild_pairing_db: Any) -> None:
    dal, cid, tenant_id, _g = guild_pairing_db
    other_tenant = _tenant(dal, "other-corp")
    foreign = _community(dal, other_tenant, "foreign")
    kept = _pair(dal, cid, OTHER_GUILD)
    gone = _pair(dal, cid)
    _pair(dal, foreign, "323456789012345678")
    assert pairing_svc.delete_pairing(dal, cid, gone.id) is True
    assert _claims(dal, OTHER_GUILD) == [tenant_id]
    assert _claims(dal, "323456789012345678") == [other_tenant]
    assert kept.id != gone.id
