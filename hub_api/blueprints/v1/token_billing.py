"""v1 `marketplace` token billing group -- the metered-token-billing subsystem.

Full-feature build (not a Node port -- net-new, no `api.js` pinned
contract, so DTO fields are plain snake_case per PEP8, not the camelCase
convention M1..M9's byte-faithful ports use). Mounted alongside the other
`v1_marketplace_*` groups (`marketplace_catalog`, `marketplace_billing`,
...): `token_products_bp` at `/api/v1/marketplace/token-products` (global
catalog), `tokens_bp` at `/api/v1/marketplace/communities/<community_id>/
tokens/*` (per-community balance/credit/debit/history).

Auth pattern (`hub_api/PORTING.md`'s "Admin/elevated action" row, DB-
backed variant): every community-scoped route is `@tenant_middleware`
plus an explicit `services.community_authz.authorize_community()` call
inside the handler -- the SAME pattern `blueprints/v1/music.py`/
`streaming.py` use, not a flat `@require_scope("community:admin")`
(`community_authz.py`'s own module docstring explains why a flat scope
claim can't answer "admin of *which* community", and would be exactly
the IDOR class this port already documents elsewhere). `authorize_community
(..., admin=True)` for admin/financial actions (credit, transaction
history); `admin=False` (active member) for balance reads and
consumption (debit) -- a community member spending their own community's
tokens on a premium feature is a normal member action, not an admin one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import tenant_middleware
from quart import Blueprint, current_app, request
from quart_schema import validate_request, validate_response

from services import token_billing_service as svc
from services.community_authz import authorize_community
from services.errors import ApiError, bad_request

token_products_bp = Blueprint(
    "v1_marketplace_token_products", __name__, url_prefix="/api/v1/marketplace/token-products"
)
tokens_bp = Blueprint(
    "v1_marketplace_tokens", __name__, url_prefix="/api/v1/marketplace/communities"
)

BLUEPRINTS = [token_products_bp, tokens_bp]


def _dal() -> tuple[Any, Any]:
    """Return `(async_dal, dal)` from app config -- same helper every other group defines."""
    return current_app.config["async_dal"], current_app.config["dal"]


def _err(exc: ApiError) -> tuple[dict[str, object], int]:
    """Convert an `ApiError` into the shared `{success, error}` JSON envelope."""
    return cast(
        tuple[dict[str, object], int], error_response(exc.message, exc.status_code, exc.code)
    )


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TokenProductDTO:
    """One catalog entry -- wire shape for `token_billing_service.TokenProductDTO`."""

    id: int
    key: str
    name: str
    unit: str
    price_cents: int
    tokens_granted: int
    active: bool


@dataclass(slots=True, frozen=True)
class TokenProductsResponse:
    """Response DTO for the token-products catalog."""

    success: bool
    products: list[TokenProductDTO]


@dataclass(slots=True, frozen=True)
class TokenBalanceDTO:
    """One product's balance for the requested community."""

    product_id: int
    product_key: str
    product_name: str
    unit: str
    balance: int
    updated_at: str | None


@dataclass(slots=True, frozen=True)
class TokenBalancesResponse:
    """Response DTO for a community's token balances."""

    success: bool
    community_id: int
    balances: list[TokenBalanceDTO]


@dataclass(slots=True, frozen=True)
class CreditTokensRequest:
    """Request DTO for granting tokens (admin action or purchase-flow grant)."""

    product_key: str
    amount: int
    reason: str
    ref: str | None = None


@dataclass(slots=True, frozen=True)
class DebitTokensRequest:
    """Request DTO for spending tokens (a metered feature's own consumption call)."""

    product_key: str
    amount: int
    reason: str
    ref: str | None = None


@dataclass(slots=True, frozen=True)
class TokenLedgerResponse:
    """Response DTO for a successful credit/debit -- deliberately flat.

    Flat (no nested dataclass field) so `@validate_response` is safe even
    though the underlying service call awaits `insert_async()`
    (`hub_api/PORTING.md` Gotcha #3 -- the crash class is specific to a
    NESTED dataclass response field, and "flat responses... were
    empirically confirmed safe with normal @validate_response" per that
    gotcha's own writeup).
    """

    success: bool
    balance_after: int
    transaction_id: int


@dataclass(slots=True, frozen=True)
class TokenTransactionDTO:
    """One ledger row."""

    id: int
    community_id: int
    product_id: int
    product_key: str
    delta: int
    reason: str
    ref: str | None
    balance_after: int
    created_at: str | None


@dataclass(slots=True, frozen=True)
class TokenTransactionsResponse:
    """Response DTO for paginated ledger history."""

    success: bool
    transactions: list[TokenTransactionDTO]
    total: int
    limit: int
    offset: int


#: `TokenLedgerResult.blocked_reason` -> (HTTP status, human message). 503 for
#: `REASON_LEDGER_UNAVAILABLE` (never a hard crash of the calling route --
#: see `token_billing_service`'s own module docstring on BLOCK-WITH-FALLBACK).
_BLOCKED_STATUS: dict[str, int] = {
    svc.REASON_UNKNOWN_PRODUCT: 404,
    svc.REASON_INSUFFICIENT_BALANCE: 402,
    svc.REASON_NO_BALANCE: 402,
    svc.REASON_LEDGER_UNAVAILABLE: 503,
}
_BLOCKED_MESSAGES: dict[str, str] = {
    svc.REASON_UNKNOWN_PRODUCT: "Unknown or inactive token product.",
    svc.REASON_INSUFFICIENT_BALANCE: "Insufficient token balance for this action.",
    svc.REASON_NO_BALANCE: "This community has no balance for this token product yet.",
    svc.REASON_LEDGER_UNAVAILABLE: (
        "Token ledger is temporarily unavailable -- please try again shortly."
    ),
}


def _blocked_response(result: svc.TokenLedgerResult) -> tuple[dict[str, object], int]:
    """Render a blocked `TokenLedgerResult` as the HTTP response -- never a raw 500.

    BLOCK-WITH-FALLBACK: insufficient/no balance is 402 with an
    `upgrade_path` pointing the caller at the token catalog (refill
    path); ledger-unavailable is 503, distinguishable from a genuine
    "you're out of tokens" so a UI can retry rather than upsell.
    """
    reason = result.blocked_reason or svc.REASON_LEDGER_UNAVAILABLE
    status = _BLOCKED_STATUS.get(reason, 503)
    body: dict[str, object] = {
        "success": False,
        "blocked_reason": reason,
        "message": _BLOCKED_MESSAGES.get(reason, "Token spend blocked."),
    }
    if reason in (svc.REASON_INSUFFICIENT_BALANCE, svc.REASON_NO_BALANCE):
        body["upgrade_path"] = "/api/v1/marketplace/token-products"
    return body, status


def _parse_iso(raw: str | None, *, field: str) -> datetime | None:
    """Parse an optional `?start=`/`?end=` ISO-8601 query param; 400 on malformed input."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise bad_request(f"Invalid {field} -- expected ISO-8601") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Routes -- token-products catalog
# ---------------------------------------------------------------------------


@token_products_bp.route("", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(TokenProductsResponse)
async def list_token_products() -> TokenProductsResponse:
    """The active token-product catalog -- any authenticated tenant caller."""
    async_dal, dal = _dal()
    products = await svc.list_products(async_dal, dal)
    return TokenProductsResponse(
        success=True,
        products=[
            TokenProductDTO(
                id=p.id,
                key=p.key,
                name=p.name,
                unit=p.unit,
                price_cents=p.price_cents,
                tokens_granted=p.tokens_granted,
                active=p.active,
            )
            for p in products
        ],
    )


# ---------------------------------------------------------------------------
# Routes -- per-community balance / credit / debit / history
# ---------------------------------------------------------------------------


@tokens_bp.route("/<int:community_id>/tokens/balances", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(TokenBalancesResponse)
async def get_balances(
    community_id: int,
) -> TokenBalancesResponse | tuple[dict[str, object], int]:
    """A community's balance for every active token product -- any active member."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=False)
        balances = await svc.list_balances(async_dal, dal, community_id=community_id)
    except ApiError as exc:
        return _err(exc)
    return TokenBalancesResponse(
        success=True,
        community_id=community_id,
        balances=[
            TokenBalanceDTO(
                product_id=b.product_id,
                product_key=b.product_key,
                product_name=b.product_name,
                unit=b.unit,
                balance=b.balance,
                updated_at=b.updated_at,
            )
            for b in balances
        ],
    )


@tokens_bp.route("/<int:community_id>/tokens/credit", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(CreditTokensRequest)
@validate_response(TokenLedgerResponse)
async def credit_tokens(
    data: CreditTokensRequest, community_id: int
) -> TokenLedgerResponse | tuple[dict[str, object], int]:
    """Grant tokens to a community -- community-admin action (manual grant or purchase credit)."""
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)
        result = await svc.credit_tokens(
            async_dal,
            dal,
            community_id=community_id,
            product_key=data.product_key,
            amount=data.amount,
            reason=data.reason,
            ref=data.ref,
        )
    except ApiError as exc:
        return _err(exc)
    if not result.ok:
        return _blocked_response(result)
    assert result.balance_after is not None and result.transaction_id is not None  # noqa: S101
    return TokenLedgerResponse(
        success=True, balance_after=result.balance_after, transaction_id=result.transaction_id
    )


@tokens_bp.route("/<int:community_id>/tokens/debit", methods=["POST"])
@tenant_middleware  # type: ignore[misc]
@validate_request(DebitTokensRequest)
@validate_response(TokenLedgerResponse)
async def debit_tokens(
    data: DebitTokensRequest, community_id: int
) -> TokenLedgerResponse | tuple[dict[str, object], int]:
    """Spend tokens from a community's balance -- the HTTP face of a metered feature's own spend.

    Community-member action (`admin=False`): a member consuming a
    premium feature on their own community's behalf, not a financial/
    admin operation. Other backend features that meter their own
    consumption should call `token_billing_service.debit_tokens()`
    directly instead of round-tripping through this HTTP endpoint.
    """
    async_dal, dal = _dal()
    try:
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=False)
        result = await svc.debit_tokens(
            async_dal,
            dal,
            community_id=community_id,
            product_key=data.product_key,
            amount=data.amount,
            reason=data.reason,
            ref=data.ref,
        )
    except ApiError as exc:
        return _err(exc)
    if not result.ok:
        return _blocked_response(result)
    assert result.balance_after is not None and result.transaction_id is not None  # noqa: S101
    return TokenLedgerResponse(
        success=True, balance_after=result.balance_after, transaction_id=result.transaction_id
    )


@tokens_bp.route("/<int:community_id>/tokens/transactions", methods=["GET"])
@tenant_middleware  # type: ignore[misc]
@validate_response(TokenTransactionsResponse)
async def get_transactions(
    community_id: int,
) -> TokenTransactionsResponse | tuple[dict[str, object], int]:
    """Paginated, filtered ledger history -- community-admin action (financial audit trail)."""
    async_dal, dal = _dal()
    product_key = request.args.get("product_key") or None

    try:
        # Authz before any query-param validation -- an unauthorized
        # caller gets the same 403 regardless of whether their query
        # string happens to be malformed, never a 400 that would confirm
        # "you reached the validation logic" ahead of the authz check.
        await authorize_community(request, async_dal, dal, community_id=community_id, admin=True)

        try:
            limit = int(request.args.get("limit", "50"))
            offset = int(request.args.get("offset", "0"))
        except ValueError as exc:
            raise bad_request("limit/offset must be integers") from exc
        if limit <= 0 or limit > 200:
            raise bad_request("limit must be between 1 and 200")
        if offset < 0:
            raise bad_request("offset must be >= 0")
        start = _parse_iso(request.args.get("start"), field="start")
        end = _parse_iso(request.args.get("end"), field="end")

        rows, total = await svc.list_transactions(
            async_dal,
            dal,
            community_id=community_id,
            product_key=product_key,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
    except ApiError as exc:
        return _err(exc)
    return TokenTransactionsResponse(
        success=True,
        transactions=[
            TokenTransactionDTO(
                id=r.id,
                community_id=r.community_id,
                product_id=r.product_id,
                product_key=r.product_key,
                delta=r.delta,
                reason=r.reason,
                ref=r.ref,
                balance_after=r.balance_after,
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
