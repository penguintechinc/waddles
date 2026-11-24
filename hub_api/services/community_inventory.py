"""Inventory (Quartermaster) service -- port of Node's `inventoryController.js`.

**Bug fixed during the port, not reproduced:** Node's `addStock`/
`removeStock`/`checkoutItem`/`checkinItem` call the Postgres helper
functions (`config/postgres/migrations/014_add_quartermaster_tables.sql`)
with the wrong argument order/count -- e.g. `addStock` calls
`add_inventory_stock($1, $2, $3, $4)` with `[itemId, userId, quantity,
notes]` against a function signature of `(p_item_id, p_quantity,
p_user_id, p_community_id, p_reason)`, silently writing `userId` into the
`quantity` slot. `checkoutItem`/`checkinItem` are worse: `update_
inventory_on_checkout`/`update_inventory_on_return` only ever touch
`inventory_items` + `inventory_log` -- neither writes an `inventory_
checkouts` row, so Node's checkout flow never actually records a
checkout (`listAllCheckouts`/`getMyCheckouts` would never see it).
This port calls the SQL functions with their real signature and
additionally manages `inventory_checkouts` directly, per general.md's
"no shortcuts / complete features" -- see the PR description for the
call-out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .community_common import ensure_community_tables
from .public_ids import lookup_public_ids
from .sql_util import execute_positional


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


@dataclass(slots=True, frozen=True)
class InventoryItem:
    """One `inventory_items` row."""

    id: str
    name: str
    description: str | None
    item_type: str | None
    category: str | None
    quantity: int
    available_quantity: int
    metadata: dict[str, Any] | None
    created_at: str | None = None
    updated_at: str | None = None


def _item_dto(row: Any) -> InventoryItem:
    return InventoryItem(
        id=str(row.public_id),
        name=row.name,
        description=row.description,
        item_type=row.item_type,
        category=row.category,
        quantity=row.quantity,
        available_quantity=row.available_quantity,
        metadata=row.metadata,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def list_items(dal: Any, community_id: int) -> list[InventoryItem]:
    """All non-deleted items for a community, category then name."""
    ensure_community_tables(dal)
    rows = dal(
        (dal.inventory_items.community_id == community_id)
        & (dal.inventory_items.deleted_at == None)  # noqa: E711
    ).select(orderby=dal.inventory_items.category | dal.inventory_items.name)
    return [_item_dto(r) for r in rows]


def create_item(
    dal: Any, community_id: int, payload: dict[str, Any]
) -> tuple[InventoryItem | None, str | None]:
    """Insert a new item. Returns `(dto, None)` or `(None, error)`."""
    ensure_community_tables(dal)
    name = (payload.get("name") or "").strip()
    if not name:
        return None, "Item name is required"
    try:
        quantity = int(payload.get("quantity"))  # type: ignore[arg-type]  # TypeError caught below
    except (TypeError, ValueError):
        return None, "Quantity must be a non-negative number"
    if quantity < 0:
        return None, "Quantity must be a non-negative number"

    new_id = dal.inventory_items.insert(
        community_id=community_id,
        name=name,
        description=payload.get("description"),
        item_type=payload.get("item_type") or "general",
        category=payload.get("category"),
        quantity=quantity,
        available_quantity=quantity,
        metadata=payload.get("metadata") or {},
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    dal.commit()
    return _item_dto(dal.inventory_items[new_id]), None


def update_item(
    dal: Any, community_id: int, item_id: int, payload: dict[str, Any]
) -> InventoryItem | None:
    """Partial update; only fields present in `payload` change."""
    ensure_community_tables(dal)
    query = (
        (dal.inventory_items.id == item_id)
        & (dal.inventory_items.community_id == community_id)
        & (dal.inventory_items.deleted_at == None)  # noqa: E711
    )
    existing = dal(query).select().first()
    if existing is None:
        return None

    fields = {"updated_at": datetime.utcnow()}
    if "name" in payload and payload["name"]:
        fields["name"] = payload["name"].strip()
    for key in ("description", "item_type", "category"):
        if key in payload:
            fields[key] = payload[key]
    if "metadata" in payload:
        fields["metadata"] = payload["metadata"]

    dal(query).update(**fields)
    dal.commit()
    return _item_dto(dal.inventory_items[item_id])


def delete_item(dal: Any, community_id: int, item_id: int) -> bool:
    """Soft-delete. Returns `False` if no matching item existed."""
    ensure_community_tables(dal)
    query = (
        (dal.inventory_items.id == item_id)
        & (dal.inventory_items.community_id == community_id)
        & (dal.inventory_items.deleted_at == None)  # noqa: E711
    )
    if dal(query).select().first() is None:
        return False
    dal(query).update(deleted_at=datetime.utcnow())
    dal.commit()
    return True


def _run_stock_fn(
    dal: Any,
    fn_name: str,
    item_id: int,
    quantity: int,
    user_id: int,
    community_id: int,
    note: str | None,
) -> tuple[bool, str]:
    """Call `add_inventory_stock`/`remove_inventory_stock`, matching their real signature.

    `fn_name` is always one of the two hardcoded literals `add_stock`/
    `remove_stock` pass below, never caller-supplied.
    """
    rows = execute_positional(
        dal,
        "SELECT * FROM " + fn_name + "($1, $2, $3, $4, $5)",  # nosec B608  # noqa: S608
        placeholders=[item_id, quantity, user_id, community_id, note],
    )
    if not rows:
        return False, "Stock update failed"
    # Both functions return (success, ...counts..., message) -- success col 0, message last col.
    return bool(rows[0][0]), str(rows[0][-1])


def add_stock(
    dal: Any, community_id: int, item_id: int, user_id: int, quantity: int, notes: str | None
) -> tuple[InventoryItem | None, str | None]:
    """Add stock via the `add_inventory_stock` SQL function (correct argument order)."""
    ensure_community_tables(dal)
    ok, message = _run_stock_fn(
        dal, "add_inventory_stock", item_id, quantity, user_id, community_id, notes
    )
    dal.commit()
    if not ok:
        return None, message
    return _item_dto(dal.inventory_items[item_id]), None


def remove_stock(
    dal: Any, community_id: int, item_id: int, user_id: int, quantity: int, notes: str | None
) -> tuple[InventoryItem | None, str | None]:
    """Remove stock via the `remove_inventory_stock` SQL function (correct argument order)."""
    ensure_community_tables(dal)
    ok, message = _run_stock_fn(
        dal, "remove_inventory_stock", item_id, quantity, user_id, community_id, notes
    )
    dal.commit()
    if not ok:
        return None, message
    return _item_dto(dal.inventory_items[item_id]), None


@dataclass(slots=True, frozen=True)
class Checkout:
    """One `inventory_checkouts` row, optionally joined with item/user display fields."""

    id: int
    item_id: str
    user_id: int
    quantity: int
    due_date: str | None
    status: str
    notes: str | None
    checked_out_at: str | None
    returned_at: str | None = None
    item_name: str | None = None
    item_category: str | None = None
    user_name: str | None = None


def list_all_checkouts(dal: Any, community_id: int, status: str | None) -> list[Checkout]:
    """Admin view -- all checkouts for a community, optionally filtered by status."""
    ensure_community_tables(dal)
    sql = """
        SELECT ic.id, ic.item_id, ic.user_id, ic.quantity, ic.due_at, ic.status, ic.notes,
               ic.checked_out_at, ic.returned_at, ii.name, ii.category, hu.display_name,
               ii.public_id
        FROM inventory_checkouts ic
        JOIN inventory_items ii ON ic.item_id = ii.id
        JOIN hub_users hu ON ic.user_id = hu.id
        WHERE ii.community_id = $1
    """
    params: list[Any] = [community_id]
    if status:
        sql += " AND ic.status = $2"
        params.append(status)
    sql += " ORDER BY ic.checked_out_at DESC"

    rows = execute_positional(dal, sql, placeholders=params)
    return [
        Checkout(
            id=r[0],
            item_id=str(r[12]),
            user_id=r[2],
            quantity=r[3],
            due_date=_iso(r[4]),
            status=r[5],
            notes=r[6],
            checked_out_at=_iso(r[7]),
            returned_at=_iso(r[8]),
            item_name=r[9],
            item_category=r[10],
            user_name=r[11],
        )
        for r in rows
    ]


@dataclass(slots=True, frozen=True)
class InventorySummary:
    """Aggregate community inventory counters, from `get_inventory_summary()`."""

    total_items: int
    total_quantity: int
    total_available: int
    active_checkouts: int
    overdue_checkouts: int
    low_stock_items: int


def get_summary(dal: Any, community_id: int) -> InventorySummary:
    """Community-wide inventory summary via the `get_inventory_summary` SQL function."""
    ensure_community_tables(dal)
    rows = execute_positional(
        dal, "SELECT * FROM get_inventory_summary($1)", placeholders=[community_id]
    )
    if not rows:
        return InventorySummary(0, 0, 0, 0, 0, 0)
    r = rows[0]
    return InventorySummary(
        total_items=r[0],
        total_quantity=r[1],
        total_available=r[2],
        active_checkouts=r[3],
        overdue_checkouts=r[4],
        low_stock_items=r[5],
    )


@dataclass(slots=True, frozen=True)
class LogEntry:
    """One `inventory_log` audit row."""

    id: int
    item_id: str | None
    action: str
    quantity_change: int | None
    notes: dict[str, Any] | None
    created_at: str | None
    item_name: str | None
    performed_by_name: str | None


def get_audit_log(
    dal: Any, community_id: int, *, item_id: int | None, action: str | None, limit: int, offset: int
) -> list[LogEntry]:
    """Community audit trail, optionally filtered by item/action."""
    ensure_community_tables(dal)
    sql = """
        SELECT il.id, il.item_id, il.action, il.quantity_change, il.details, il.created_at,
               ii.name, hu.display_name, ii.public_id
        FROM inventory_log il
        JOIN inventory_items ii ON il.item_id = ii.id
        LEFT JOIN hub_users hu ON il.user_id = hu.id
        WHERE ii.community_id = $1
    """
    params: list[Any] = [community_id]
    if item_id:
        params.append(item_id)
        sql += f" AND il.item_id = ${len(params)}"
    if action:
        params.append(action)
        sql += f" AND il.action = ${len(params)}"
    params.extend([limit, offset])
    sql += f" ORDER BY il.created_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}"

    rows = execute_positional(dal, sql, placeholders=params)
    return [
        LogEntry(
            id=r[0],
            item_id=str(r[8]),
            action=r[2],
            quantity_change=r[3],
            notes=r[4],
            created_at=_iso(r[5]),
            item_name=r[6],
            performed_by_name=r[7],
        )
        for r in rows
    ]


def list_available(dal: Any, community_id: int, search: str | None) -> list[InventoryItem]:
    """Member view -- items with `available_quantity > 0`, optionally full-text searched."""
    ensure_community_tables(dal)
    if search:
        rows = execute_positional(
            dal, "SELECT * FROM search_inventory_items($1, $2)", placeholders=[community_id, search]
        )
        public_ids = lookup_public_ids(dal, "inventory_items", [r[0] for r in rows])
        return [
            InventoryItem(
                id=public_ids[int(r[0])],
                name=r[1],
                description=r[2],
                item_type=r[3],
                category=r[4],
                quantity=r[5],
                available_quantity=r[6],
                metadata=None,
            )
            for r in rows
        ]
    rows = dal(
        (dal.inventory_items.community_id == community_id)
        & (dal.inventory_items.deleted_at == None)  # noqa: E711
        & (dal.inventory_items.available_quantity > 0)
    ).select(orderby=dal.inventory_items.category | dal.inventory_items.name)
    return [_item_dto(r) for r in rows]


def checkout_item(
    dal: Any,
    community_id: int,
    user_id: int,
    item_id: int,
    quantity: int,
    due_date: str | None,
    notes: str | None,
) -> tuple[Checkout | None, str | None]:
    """Member checkout: decrement `available_quantity`, log, and create the checkout row."""
    ensure_community_tables(dal)
    item = (
        dal(
            (dal.inventory_items.id == item_id)
            & (dal.inventory_items.community_id == community_id)
            & (dal.inventory_items.deleted_at == None)  # noqa: E711
        )
        .select()
        .first()
    )
    if item is None:
        return None, "Item not found"
    if item.available_quantity < quantity:
        return None, "Insufficient available quantity"

    ok, message = _run_stock_fn(
        dal, "remove_inventory_stock", item_id, quantity, user_id, community_id, notes
    )
    if not ok:
        dal.commit()
        return None, message

    checkout_id = dal.inventory_checkouts.insert(
        item_id=item_id,
        user_id=user_id,
        community_id=community_id,
        quantity=quantity,
        checked_out_at=datetime.utcnow(),
        due_at=due_date,
        status="active",
        notes=notes,
    )
    dal.commit()
    row = dal.inventory_checkouts[checkout_id]
    return (
        Checkout(
            id=row.id,
            item_id=str(item.public_id),
            user_id=row.user_id,
            quantity=row.quantity,
            due_date=_iso(row.due_at),
            status=row.status,
            notes=row.notes,
            checked_out_at=_iso(row.checked_out_at),
        ),
        None,
    )


def checkin_item(
    dal: Any,
    community_id: int,
    user_id: int,
    checkout_id: int,
    quantity_returned: int | None,
    notes: str | None,
) -> tuple[Checkout | None, str | None]:
    """Member return: restore `available_quantity`, log, and close the checkout row."""
    ensure_community_tables(dal)
    row = execute_positional(
        dal,
        """
        SELECT ic.id, ic.item_id, ic.quantity
        FROM inventory_checkouts ic
        JOIN inventory_items ii ON ic.item_id = ii.id
        WHERE ic.id = $1 AND ic.user_id = $2 AND ii.community_id = $3 AND ic.status = 'active'
        """,
        placeholders=[checkout_id, user_id, community_id],
    )
    if not row:
        return None, "Active checkout not found"

    item_id, checked_out_qty = row[0][1], row[0][2]
    if quantity_returned is None:
        qty = checked_out_qty
    elif isinstance(quantity_returned, bool) or not 0 < quantity_returned <= checked_out_qty:
        # Defence in depth (the route validates too): returning more than was checked out
        # would inflate stock; zero/negative would silently deduct it.
        return None, f"Quantity returned must be between 1 and {checked_out_qty}"
    else:
        qty = quantity_returned

    ok, message = _run_stock_fn(
        dal, "add_inventory_stock", item_id, qty, user_id, community_id, notes
    )
    if not ok:
        dal.commit()
        return None, message

    dal(dal.inventory_checkouts.id == checkout_id).update(
        status="returned", returned_at=datetime.utcnow()
    )
    dal.commit()
    result = dal.inventory_checkouts[checkout_id]
    return (
        Checkout(
            id=result.id,
            item_id=lookup_public_ids(dal, "inventory_items", [result.item_id])[
                int(result.item_id)
            ],
            user_id=result.user_id,
            quantity=result.quantity,
            due_date=_iso(result.due_at),
            status=result.status,
            notes=result.notes,
            checked_out_at=_iso(result.checked_out_at),
            returned_at=_iso(result.returned_at),
        ),
        None,
    )


def get_my_checkouts(dal: Any, community_id: int, user_id: int) -> list[Checkout]:
    """Caller's own active checkouts."""
    ensure_community_tables(dal)
    rows = execute_positional(
        dal,
        """
        SELECT ic.id, ic.item_id, ic.quantity, ic.due_at, ic.status, ic.notes, ic.checked_out_at,
               ii.name, ii.category, ii.public_id
        FROM inventory_checkouts ic
        JOIN inventory_items ii ON ic.item_id = ii.id
        WHERE ii.community_id = $1 AND ic.user_id = $2 AND ic.status = 'active'
        ORDER BY ic.checked_out_at DESC
        """,
        placeholders=[community_id, user_id],
    )
    return [
        Checkout(
            id=r[0],
            item_id=str(r[9]),
            user_id=user_id,
            quantity=r[2],
            due_date=_iso(r[3]),
            status=r[4],
            notes=r[5],
            checked_out_at=_iso(r[6]),
            item_name=r[7],
            item_category=r[8],
        )
        for r in rows
    ]
