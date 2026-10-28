"""`services/marketplace_analytics_service.py` -- unit tests against a seeded sqlite DAL.

Exercises branches the blueprint-level tests (empty-state happy paths)
don't reach: period cutoffs (`mtd`/`ytd`/`all`), install time-series
bucketing across granularities, discount-code active/expired
classification, and community-drilldown sorting/pagination.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services import marketplace_analytics_service as analytics
from services.schema import bind_marketplace_module_tables, bind_marketplace_vendor_tables


@pytest.fixture
def dal() -> Any:
    from pydal import DAL

    db = DAL("sqlite:memory")
    bind_marketplace_module_tables(db, migrate=True)
    bind_marketplace_vendor_tables(db, migrate=True)
    yield db
    db.close()


def _seed_seller_with_module(dal: Any, *, user_id: int = 1) -> tuple[int, int]:
    seller_id: int = dal.marketplace_sellers.insert(user_id=user_id, display_name="V")
    module_id: int = dal.marketplace_modules.insert(
        seller_id=seller_id,
        name="Mod",
        slug="mod",
        webhook_url="https://vendor.example.com/hook",
        webhook_secret="s",
    )
    dal.commit()
    return seller_id, module_id


class TestSalesMetricsPeriods:
    def test_mtd_and_ytd_periods_compute_without_error(self, dal: Any) -> None:
        _, module_id = _seed_seller_with_module(dal)
        now = datetime.now(UTC)
        dal.community_vendor_installations.insert(
            community_id=1, module_id=module_id, status="active", installed_at=now
        )
        dal.commit()

        for period in ("mtd", "ytd", "all", "90d"):
            result = analytics.get_sales_metrics(dal, 1, period=period)
            assert result is not None
            assert result["installations"]["total"] >= 0

    def test_uninstalled_and_active_counted_separately(self, dal: Any) -> None:
        _, module_id = _seed_seller_with_module(dal)
        now = datetime.now(UTC)
        dal.community_vendor_installations.insert(
            community_id=1,
            module_id=module_id,
            status="uninstalled",
            installed_at=now - timedelta(days=1),
            uninstalled_at=now,
        )
        dal.community_vendor_installations.insert(
            community_id=2, module_id=module_id, status="active", installed_at=now
        )
        dal.vendor_payments.insert(
            seller_id=1, module_id=module_id, amount_cents=500, status="completed", paid_at=now
        )
        dal.commit()

        result = analytics.get_sales_metrics(dal, 1, period="30d")
        assert result is not None
        assert result["installations"]["uninstalls"] == 1
        assert result["installations"]["active"] == 1
        assert result["revenue"]["totalCents"] == 500


class TestInstallTimeSeriesBucketing:
    @pytest.mark.parametrize("granularity", ["day", "week", "month", "bogus"])
    def test_buckets_installs_by_granularity(self, dal: Any, granularity: str) -> None:
        _, module_id = _seed_seller_with_module(dal)
        now = datetime.now(UTC)
        dal.community_vendor_installations.insert(
            community_id=1, module_id=module_id, status="active", installed_at=now
        )
        dal.commit()

        series = analytics.get_install_time_series(dal, 1, period="30d", granularity=granularity)
        assert len(series) == 1
        assert series[0]["installs"] == 1

    def test_no_seller_returns_empty_list(self, dal: Any) -> None:
        assert analytics.get_install_time_series(dal, 999) == []


class TestDiscountCodePerformance:
    def test_active_and_expired_codes_classified(self, dal: Any) -> None:
        now = datetime.now(UTC)
        active_code = dal.vendor_discount_codes.insert(
            code="ACTIVE10",
            vendor_id=1,
            discount_type="percentage",
            discount_value=10,
            is_active=True,
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=30),
            created_at=now,
            updated_at=now,
        )
        expired_code = dal.vendor_discount_codes.insert(
            code="EXPIRED",
            vendor_id=1,
            discount_type="fixed_amount",
            discount_value=5,
            is_active=True,
            valid_from=now - timedelta(days=30),
            valid_until=now - timedelta(days=1),
            created_at=now,
            updated_at=now,
        )
        dal.discount_code_redemptions.insert(
            discount_code_id=active_code,
            community_id=1,
            original_price_cents=500,
            discounted_price_cents=400,
            discount_amount_cents=100,
            redeemed_at=now,
        )
        dal.marketplace_sellers.insert(user_id=1, display_name="V")
        dal.commit()

        result = analytics.get_discount_code_performance(dal, 1)
        assert result["summary"]["active"] == 1
        assert result["summary"]["expired"] == 1
        codes_by_code = {c["code"]: c for c in result["codes"]}
        assert codes_by_code["ACTIVE10"]["totalRedemptions"] == 1
        assert codes_by_code["ACTIVE10"]["totalDiscountCents"] == 100
        _ = expired_code

    def test_no_seller_returns_empty_summary(self, dal: Any) -> None:
        result = analytics.get_discount_code_performance(dal, 999)
        assert result == {"codes": [], "summary": {"active": 0, "expired": 0}}


class TestCommunityDrilldown:
    def test_sorts_and_paginates(self, dal: Any) -> None:
        _, module_id = _seed_seller_with_module(dal)
        now = datetime.now(UTC)
        for i in range(3):
            dal.community_vendor_installations.insert(
                community_id=i,
                module_id=module_id,
                status="active",
                installed_at=now - timedelta(days=i),
            )
        dal.commit()

        result = analytics.get_community_drilldown(dal, 1, page=1, limit=2, sort_by="status")
        assert result["total"] == 3
        assert len(result["rows"]) == 2

    def test_filters_by_module_id(self, dal: Any) -> None:
        seller_id, module_id = _seed_seller_with_module(dal)
        other_module_id: int = dal.marketplace_modules.insert(
            seller_id=seller_id,
            name="Other",
            slug="other",
            webhook_url="https://vendor.example.com/hook2",
            webhook_secret="s",
        )
        dal.community_vendor_installations.insert(
            community_id=1, module_id=module_id, status="active", installed_at=datetime.now(UTC)
        )
        dal.community_vendor_installations.insert(
            community_id=2,
            module_id=other_module_id,
            status="active",
            installed_at=datetime.now(UTC),
        )
        dal.commit()

        result = analytics.get_community_drilldown(dal, 1, module_id=other_module_id)
        assert result["total"] == 1
        assert result["rows"][0]["moduleId"] == other_module_id

    def test_no_seller_returns_empty(self, dal: Any) -> None:
        result = analytics.get_community_drilldown(dal, 999)
        assert result == {"rows": [], "total": 0, "page": 1, "limit": 25}

    def test_seller_with_no_modules_returns_empty(self, dal: Any) -> None:
        dal.marketplace_sellers.insert(user_id=42, display_name="Empty")
        dal.commit()
        result = analytics.get_community_drilldown(dal, 42)
        assert result == {"rows": [], "total": 0, "page": 1, "limit": 25}


class TestExportCsv:
    def test_export_installs_with_data(self, dal: Any) -> None:
        _, module_id = _seed_seller_with_module(dal)
        dal.community_vendor_installations.insert(
            community_id=1, module_id=module_id, status="active", installed_at=datetime.now(UTC)
        )
        dal.commit()
        csv_text, filename = analytics.export_analytics_csv(dal, 1, export_type="installs")
        assert "installs,uninstalls" in csv_text
        assert filename.startswith("install-timeseries")

    def test_export_sales_no_seller(self, dal: Any) -> None:
        csv_text, filename = analytics.export_analytics_csv(dal, 999, export_type="sales")
        assert csv_text == "metric,value\n"
        assert filename == "sales-30d.csv"


class TestApiUsageMetrics:
    def test_returns_placeholder(self) -> None:
        result = analytics.get_api_usage_metrics(period="7d")
        assert result["placeholder"] is True
        assert result["period"] == "7d"
