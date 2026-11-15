"""At most ONE `provides: reputation-logic` app bundle per tenant (available AND active).

Typed 409 `reputation_logic_conflict` naming the bundle already holding the slot; a different
tenant, a non-logic bundle, and re-activating the SAME app are all unaffected.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.bundle_approval_service import activate_for_community, activate_tenant_wide
from services.errors import ApiError
from services.reputation_logic_exclusivity import (
    REPUTATION_LOGIC_CONFLICT_CODE,
    provides_capabilities,
)
from services.tenant_app_availability_service import set_available, unset_available
from tests.test_bundle_activation_service import _MANIFEST, _install_and_make_available

LOGIC_A = {
    **_MANIFEST,
    "app_id": "waddles.core.app.reputation-accrual",
    "provides": ["reputation-logic"],
}
LOGIC_B = {**_MANIFEST, "app_id": "thirdparty.app.my-reputation", "provides": "reputation-logic"}
PLAIN = {**_MANIFEST, "app_id": "waddles.socials.music.default"}


def _is_conflict(exc: pytest.ExceptionInfo[ApiError], active_app: str) -> bool:
    e = exc.value
    return (
        e.status_code == 409
        and e.code == REPUTATION_LOGIC_CONFLICT_CODE
        and (e.details or {}).get("active_app_id") == active_app
    )


async def _make_plain_then_flag_as_logic(install_dal: Any, manifest: dict[str, Any]) -> None:
    """Install + make available as an ordinary bundle, then stamp `provides:` onto its manifest.

    Lets a test reach the ACTIVATION check for a second logic bundle even though the
    availability check would already have refused it.
    """
    plain = {k: v for k, v in manifest.items() if k != "provides"}
    await _install_and_make_available(install_dal, manifest=plain)
    await install_dal(install_dal.app_version_uploads.app_id == manifest["app_id"]).update(
        manifest_json=manifest
    )


def test_provides_accepts_a_string_or_a_list_and_ignores_junk() -> None:
    assert provides_capabilities({"provides": "reputation-logic"}) == {"reputation-logic"}
    assert provides_capabilities({"provides": ["reputation-logic", "x"]}) == {
        "reputation-logic",
        "x",
    }
    assert provides_capabilities({"provides": 5}) == frozenset()
    assert provides_capabilities({}) == frozenset()
    assert provides_capabilities(None) == frozenset()


async def test_second_logic_bundle_cannot_be_made_available_in_the_tenant(install_dal: Any) -> None:
    await _install_and_make_available(install_dal, manifest=LOGIC_A)
    with pytest.raises(ApiError) as exc:
        await _install_and_make_available(install_dal, manifest=LOGIC_B)
    assert _is_conflict(exc, LOGIC_A["app_id"])


async def test_non_logic_bundle_is_unaffected(install_dal: Any) -> None:
    await _install_and_make_available(install_dal, manifest=LOGIC_A)
    await _install_and_make_available(install_dal, manifest=PLAIN)  # no raise


async def test_other_tenant_may_choose_its_own_logic_bundle(install_dal: Any) -> None:
    await _install_and_make_available(install_dal, tenant_id=1, manifest=LOGIC_A)
    await set_available(install_dal, tenant_id=2, app_id=LOGIC_A["app_id"], updated_by=1)


async def test_swapping_requires_removing_the_first(install_dal: Any) -> None:
    await _install_and_make_available(install_dal, manifest=LOGIC_A)
    await unset_available(install_dal, tenant_id=1, app_id=LOGIC_A["app_id"], updated_by=1)
    await _install_and_make_available(install_dal, manifest=LOGIC_B)  # now allowed


async def test_activation_is_per_tenant_not_per_community(install_dal: Any) -> None:
    await _make_plain_then_flag_as_logic(install_dal, LOGIC_A)
    await _make_plain_then_flag_as_logic(install_dal, LOGIC_B)
    c1 = await install_dal.communities.async_insert(tenant_id=1, name="c1")
    c2 = await install_dal.communities.async_insert(tenant_id=1, name="c2")
    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=c1,
        app_id=LOGIC_A["app_id"],
        activated_by=1,
        valkey_client=AsyncMock(),
    )
    # the SAME app in another community of the tenant is fine ...
    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=c2,
        app_id=LOGIC_A["app_id"],
        activated_by=1,
        valkey_client=AsyncMock(),
    )
    # ... a DIFFERENT logic bundle in ANY community of the tenant is not
    with pytest.raises(ApiError) as exc:
        await activate_for_community(
            install_dal,
            tenant_id=1,
            community_id=c2,
            app_id=LOGIC_B["app_id"],
            activated_by=1,
            valkey_client=AsyncMock(),
        )
    assert _is_conflict(exc, LOGIC_A["app_id"])
    rows = await install_dal(install_dal.app_active_versions.app_id == LOGIC_B["app_id"]).select()
    assert len(rows) == 0, "a refused activation must not leave an active pointer behind"


async def test_tenant_wide_activation_conflicts_with_a_community_activation(
    install_dal: Any,
) -> None:
    await _make_plain_then_flag_as_logic(install_dal, LOGIC_A)
    await _make_plain_then_flag_as_logic(install_dal, LOGIC_B)
    c1 = await install_dal.communities.async_insert(tenant_id=1, name="c1")
    await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=c1,
        app_id=LOGIC_A["app_id"],
        activated_by=1,
        valkey_client=AsyncMock(),
    )
    with pytest.raises(ApiError) as exc:
        await activate_tenant_wide(
            install_dal,
            tenant_id=1,
            app_id=LOGIC_B["app_id"],
            activated_by=None,
            valkey_client=AsyncMock(),
        )
    assert _is_conflict(exc, LOGIC_A["app_id"])


async def test_non_logic_bundle_activates_alongside_a_logic_bundle(install_dal: Any) -> None:
    await _make_plain_then_flag_as_logic(install_dal, LOGIC_A)
    await _install_and_make_available(install_dal, manifest=PLAIN)
    c1 = await install_dal.communities.async_insert(tenant_id=1, name="c1")
    for app in (LOGIC_A["app_id"], PLAIN["app_id"]):
        await activate_for_community(
            install_dal,
            tenant_id=1,
            community_id=c1,
            app_id=app,
            activated_by=1,
            valkey_client=AsyncMock(),
        )
