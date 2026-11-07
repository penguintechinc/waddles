"""v1 internal EventSub reconcile contract for the Twitch platform bundle's reconcile hook.

* ``POST /api/v1/internal/eventsub/reconcile`` -- sweep desired state (one bounded page of
  communities) and return the create/delete work plus the Twitch ids the ledger considers live.
* ``POST /api/v1/internal/eventsub/results`` -- report create/delete/failed/revoked outcomes.
* ``GET  /api/v1/internal/eventsub/secret/<callback_key>`` -- the ingest verifier's webhook
  secret lookup (separate, narrower scope).

Machine JWT / verified SVID only, ``system`` tenant, scope ``eventsub:reconcile`` (the secret
route: ``eventsub:secret-read``). Ids are numeric tenant/community/connection (data-plane
convention) plus the subscription ``public_id`` UUID; no raw broadcaster id or PII exists here --
the bundle resolves the broadcaster/app token through the connection resolver. Enums are int codes.
The webhook secret is NEVER in the reconcile response (it must not enter guest memory); only the
host (svc-ingest) reads it, via the secret route, and injects it host-side. ``no-store``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

from flask_core.api_utils import error_response
from penguin_dal import AsyncDB
from quart import Blueprint, Response, current_app
from quart_schema import validate_request, validate_response

from services import eventsub_desired_state as es
from services.machine_auth import machine_authenticated

logger = logging.getLogger(__name__)

eventsub_internal_bp = Blueprint(
    "v1_eventsub_internal", __name__, url_prefix="/api/v1/internal/eventsub"
)

SCOPE_RECONCILE = "eventsub:reconcile"
SCOPE_SECRET_READ = "eventsub:secret-read"  # noqa: S105 -- scope name, not a credential


@dataclass(slots=True, frozen=True)
class ReconcileRequest:
    """Body of the reconcile call (paging into the community sweep)."""

    afterCommunityId: int = 0
    limit: int = es.MAX_WORK


@dataclass(slots=True, frozen=True)
class WorkItemDTO:
    """One create (``action`` 1) or delete (``action`` 2). Never carries the webhook secret."""

    subscriptionId: str
    action: int
    tenantId: int
    communityId: int
    connectionId: int
    subType: int
    twitchType: str
    version: int
    callbackUrl: str
    callbackKey: str
    nonce: str
    twitchSubscriptionId: str | None = None


@dataclass(slots=True, frozen=True)
class SweepDTO:
    """Denominators of the sweep."""

    communities: int
    created: int
    markedDelete: int
    removed: int
    markedStale: int
    requeued: int
    nextCommunityId: int | None = None


@dataclass(slots=True, frozen=True)
class ReconcileResponse:
    """Work plan plus the swept communities' live Twitch ids; GC only if ``enabledIdsComplete``."""

    success: bool
    sweep: SweepDTO
    work: list[WorkItemDTO] = field(default_factory=list)
    enabledTwitchSubscriptionIds: list[str] = field(default_factory=list)
    enabledIdsComplete: bool = False


@dataclass(slots=True, frozen=True)
class ReportItem:
    """Outcome code: 1 created, 2 deleted, 3 failed, 4 revoked; echo the work item's ``nonce``."""

    subscriptionId: str
    outcome: int
    twitchSubscriptionId: str | None = None
    failureReason: int | None = None
    nonce: str | None = None


@dataclass(slots=True, frozen=True)
class ResultsRequest:
    """Body of the results call; applied atomically."""

    results: list[ReportItem] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ResultsResponse:
    """Number of results applied."""

    success: bool
    applied: int


@dataclass(slots=True, frozen=True)
class SecretDTO:
    """Webhook secret for one callback key."""

    success: bool
    secret: str = field(repr=False, default="")


_auth = machine_authenticated(
    SCOPE_RECONCILE,
    unconfigured_event="eventsub.service_auth_unconfigured",
    rejected_event="eventsub.service_auth_rejected",
    wrong_tenant_event="eventsub.service_auth_wrong_tenant",
)
_auth_secret = machine_authenticated(
    SCOPE_SECRET_READ,
    unconfigured_event="eventsub.secret_auth_unconfigured",
    rejected_event="eventsub.secret_auth_rejected",
    wrong_tenant_event="eventsub.secret_auth_wrong_tenant",
)


@eventsub_internal_bp.after_request
async def _no_store(response: Response) -> Response:
    """Every response may carry a secret: never cacheable."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _dal() -> AsyncDB:
    return cast(AsyncDB, current_app.config["install_dal"])


def _base_url() -> str:
    return str(current_app.config["HUB_API_CONFIG"].eventsub_callback_base_url)


def _refusal(exc: es.EventSubError) -> tuple[dict[str, object], int]:
    """Map a service refusal to the fixed JSON error envelope."""
    if isinstance(exc, es.NotConfiguredError):
        status, code, message = 503, "NOT_CONFIGURED", "eventsub callback url is not configured"
    elif isinstance(exc, es.UnknownSubscriptionError):
        status, code, message = 404, "NOT_FOUND", "unknown subscription"
    else:
        status, code, message = 400, "BAD_REQUEST", "invalid report"
    return cast(tuple[dict[str, object], int], error_response(message, status, code))


@eventsub_internal_bp.route("/reconcile", methods=["POST"])
@_auth
@validate_request(ReconcileRequest)
@validate_response(ReconcileResponse)
async def reconcile_route(data: ReconcileRequest) -> Any:
    """Sweep desired state and return the pending create/delete work."""
    if data.afterCommunityId < 0 or not 1 <= data.limit <= es.MAX_WORK:
        return error_response("afterCommunityId/limit out of range", 400, "BAD_REQUEST")
    try:
        summary, work, enabled = await es.reconcile(
            _dal(),
            base_url=_base_url(),
            after_community_id=data.afterCommunityId,
            limit=data.limit,
        )
    except es.EventSubError as exc:
        return _refusal(exc)
    return ReconcileResponse(
        success=True,
        sweep=SweepDTO(
            communities=summary.communities,
            created=summary.created,
            markedDelete=summary.marked_delete,
            removed=summary.removed,
            markedStale=summary.marked_stale,
            requeued=summary.requeued,
            nextCommunityId=summary.next_community_id,
        ),
        work=[
            WorkItemDTO(
                subscriptionId=w.public_id,
                action=int(w.action),
                tenantId=w.tenant_id,
                communityId=w.community_id,
                connectionId=w.connection_id,
                subType=int(w.sub_type),
                twitchType=w.twitch_type,
                version=w.version,
                callbackUrl=w.callback_url,
                callbackKey=w.callback_key,
                nonce=w.nonce,
                twitchSubscriptionId=w.twitch_subscription_id,
            )
            for w in work
        ],
        enabledTwitchSubscriptionIds=enabled.ids,
        enabledIdsComplete=enabled.complete,
    )


@eventsub_internal_bp.route("/results", methods=["POST"])
@_auth
@validate_request(ResultsRequest)
@validate_response(ResultsResponse)
async def results_route(data: ResultsRequest) -> Any:
    """Apply create/delete/failed/revoked outcomes atomically."""
    if not 1 <= len(data.results) <= es.MAX_WORK:
        return error_response(
            f"results must contain between 1 and {es.MAX_WORK} entries", 400, "BAD_REQUEST"
        )
    try:
        reports = [
            es.Report(
                public_id=r.subscriptionId,
                outcome=es.Outcome(r.outcome),
                twitch_subscription_id=r.twitchSubscriptionId,
                nonce=r.nonce,
                failure_reason=(
                    es.FailureReason(r.failureReason) if r.failureReason is not None else None
                ),
            )
            for r in data.results
        ]
    except ValueError:
        return error_response("unknown outcome or failure reason", 400, "BAD_REQUEST")
    try:
        applied = await es.report_results(_dal(), reports)
    except es.EventSubError as exc:
        return _refusal(exc)
    return ResultsResponse(success=True, applied=applied)


@eventsub_internal_bp.route("/secret/<callback_key>", methods=["GET"])
@_auth_secret
@validate_response(SecretDTO)
async def secret_route(callback_key: str) -> Any:
    """Webhook secret for the ingest verifier (404 for an unknown key)."""
    try:
        return SecretDTO(
            success=True, secret=await es.secret_for_callback_key(_dal(), callback_key)
        )
    except es.EventSubError as exc:
        return _refusal(exc)


BLUEPRINTS = [eventsub_internal_bp]
