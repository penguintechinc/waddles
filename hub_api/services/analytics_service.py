"""Service layer for the Analytics-module port (M9) -- port of `analyticsController.js`.

One function per Node controller action (`analyticsController.js`'s own
8 exports), each composing `services/analytics_proxy.py::
AnalyticsCoreProxyClient` calls the way the Node controller composed
`analyticsService.js` calls, then reshaping the response the same way
Node's `res.json({success: true, ...(data.data || data)})` does. See
`blueprints/v1/analytics.py`'s module docstring for the full auth/
security-fix rationale; this module is pure composition + the one
tenant/membership-scoping helper (`community_member_exists`) the
blueprint's Scenario 2 routes call before ever touching `analytics-core`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from services.analytics_proxy import AnalyticsCoreProxyClient, ProxyResult
from services.community_common import api_error


def _unwrap(body: Any) -> dict[str, Any]:
    """Port of `data.data || data` -- unwrap `flask_core.api_utils.success_response`'s envelope.

    `analytics-core` wraps every response as `{success, data, timestamp}`;
    Node's controllers unwrap `.data` when present, else use the body
    as-is (defensive fallback for a non-enveloped/malformed response --
    never raises, matches Node's permissive `||` fallback).
    """
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return dict(body["data"])
    if isinstance(body, dict):
        return body
    return {}


def _upstream_error(result: ProxyResult) -> tuple[dict[str, Any], int]:
    """Map a failed `analytics-core` call to the right client-facing response.

    Transport failures (connect error / timeout) and upstream 5xx are an
    upstream-dependency fault -> 502 naming `analytics-core`. An upstream 4xx
    is a genuine client error and passes through unchanged (same status, same
    JSON envelope; a synthesized envelope only when the body wasn't JSON) so
    it is never masked as a dependency failure.
    """
    if not result.transport_error and 400 <= result.status_code < 500:
        if isinstance(result.body, dict):
            return result.body, result.status_code
        return api_error("Request rejected by analytics-core", result.status_code)
    if result.transport_error:
        message = "Upstream dependency analytics-core is unreachable or timed out"
    else:
        message = f"Upstream dependency analytics-core returned HTTP {result.status_code}"
    return api_error(message, 502)


def _relay(result: ProxyResult) -> tuple[dict[str, Any], int]:
    """Port of every simple pass-through controller action's `try/catch` -> `res.json`.

    `ok=False` fails LOUD, never as a masked 500 (gh-894): connection/timeout
    failures and upstream 5xx -> 502 naming the upstream dependency; an
    upstream 4xx passes through unchanged (see `_upstream_error`).
    """
    if not result.ok:
        return _upstream_error(result)
    return {"success": True, **_unwrap(result.body)}, 200


async def platform_overview(client: AnalyticsCoreProxyClient) -> tuple[dict[str, Any], int]:
    """Port of `getPlatformOverview` -- 3 concurrent analytics-core calls, reshaped.

    Faithful port including Node's own dead-code quirk: `activity` is
    fetched (matching `Promise.all`'s 3-way concurrency -- and its
    rejection DOES fail the whole request, same as here) but its result is
    discarded. `platformBreakdown`/`communityTypes` are hardcoded empty
    arrays in Node today, not derived from `activity` at all. Preserved
    verbatim (migration plan: "no behavior changes"), not silently
    "fixed" to use the fetched data.
    """
    summary_result, reputation_result, activity_result = await asyncio.gather(
        client.get("/api/v1/analytics/platform/summary"),
        client.get("/api/v1/analytics/platform/reputation"),
        client.get("/api/v1/analytics/platform/activity"),
    )
    for failed in (summary_result, reputation_result, activity_result):
        if not failed.ok:
            return _upstream_error(failed)

    summary = _unwrap(summary_result.body)
    reputation = _unwrap(reputation_result.body)
    return {
        "success": True,
        "summary": summary.get("summary", summary),
        "reputationTiers": reputation.get("histogram", []),
        "platformBreakdown": [],
        "communityTypes": [],
    }, 200


async def reputation_distribution(client: AnalyticsCoreProxyClient) -> tuple[dict[str, Any], int]:
    """Port of `getReputationDistribution`."""
    return _relay(await client.get("/api/v1/analytics/platform/reputation"))


async def growth_trends(
    client: AnalyticsCoreProxyClient, period: str
) -> tuple[dict[str, Any], int]:
    """Port of `getGrowthTrends` -- `period` forwarded as-is, analytics-core validates it."""
    return _relay(await client.get("/api/v1/analytics/platform/growth", query={"period": period}))


async def activity_breakdown(client: AnalyticsCoreProxyClient) -> tuple[dict[str, Any], int]:
    """Port of `getActivityBreakdown`."""
    return _relay(await client.get("/api/v1/analytics/platform/activity"))


async def community_health_summaries(
    client: AnalyticsCoreProxyClient, limit: int
) -> tuple[dict[str, Any], int]:
    """Port of `getCommunityHealthSummaries` -- `limit` already clamped by the caller."""
    return _relay(
        await client.get("/api/v1/analytics/platform/community-health", query={"limit": str(limit)})
    )


async def user_self_stats(
    client: AnalyticsCoreProxyClient, hub_user_id: int, caller_id: int, caller_role: str
) -> tuple[dict[str, Any], int]:
    """Port of `getUserSelfStats` -- serves both `/me/stats` (self) and `/admin/users/*/stats`."""
    return _relay(
        await client.get(
            f"/api/v1/analytics/user/{hub_user_id}/self",
            caller_user_id=caller_id,
            caller_role=caller_role,
        )
    )


async def user_community_stats(
    client: AnalyticsCoreProxyClient, hub_user_id: int, community_id: int, caller_id: int
) -> tuple[dict[str, Any], int]:
    """Port of `getUserCommunityStats`.

    Node hardcodes `callerRole='community_admin'` regardless of the
    actual caller (superadmin, tenant-admin, or community admin) --
    preserved verbatim here, not "fixed" to derive the real role.
    """
    return _relay(
        await client.get(
            f"/api/v1/analytics/user/{hub_user_id}/in-community/{community_id}",
            caller_user_id=caller_id,
            caller_role="community_admin",
        )
    )


async def user_reputation(
    client: AnalyticsCoreProxyClient, hub_user_id: int, caller_id: int, caller_role: str
) -> tuple[dict[str, Any], int]:
    """Port of `getUserReputation` -- serves `/me/*`, community-member, and admin mounts."""
    return _relay(
        await client.get(
            f"/api/v1/analytics/user/{hub_user_id}/reputation",
            caller_user_id=caller_id,
            caller_role=caller_role,
        )
    )


def community_member_exists(dal: Any, community_id: int, hub_user_id: int) -> bool:
    """True iff `hub_user_id` is an active member of `community_id`.

    SECURITY (fixed during this port, not a faithful reproduction of a
    Node bug): Node's `requireCommunityAdmin` dynamically resolves the
    CALLER's own role on the specific `communityId` in the URL. This
    port's `require_scope("community.analytics:admin")` is, like every
    other Community-module blueprint already in this repo
    (`community_activity.py` et al.), a STATIC JWT scope -- it proves the
    caller holds a community-admin-shaped grant somewhere, not that they
    administer THIS community or that the target user belongs to it.
    `community_in_tenant()` (`community_common.py`) closes the parallel
    cross-TENANT gap for `community_id` itself; this closes the cross-
    MEMBERSHIP gap for the `(community_id, user_id)` pair -- without it, a
    caller could pair an arbitrary `user_id` with a community they
    legitimately administer and still receive whatever `analytics-core`
    computes for that pairing, since `analytics-core` trusts hub-api
    completely and performs no membership check of its own.

    `community_members.user_id` is bound as a string (legacy platform-
    identity membership model, see `schema.py`/`community_common.py`'s own
    field comments) -- callers pass `str(hub_user_id)`, matching every
    other query against this column in this codebase.
    """
    row = (
        dal(
            (dal.community_members.community_id == community_id)
            & (dal.community_members.user_id == str(hub_user_id))
            & (dal.community_members.is_active == True)  # noqa: E712 - pydal Field comparison
        )
        .select()
        .first()
    )
    return row is not None
