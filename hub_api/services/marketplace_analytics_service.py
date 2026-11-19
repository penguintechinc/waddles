"""Vendor analytics group -- port of `vendorAnalyticsService.js`.

Node's original expresses every query as Postgres-only SQL (`DATE_TRUNC`,
`FILTER (WHERE ...)`, `generate_series`, `INTERVAL` literals) -- none of
which sqlite (this port's own test backend, `backend-database.md`: support
every `DB_TYPE`) understands. Rewritten here as portable `pydal`
query-builder reads (`hub_api/PORTING.md` Gotcha #1) with period-cutoff
date arithmetic computed in Python and passed as a plain filter value,
and date-bucketing (`get_install_time_series`'s gap-filled day/week/month
series, previously a `generate_series` CTE) done in Python instead of SQL.
Same computed results, portable across every `DB_TYPE`, exercised by this
port's own sqlite test suite -- not a byte-for-byte SQL port, a faithful
behavioral one (`hub_api/PORTING.md`'s own "document it, don't silently
drop the feature" gotcha guidance, extended from schema gaps to
portability).

`community_vendor_installations`/`vendor_payments`/`vendor_discount_codes.
module_id` are all bound against Node's EXPECTED (not migration-actual)
shape -- see `services/schema.py::bind_marketplace_vendor_tables`'s
docstring gaps (2)-(4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from services.community_public_id import lookup_public_ids_sync

_PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90}


def _period_cutoff(period: str) -> datetime | None:
    """Return the earliest-included timestamp for `period`, or `None` for 'all'."""
    now = datetime.now(UTC)
    if period in _PERIOD_DAYS:
        return now - timedelta(days=_PERIOD_DAYS[period])
    if period == "mtd":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "ytd":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return None  # 'all' or unrecognized


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _seller_id(dal: Any, user_id: int) -> int | None:
    row = dal(dal.marketplace_sellers.user_id == user_id).select(dal.marketplace_sellers.id).first()
    return int(row.id) if row else None


def _module_ids_for_seller(dal: Any, seller_id: int) -> list[int]:
    rows = dal(dal.marketplace_modules.seller_id == seller_id).select(dal.marketplace_modules.id)
    return [m.id for m in rows]


def get_sales_metrics(dal: Any, user_id: int, *, period: str = "30d") -> dict[str, Any] | None:
    """`GET /vendor/analytics/sales` -- overall sales/installation metrics, or `None` (404)."""
    seller_id = _seller_id(dal, user_id)
    if seller_id is None:
        return None
    module_ids = _module_ids_for_seller(dal, seller_id)
    installs = (
        dal(dal.community_vendor_installations.module_id.belongs(module_ids)).select()
        if module_ids
        else []
    )
    cutoff = _period_cutoff(period)

    def _in_period(dt: datetime | None) -> bool:
        return dt is not None and (cutoff is None or _aware(dt) >= cutoff)

    installs_in_period = [i for i in installs if _in_period(i.installed_at)]
    uninstalls_in_period = [
        i for i in installs if i.status == "uninstalled" and _in_period(i.uninstalled_at)
    ]
    active = [i for i in installs if i.status == "active"]

    total_installs = len(installs_in_period)
    uninstalls = len(uninstalls_in_period)
    churn_rate = round((uninstalls / total_installs) * 100, 2) if total_installs > 0 else 0.0

    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    def _count_since(cutoff_dt: datetime) -> int:
        return sum(1 for i in installs if i.installed_at and _aware(i.installed_at) >= cutoff_dt)

    payments = (
        dal(dal.vendor_payments.seller_id == seller_id).select(orderby=None) if module_ids else []
    )
    completed = [p for p in payments if p.status == "completed"]
    total_revenue = sum(p.amount_cents or 0 for p in completed)
    mtd_revenue = sum(
        p.amount_cents or 0 for p in completed if p.paid_at and _aware(p.paid_at) >= month_start
    )
    ytd_revenue = sum(
        p.amount_cents or 0 for p in completed if p.paid_at and _aware(p.paid_at) >= year_start
    )

    return {
        "period": period,
        "installations": {
            "total": total_installs,
            "active": len(active),
            "uninstalls": uninstalls,
            "churnRate": churn_rate,
            "new": {
                "today": _count_since(day_start),
                "thisWeek": _count_since(week_start),
                "mtd": _count_since(month_start),
                "ytd": _count_since(year_start),
            },
        },
        "revenue": {
            "totalCents": total_revenue,
            "mtdCents": mtd_revenue,
            "ytdCents": ytd_revenue,
        },
    }


_VALID_GRANULARITIES = frozenset({"day", "week", "month"})


def _bucket_key(dt: datetime, granularity: str) -> datetime:
    dt = _aware(dt).replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "week":
        return dt - timedelta(days=dt.weekday())
    if granularity == "month":
        return dt.replace(day=1)
    return dt


def get_install_time_series(
    dal: Any, user_id: int, *, period: str = "30d", granularity: str = "day"
) -> list[dict[str, Any]]:
    """`GET /vendor/analytics/installs` -- gap-filled install/uninstall time series."""
    seller_id = _seller_id(dal, user_id)
    if seller_id is None:
        return []
    safe_granularity = granularity if granularity in _VALID_GRANULARITIES else "day"
    module_ids = _module_ids_for_seller(dal, seller_id)
    installs = (
        dal(dal.community_vendor_installations.module_id.belongs(module_ids)).select()
        if module_ids
        else []
    )
    cutoff = _period_cutoff(period) or (datetime.now(UTC) - timedelta(days=30))

    buckets: dict[datetime, dict[str, int]] = {}
    for i in installs:
        if i.installed_at and _aware(i.installed_at) >= cutoff:
            key = _bucket_key(i.installed_at, safe_granularity)
            buckets.setdefault(key, {"installs": 0, "uninstalls": 0})["installs"] += 1
        if i.status == "uninstalled" and i.uninstalled_at and _aware(i.uninstalled_at) >= cutoff:
            key = _bucket_key(i.uninstalled_at, safe_granularity)
            buckets.setdefault(key, {"installs": 0, "uninstalls": 0})["uninstalls"] += 1

    return [
        {"date": key.isoformat(), "installs": val["installs"], "uninstalls": val["uninstalls"]}
        for key, val in sorted(buckets.items())
    ]


def get_api_usage_metrics(*, period: str = "30d") -> dict[str, Any]:
    """`GET /vendor/analytics/api-usage` -- placeholder, matches Node (no per-request tracking)."""
    return {
        "period": period,
        "placeholder": True,
        "totalRequests": 0,
        "requestsPerDay": [],
        "errorRate": 0,
        "avgResponseTimeMs": 0,
        "message": "Per-request API tracking not yet implemented.",
    }


def get_discount_code_performance(dal: Any, user_id: int) -> dict[str, Any]:
    """`GET /vendor/analytics/discount-codes` -- per-code redemption stats."""
    seller_id = _seller_id(dal, user_id)
    if seller_id is None:
        return {"codes": [], "summary": {"active": 0, "expired": 0}}

    # Filtered directly on `vendor_discount_codes.vendor_id` (the real
    # migration 064 column, -> hub_users.id) rather than Node's
    # `JOIN marketplace_modules mm ON mm.id = dc.module_id WHERE
    # mm.seller_id = $1` -- Node's join target is already wrong per
    # schema.py's bind_marketplace_vendor_tables() docstring gap (4)
    # (`dc.module_id` -> `approved_vendor_modules`, not
    # `marketplace_modules`), so reproducing it byte-for-byte would
    # silently return zero rows for every real vendor. Filtering on the
    # column that actually, correctly identifies the caller is the
    # faithful-to-intent choice, not a Gotcha #4 "silently invented"
    # deviation.
    codes = dal(dal.vendor_discount_codes.vendor_id == user_id).select()
    now = datetime.now(UTC)
    result_codes = []
    active_count = 0
    expired_count = 0
    for code in codes:
        redemptions = dal(dal.discount_code_redemptions.discount_code_id == code.id).select()
        total_redemptions = len(redemptions)
        total_discount_cents = sum(r.discount_amount_cents or 0 for r in redemptions)
        unique_communities = len({r.community_id for r in redemptions})
        conversion_rate = (
            round((total_redemptions / code.max_uses) * 100, 2)
            if code.max_uses and code.max_uses > 0
            else None
        )
        is_active = bool(code.is_active)
        valid_until = _aware(code.valid_until) if code.valid_until else None
        uses_exhausted = code.max_uses is not None and code.current_uses >= code.max_uses
        is_expired = (valid_until is not None and valid_until < now) or uses_exhausted
        if is_active and not is_expired:
            active_count += 1
        if not is_active or is_expired:
            expired_count += 1
        result_codes.append(
            {
                "id": code.id,
                "code": code.code,
                "discountType": code.discount_type,
                "discountValue": code.discount_value,
                "isActive": is_active,
                "validFrom": code.valid_from.isoformat() if code.valid_from else None,
                "validUntil": code.valid_until.isoformat() if code.valid_until else None,
                "maxUses": code.max_uses,
                "currentUses": code.current_uses,
                "totalRedemptions": total_redemptions,
                "totalDiscountCents": total_discount_cents,
                "uniqueCommunities": unique_communities,
                "conversionRate": conversion_rate,
            }
        )
    result_codes.sort(key=lambda c: -c["totalRedemptions"])
    return {"codes": result_codes, "summary": {"active": active_count, "expired": expired_count}}


_VALID_SORT_COLUMNS = frozenset({"installed_at", "status", "last_active"})


def get_community_drilldown(
    dal: Any,
    user_id: int,
    *,
    module_id: int | None = None,
    page: int = 1,
    limit: int = 25,
    sort_by: str = "installed_at",
) -> dict[str, Any]:
    """`GET /vendor/analytics/communities` -- paginated per-community install breakdown."""
    seller_id = _seller_id(dal, user_id)
    if seller_id is None:
        return {"rows": [], "total": 0, "page": page, "limit": limit}
    module_ids = _module_ids_for_seller(dal, seller_id)
    if module_id is not None:
        module_ids = [m for m in module_ids if m == module_id]
    if not module_ids:
        return {"rows": [], "total": 0, "page": page, "limit": limit}

    installs = dal(dal.community_vendor_installations.module_id.belongs(module_ids)).select()
    sort_key = sort_by if sort_by in _VALID_SORT_COLUMNS else "installed_at"
    col = {"installed_at": "installed_at", "status": "status", "last_active": "last_active_at"}[
        sort_key
    ]
    rows = sorted(installs, key=lambda r: (getattr(r, col) is None, getattr(r, col)), reverse=True)
    total = len(rows)
    offset = (page - 1) * limit
    page_rows = rows[offset : offset + limit]

    module_names = {
        m.id: m.name
        for m in dal(dal.marketplace_modules.id.belongs(module_ids)).select(
            dal.marketplace_modules.id, dal.marketplace_modules.name
        )
    }
    public_ids = lookup_public_ids_sync(dal, (r.community_id for r in page_rows))
    return {
        "rows": [
            {
                "installationId": r.id,
                "communityId": public_ids[int(r.community_id)],
                "moduleId": r.module_id,
                "moduleName": module_names.get(r.module_id),
                "status": r.status,
                "installedAt": r.installed_at.isoformat() if r.installed_at else None,
                "uninstalledAt": r.uninstalled_at.isoformat() if r.uninstalled_at else None,
                "lastActiveAt": r.last_active_at.isoformat() if r.last_active_at else None,
            }
            for r in page_rows
        ],
        "total": total,
        "page": page,
        "limit": limit,
    }


def export_analytics_csv(
    dal: Any, user_id: int, *, export_type: str = "sales", period: str = "30d"
) -> tuple[str, str]:
    """`GET /vendor/analytics/export` -- returns `(csv_text, filename)`."""
    if export_type == "installs":
        series = get_install_time_series(dal, user_id, period=period, granularity="day")
        header = "date,installs,uninstalls\n"
        body = "\n".join(
            f"{row['date'].split('T')[0]},{row['installs']},{row['uninstalls']}" for row in series
        )
        return header + body, f"install-timeseries-{period}.csv"

    metrics = get_sales_metrics(dal, user_id, period=period)
    if metrics is None:
        return "metric,value\n", f"sales-{period}.csv"

    rows = [
        ("metric", "value"),
        ("period", period),
        ("total_installations", metrics["installations"]["total"]),
        ("active_installations", metrics["installations"]["active"]),
        ("uninstalls", metrics["installations"]["uninstalls"]),
        ("churn_rate_pct", metrics["installations"]["churnRate"]),
        ("new_installs_today", metrics["installations"]["new"]["today"]),
        ("new_installs_this_week", metrics["installations"]["new"]["thisWeek"]),
        ("new_installs_mtd", metrics["installations"]["new"]["mtd"]),
        ("new_installs_ytd", metrics["installations"]["new"]["ytd"]),
        ("total_revenue_cents", metrics["revenue"]["totalCents"]),
        ("mtd_revenue_cents", metrics["revenue"]["mtdCents"]),
        ("ytd_revenue_cents", metrics["revenue"]["ytdCents"]),
    ]
    csv_text = "\n".join(f"{k},{v}" for k, v in rows)
    return csv_text, f"sales-metrics-{period}.csv"
