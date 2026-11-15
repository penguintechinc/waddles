"""`services/marketplace_billing_service.py` -- direct service-layer coverage.

Exercises the pydal query/mutation logic directly against
`marketplace_billing_db` (no HTTP layer) -- faster and more exhaustive
than round-tripping every branch through the blueprint, matching this
repo's established split between blueprint-level auth/wiring tests
(`test_v1_marketplace_billing_blueprint.py`) and service-level logic
tests.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from services import marketplace_billing_service as svc
from services.errors import ApiError
from tests.conftest import insert_global_admin


@pytest.fixture
def seeded(marketplace_billing_db: Any) -> tuple[Any, int, int]:
    return marketplace_billing_db


class TestIsCommunityAdmin:
    def test_super_admin_bypasses_membership(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        user_id = insert_global_admin(dal, username="sa", email="sa@x.com")
        dal.commit()
        assert svc.is_community_admin(dal, community_id, user_id) is True

    def test_non_member_is_denied(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        user_id = int(dal.hub_users.insert(username="u", email="u@x.com"))
        dal.commit()
        assert svc.is_community_admin(dal, community_id, user_id) is False

    def test_moderator_role_is_admin(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        user_id = int(dal.hub_users.insert(username="mod", email="mod@x.com"))
        dal.community_members.insert(
            community_id=community_id, user_id=str(user_id), role="moderator", is_active=True
        )
        dal.commit()
        assert svc.is_community_admin(dal, community_id, user_id) is True

    def test_plain_member_role_is_not_admin(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        user_id = int(dal.hub_users.insert(username="member", email="m@x.com"))
        dal.community_members.insert(
            community_id=community_id, user_id=str(user_id), role="member", is_active=True
        )
        dal.commit()
        assert svc.is_community_admin(dal, community_id, user_id) is False

    def test_inactive_membership_is_not_admin(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        user_id = int(dal.hub_users.insert(username="ex", email="ex@x.com"))
        dal.community_members.insert(
            community_id=community_id, user_id=str(user_id), role="community-owner", is_active=False
        )
        dal.commit()
        assert svc.is_community_admin(dal, community_id, user_id) is False

    def test_unknown_user_id_is_not_admin(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        assert svc.is_community_admin(dal, community_id, 999999) is False


class TestPremiumSubscriptionLifecycle:
    def test_get_premium_status_none_when_never_subscribed(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        assert svc.get_premium_status(dal, community_id) is None

    def test_upsert_then_get_status(self, seeded: Any) -> None:
        dal, tenant_id, community_id = seeded
        svc.upsert_trialing_premium_subscription(dal, community_id, tenant_id, 500, 10, 50, 3)
        status = svc.get_premium_status(dal, community_id)
        assert status is not None
        assert status.status == "trialing"
        assert status.currentSeatCount == 3

        # Idempotent re-upsert (ON CONFLICT DO UPDATE equivalent) -- one row.
        svc.upsert_trialing_premium_subscription(dal, community_id, tenant_id, 500, 10, 50, 5)
        rows = dal(dal.community_premium_subscriptions.community_id == community_id).select()
        assert len(rows) == 1
        assert rows.first().current_seat_count == 5

    def test_cancel_without_existing_subscription_raises_404(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        with pytest.raises(ApiError) as excinfo:
            svc.cancel_premium_subscription(dal, community_id, False)
        assert excinfo.value.status_code == 404

    def test_cancel_marks_canceled(self, seeded: Any) -> None:
        dal, tenant_id, community_id = seeded
        svc.upsert_trialing_premium_subscription(dal, community_id, tenant_id, 500, 10, 50, 1)
        result = svc.cancel_premium_subscription(dal, community_id, immediately=False)
        assert result.status == "canceled"
        assert result.cancelAtPeriodEnd is True


class TestModuleSubscriptions:
    def _make_module(self, dal: Any, *, published: bool = True, is_core: bool = False) -> int:
        return int(
            dal.hub_modules.insert(
                name="weather", display_name="Weather", is_published=published, is_core=is_core
            )
        )

    def test_list_empty(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        assert svc.list_community_subscriptions(dal, community_id) == []

    def test_subscribe_then_list(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        module_id = self._make_module(dal)
        dal.commit()
        new_id = svc.subscribe_module(dal, community_id, module_id, installed_by=1)
        assert new_id > 0
        rows = svc.list_community_subscriptions(dal, community_id)
        assert len(rows) == 1
        assert rows[0].moduleId == module_id
        assert rows[0].isEnabled is True

    def test_subscribe_unpublished_module_raises_404(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        module_id = self._make_module(dal, published=False)
        dal.commit()
        with pytest.raises(ApiError) as excinfo:
            svc.subscribe_module(dal, community_id, module_id, installed_by=1)
        assert excinfo.value.status_code == 404

    def test_subscribe_twice_raises_409(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        module_id = self._make_module(dal)
        dal.commit()
        svc.subscribe_module(dal, community_id, module_id, installed_by=1)
        with pytest.raises(ApiError) as excinfo:
            svc.subscribe_module(dal, community_id, module_id, installed_by=1)
        assert excinfo.value.status_code == 409

    def test_unsubscribe_missing_raises_404(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        with pytest.raises(ApiError) as excinfo:
            svc.unsubscribe_module(dal, community_id, 999)
        assert excinfo.value.status_code == 404

    def test_unsubscribe_core_module_raises_400(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        module_id = self._make_module(dal, is_core=True)
        dal.commit()
        sub_id = svc.subscribe_module(dal, community_id, module_id, installed_by=1)
        with pytest.raises(ApiError) as excinfo:
            svc.unsubscribe_module(dal, community_id, sub_id)
        assert excinfo.value.status_code == 400

    def test_unsubscribe_success(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        module_id = self._make_module(dal)
        dal.commit()
        sub_id = svc.subscribe_module(dal, community_id, module_id, installed_by=1)
        svc.unsubscribe_module(dal, community_id, sub_id)
        assert svc.list_community_subscriptions(dal, community_id) == []

    def test_update_no_fields_raises_400(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        module_id = self._make_module(dal)
        dal.commit()
        sub_id = svc.subscribe_module(dal, community_id, module_id, installed_by=1)
        with pytest.raises(ApiError) as excinfo:
            svc.update_module_subscription(dal, community_id, sub_id, config=None, is_enabled=None)
        assert excinfo.value.status_code == 400

    def test_update_missing_subscription_raises_404(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        with pytest.raises(ApiError) as excinfo:
            svc.update_module_subscription(dal, community_id, 999, config=None, is_enabled=False)
        assert excinfo.value.status_code == 404

    def test_update_is_enabled(self, seeded: Any) -> None:
        dal, _tenant_id, community_id = seeded
        module_id = self._make_module(dal)
        dal.commit()
        sub_id = svc.subscribe_module(dal, community_id, module_id, installed_by=1)
        svc.update_module_subscription(dal, community_id, sub_id, config=None, is_enabled=False)
        rows = svc.list_community_subscriptions(dal, community_id)
        assert rows[0].isEnabled is False


class TestDiscountCodes:
    def _vendor(self, dal: Any) -> int:
        vendor_id = int(dal.hub_users.insert(username="v", email="v@x.com"))
        dal.commit()
        return vendor_id

    def test_create_requires_code(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        with pytest.raises(ApiError):
            svc.create_discount_code(
                dal,
                vendor_id,
                code="",
                discount_type="percentage",
                discount_value=10,
                module_id=None,
                valid_from=None,
                valid_until=None,
                max_uses=None,
                description=None,
            )

    def test_create_invalid_type_raises(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        with pytest.raises(ApiError):
            svc.create_discount_code(
                dal,
                vendor_id,
                code="X",
                discount_type="bogus",
                discount_value=10,
                module_id=None,
                valid_from=None,
                valid_until=None,
                max_uses=None,
                description=None,
            )

    def test_create_percentage_over_100_raises(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        with pytest.raises(ApiError):
            svc.create_discount_code(
                dal,
                vendor_id,
                code="X",
                discount_type="percentage",
                discount_value=150,
                module_id=None,
                valid_from=None,
                valid_until=None,
                max_uses=None,
                description=None,
            )

    def test_create_and_duplicate_conflict(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        svc.create_discount_code(
            dal,
            vendor_id,
            code="save10",
            discount_type="percentage",
            discount_value=10,
            module_id=None,
            valid_from=None,
            valid_until=None,
            max_uses=None,
            description=None,
        )
        with pytest.raises(ApiError) as excinfo:
            svc.create_discount_code(
                dal,
                vendor_id,
                code="SAVE10",
                discount_type="percentage",
                discount_value=5,
                module_id=None,
                valid_from=None,
                valid_until=None,
                max_uses=None,
                description=None,
            )
        assert excinfo.value.status_code == 409

    def test_create_free_type_allows_zero_value(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        dto = svc.create_discount_code(
            dal,
            vendor_id,
            code="FREEBIE",
            discount_type="free",
            discount_value=0,
            module_id=None,
            valid_from=None,
            valid_until=None,
            max_uses=None,
            description=None,
        )
        assert dto.discountType == "free"

    def test_list_pagination_and_status_filter(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        for i in range(3):
            svc.create_discount_code(
                dal,
                vendor_id,
                code=f"CODE{i}",
                discount_type="percentage",
                discount_value=10,
                module_id=None,
                valid_from=None,
                valid_until=None,
                max_uses=None,
                description=None,
            )
        page = svc.list_vendor_discount_codes(dal, vendor_id, page=1, limit=2, status="all")
        assert page.total == 3
        assert len(page.discountCodes) == 2

        active_page = svc.list_vendor_discount_codes(dal, vendor_id, status="active")
        assert active_page.total == 3
        expired_page = svc.list_vendor_discount_codes(dal, vendor_id, status="expired")
        assert expired_page.total == 0

    def test_update_not_found_raises_404(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        with pytest.raises(ApiError) as excinfo:
            svc.update_discount_code(dal, vendor_id, 999, {"description": "x"})
        assert excinfo.value.status_code == 404

    def test_update_no_changes_returns_existing(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        dto = svc.create_discount_code(
            dal,
            vendor_id,
            code="NOOP",
            discount_type="percentage",
            discount_value=10,
            module_id=None,
            valid_from=None,
            valid_until=None,
            max_uses=None,
            description=None,
        )
        updated = svc.update_discount_code(dal, vendor_id, dto.id, {})
        assert updated.discountValue == dto.discountValue

    def test_update_invalid_new_value_raises(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        dto = svc.create_discount_code(
            dal,
            vendor_id,
            code="BAD",
            discount_type="percentage",
            discount_value=10,
            module_id=None,
            valid_from=None,
            valid_until=None,
            max_uses=None,
            description=None,
        )
        with pytest.raises(ApiError):
            svc.update_discount_code(dal, vendor_id, dto.id, {"discountValue": -5})

    def test_delete_deactivates(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        dto = svc.create_discount_code(
            dal,
            vendor_id,
            code="BYE",
            discount_type="percentage",
            discount_value=10,
            module_id=None,
            valid_from=None,
            valid_until=None,
            max_uses=None,
            description=None,
        )
        deleted = svc.delete_discount_code(dal, vendor_id, dto.id)
        assert deleted.isActive is False

    def test_validate_code_not_found(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        result = svc.validate_discount_code(dal, "NOPE", None)
        assert result.valid is False
        assert result.reason == "CODE_NOT_FOUND"

    def test_validate_empty_code_raises(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        with pytest.raises(ApiError):
            svc.validate_discount_code(dal, "", None)

    def test_validate_inactive_code(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        dto = svc.create_discount_code(
            dal,
            vendor_id,
            code="INACTIVE",
            discount_type="percentage",
            discount_value=10,
            module_id=None,
            valid_from=None,
            valid_until=None,
            max_uses=None,
            description=None,
        )
        svc.delete_discount_code(dal, vendor_id, dto.id)
        result = svc.validate_discount_code(dal, "INACTIVE", None)
        assert result.valid is False
        assert result.reason == "CODE_INACTIVE"

    def test_validate_max_uses_reached(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        now = dt.datetime.utcnow()
        dal.vendor_discount_codes.insert(
            code="MAXED",
            vendor_id=vendor_id,
            discount_type="percentage",
            discount_value=10,
            max_uses=1,
            current_uses=1,
            valid_from=now,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        dal.commit()
        result = svc.validate_discount_code(dal, "MAXED", None)
        assert result.valid is False
        assert result.reason == "CODE_MAX_USES_REACHED"

    def test_validate_wrong_module(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        dto = svc.create_discount_code(
            dal,
            vendor_id,
            code="SCOPED",
            discount_type="percentage",
            discount_value=10,
            module_id=42,
            valid_from=None,
            valid_until=None,
            max_uses=None,
            description=None,
        )
        assert dto.moduleId == 42
        result = svc.validate_discount_code(dal, "SCOPED", 999)
        assert result.valid is False
        assert result.reason == "CODE_WRONG_MODULE"

    def test_validate_success(self, seeded: Any) -> None:
        dal, _t, _c = seeded
        vendor_id = self._vendor(dal)
        svc.create_discount_code(
            dal,
            vendor_id,
            code="GOOD",
            discount_type="percentage",
            discount_value=10,
            module_id=None,
            valid_from=None,
            valid_until=None,
            max_uses=5,
            description=None,
        )
        result = svc.validate_discount_code(dal, "good", None)
        assert result.valid is True
        assert result.usesRemaining == 5

    def test_redeem_free_type_zeroes_price(self, seeded: Any) -> None:
        dal, _t, community_id = seeded
        vendor_id = self._vendor(dal)
        dto = svc.create_discount_code(
            dal,
            vendor_id,
            code="FREE100",
            discount_type="free",
            discount_value=0,
            module_id=None,
            valid_from=None,
            valid_until=None,
            max_uses=None,
            description=None,
        )
        result = svc.redeem_discount_code(dal, dto.id, community_id, 1)
        assert result.discountedPriceCents == 0
        assert result.originalPriceCents == 500  # base price, 0 members -> no overage

    def test_redeem_fixed_amount_type(self, seeded: Any) -> None:
        dal, _t, community_id = seeded
        vendor_id = self._vendor(dal)
        dto = svc.create_discount_code(
            dal,
            vendor_id,
            code="FIXED2",
            discount_type="fixed_amount",
            discount_value=2.00,
            module_id=None,
            valid_from=None,
            valid_until=None,
            max_uses=None,
            description=None,
        )
        result = svc.redeem_discount_code(dal, dto.id, community_id, 1)
        assert result.discountedPriceCents == 500 - 200

    def test_redeem_unknown_code_raises_409(self, seeded: Any) -> None:
        dal, _t, community_id = seeded
        with pytest.raises(ApiError) as excinfo:
            svc.redeem_discount_code(dal, 999, community_id, 1)
        assert excinfo.value.status_code == 409

    def test_redeem_expired_raises_409(self, seeded: Any) -> None:
        dal, _t, community_id = seeded
        vendor_id = self._vendor(dal)
        now = dt.datetime.utcnow()
        code_id = dal.vendor_discount_codes.insert(
            code="EXPIRED",
            vendor_id=vendor_id,
            discount_type="percentage",
            discount_value=10,
            valid_from=now - dt.timedelta(days=10),
            valid_until=now - dt.timedelta(days=1),
            is_active=True,
            current_uses=0,
            created_at=now,
            updated_at=now,
        )
        dal.commit()
        with pytest.raises(ApiError) as excinfo:
            svc.redeem_discount_code(dal, code_id, community_id, 1)
        assert excinfo.value.status_code == 409
