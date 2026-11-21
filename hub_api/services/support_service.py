"""Support-ticket service -- port of Node's `supportController.js`.

Categories, tickets, comments, and stats for the community support-ticket
system. Query style matches `services/community_common.py`'s established
convention for this port wave: synchronous `dal` (not `AsyncDAL`) called
from inside async blueprint handlers, pydal query builder only (Gotcha #1
in `hub_api/PORTING.md` -- `AsyncDAL`'s raw-SQL helpers hardcode `%s`
placeholders, Postgres-only), every write followed by an explicit
`dal.commit()`.

`support_tickets`/`support_ticket_categories`/`support_ticket_comments`
are created at Node runtime startup (`admin/hub_module/backend/src/
index.js`'s `initializeDatabase()`), not by a numbered SQL migration --
see `services/schema.py::bind_support_token_tables()`'s own docstring.

Node's ticket listing/stats queries use Postgres-only SQL (`ORDER BY CASE
t.priority WHEN 'critical' THEN 0 ...`, `COUNT(*) FILTER (WHERE ...)`) that
the pydal query builder cannot express directly and `AsyncDAL.executesql`
would break on sqlite (Gotcha #1 again). Both are ported as: fetch the
filtered/matching row set via the pydal query builder, then rank/aggregate
in Python -- portable across every `DB_TYPE`, and the only form testable
against this repo's sqlite test fixtures.

**SECURITY FIX** (not in Node's source -- see `hub_api/PORTING.md`'s
"faithful port BUT fix vulns" mandate): Node's `getTicket()` and
`addComment()` never verify that the caller viewing/commenting on a ticket
via the member-facing `/support/my-tickets/:ticketId` routes is actually
that ticket's reporter -- any authenticated tenant member who knows or
guesses a ticket id can read another member's ticket (including its
non-internal comment thread) or post a comment onto it. `get_ticket()`/
`add_comment()` below take an optional `require_reporter_id` -- the
member-facing blueprint routes pass the caller's own id; a mismatch (or a
ticket belonging to someone else) raises `not_found()` rather than
`forbidden()`, so a probing caller can't distinguish "wrong owner" from
"doesn't exist" (avoids confirming another member's ticket ids exist).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .community_common import ensure_community_tables
from .errors import bad_request, not_found
from .schema import bind_support_token_tables

_VALID_STATUSES = ("open", "in_progress", "waiting", "resolved", "closed")
# Controller-level enum (`supportController.js`'s own `validStatuses`/
# `validPriorities` arrays) is the source of truth ported here. Node's
# *route*-level validator (`routes/support.js`'s `validators.text('priority',
# {pattern: /^(low|medium|high|urgent)$/})`) disagrees with its own
# controller (accepts "urgent", rejects "critical") -- a pre-existing Node
# drift between the two layers, not introduced by this port. The
# controller's enum is authoritative since that's the layer that actually
# persists the value.
_VALID_PRIORITIES = ("low", "medium", "high", "critical")
_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(slots=True, frozen=True)
class TicketStats:
    """Aggregate ticket counts + average resolution time for a community."""

    total: int
    open: int
    in_progress: int
    waiting: int
    resolved: int
    closed: int
    avg_resolution_seconds: float | None


def _bind(dal: Any) -> None:
    ensure_community_tables(dal)
    bind_support_token_tables(dal)


def _category_names(dal: Any, category_ids: set[int]) -> dict[int, str]:
    """`{category_id: name}` for every id in `category_ids` -- avoids a JOIN.

    Selecting fields from two tables via `left=` nests the returned `Row`
    under `row.<tablename>.<field>` (PORTING.md Gotcha #6) -- a second,
    single-table query + a Python-side dict merge sidesteps that entirely
    and is simpler to test.
    """
    ids = {c for c in category_ids if c is not None}
    if not ids:
        return {}
    rows = dal(dal.support_ticket_categories.id.belongs(ids)).select(
        dal.support_ticket_categories.id, dal.support_ticket_categories.name
    )
    return {r.id: r.name for r in rows}


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def list_categories(dal: Any, community_id: int) -> list[Any]:
    """`GET /support/categories` -- ordered `sort_order` then `name`, matching Node."""
    _bind(dal)
    rows = dal(dal.support_ticket_categories.community_id == community_id).select(
        orderby=(dal.support_ticket_categories.sort_order | dal.support_ticket_categories.name)
    )
    return list(rows)


def create_category(
    dal: Any,
    community_id: int,
    *,
    name: str | None,
    description: str | None,
    sort_order: int | None,
    form_fields: list[Any] | None,
) -> Any:
    """`POST /support/categories`."""
    _bind(dal)
    clean_name = (name or "").strip()
    if not clean_name:
        raise bad_request("Category name is required")
    new_id = int(
        dal.support_ticket_categories.insert(
            community_id=community_id,
            name=clean_name,
            description=description,
            sort_order=sort_order or 0,
            form_fields=form_fields or [],
            created_at=datetime.utcnow(),
        )
    )
    dal.commit()
    return dal.support_ticket_categories[new_id]


def update_category(
    dal: Any,
    community_id: int,
    category_id: int,
    *,
    name: str | None,
    description: str | None,
    sort_order: int | None,
    is_active: bool | None,
    form_fields: list[Any] | None,
) -> Any:
    """`PUT /support/categories/<id>`.

    COALESCE semantics: an omitted field keeps its old value.
    """
    _bind(dal)
    row = (
        dal(
            (dal.support_ticket_categories.id == category_id)
            & (dal.support_ticket_categories.community_id == community_id)
        )
        .select()
        .first()
    )
    if row is None:
        raise not_found("Category not found")

    updates: dict[str, Any] = {}
    # `name || null` in Node -- an empty/whitespace name also falls back
    # to the existing value, not just an entirely-absent field.
    if name is not None and name.strip():
        updates["name"] = name.strip()
    if description is not None:
        updates["description"] = description
    if sort_order is not None:
        updates["sort_order"] = sort_order
    if is_active is not None:
        updates["is_active"] = is_active
    if form_fields is not None:
        updates["form_fields"] = form_fields

    if updates:
        dal(dal.support_ticket_categories.id == category_id).update(**updates)
        dal.commit()
    return dal.support_ticket_categories[category_id]


def delete_category(dal: Any, community_id: int, category_id: int) -> None:
    """`DELETE /support/categories/<id>`."""
    _bind(dal)
    row = (
        dal(
            (dal.support_ticket_categories.id == category_id)
            & (dal.support_ticket_categories.community_id == community_id)
        )
        .select()
        .first()
    )
    if row is None:
        raise not_found("Category not found")
    dal(dal.support_ticket_categories.id == category_id).delete()
    dal.commit()


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------


def list_tickets(
    dal: Any,
    community_id: int,
    *,
    status: str | None,
    priority: str | None,
    category_id: int | None,
    assignee_user_id: int | None,
    search: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Any], dict[int, str], int]:
    """`GET /support/tickets` (admin) -- filters + priority-then-recency ordering.

    Ordering can't be expressed portably via pydal's query builder (Node's
    `ORDER BY CASE t.priority ...` is Postgres-only SQL) -- fetches every
    row matching the filters, ranks by priority in Python (stable sort
    preserves the `updated_at DESC` tiebreak already applied by the DB-level
    `orderby`), then slices for `limit`/`offset`. See this module's own
    docstring.
    """
    _bind(dal)
    query = dal.support_tickets.community_id == community_id
    if status:
        query &= dal.support_tickets.status == status
    if priority:
        query &= dal.support_tickets.priority == priority
    if category_id is not None:
        query &= dal.support_tickets.category_id == category_id
    if assignee_user_id is not None:
        query &= dal.support_tickets.assignee_user_id == assignee_user_id
    if search:
        like = f"%{search}%"
        query &= dal.support_tickets.subject.like(
            like, case_sensitive=False
        ) | dal.support_tickets.ticket_number.like(like, case_sensitive=False)

    rows = list(dal(query).select(orderby=~dal.support_tickets.updated_at))
    total = len(rows)
    rows.sort(key=lambda r: _PRIORITY_RANK.get(r.priority, 4))
    page = rows[offset : offset + limit]
    names = _category_names(dal, {r.category_id for r in page})
    return page, names, total


def get_ticket(
    dal: Any,
    community_id: int,
    ticket_id: int,
    *,
    include_internal: bool,
    require_reporter_id: int | None = None,
) -> tuple[Any, str | None, list[Any]]:
    """`GET /support/tickets/<id>` (admin) / `.../my-tickets/<id>` (member).

    `require_reporter_id` -- see this module's docstring (SECURITY FIX):
    when set, a ticket that exists but belongs to a different reporter
    raises `not_found()`, same as a ticket that doesn't exist at all.
    """
    _bind(dal)
    ticket = (
        dal(
            (dal.support_tickets.id == ticket_id)
            & (dal.support_tickets.community_id == community_id)
        )
        .select()
        .first()
    )
    if ticket is None:
        raise not_found("Ticket not found")
    if require_reporter_id is not None and ticket.reporter_user_id != require_reporter_id:
        raise not_found("Ticket not found")

    comment_query = dal.support_ticket_comments.ticket_id == ticket_id
    if not include_internal:
        comment_query &= dal.support_ticket_comments.is_internal == False  # noqa: E712
    comments = list(dal(comment_query).select(orderby=dal.support_ticket_comments.created_at))

    category_name = _category_names(dal, {ticket.category_id}).get(ticket.category_id)
    return ticket, category_name, comments


def create_ticket(
    dal: Any,
    community_id: int,
    *,
    category_id: int | None,
    subject: str | None,
    description: str | None,
    priority: str | None,
    reporter_user_id: int | None,
    reporter_name: str | None,
    reporter_email: str | None,
    custom_fields: dict[str, Any] | None,
) -> Any:
    """`POST /support/submit`."""
    _bind(dal)
    clean_subject = (subject or "").strip()
    if not clean_subject:
        raise bad_request("Subject is required")

    if category_id is not None:
        owned = (
            dal(
                (dal.support_ticket_categories.id == category_id)
                & (dal.support_ticket_categories.community_id == community_id)
            )
            .select(dal.support_ticket_categories.id)
            .first()
        )
        if owned is None:  # a category of another community must never be attached here
            raise not_found("Category not found")

    count = dal(dal.support_tickets.community_id == community_id).count()
    ticket_number = f"SUP-{count + 1:05d}"
    now = datetime.utcnow()
    new_id = int(
        dal.support_tickets.insert(
            community_id=community_id,
            category_id=category_id,
            ticket_number=ticket_number,
            subject=clean_subject,
            description=description,
            priority=priority or "medium",
            status="open",
            reporter_user_id=reporter_user_id,
            reporter_name=reporter_name,
            reporter_email=reporter_email,
            custom_fields=custom_fields or {},
            created_at=now,
            updated_at=now,
        )
    )
    dal.commit()
    return dal.support_tickets[new_id]


def _get_ticket_in_community(dal: Any, community_id: int, ticket_id: int) -> Any:
    row = (
        dal(
            (dal.support_tickets.id == ticket_id)
            & (dal.support_tickets.community_id == community_id)
        )
        .select()
        .first()
    )
    if row is None:
        raise not_found("Ticket not found")
    return row


def update_ticket_status(dal: Any, community_id: int, ticket_id: int, status: str | None) -> Any:
    """`PUT /support/tickets/<id>/status`."""
    _bind(dal)
    if status not in _VALID_STATUSES:
        raise bad_request(f"Invalid status. Must be one of: {', '.join(_VALID_STATUSES)}")
    _get_ticket_in_community(dal, community_id, ticket_id)
    now = datetime.utcnow()
    updates: dict[str, Any] = {"status": status, "updated_at": now}
    if status == "resolved":
        updates["resolved_at"] = now
    dal(dal.support_tickets.id == ticket_id).update(**updates)
    dal.commit()
    return dal.support_tickets[ticket_id]


def assign_ticket(dal: Any, community_id: int, ticket_id: int, assignee_user_id: int | None) -> Any:
    """`PUT /support/tickets/<id>/assign`."""
    _bind(dal)
    _get_ticket_in_community(dal, community_id, ticket_id)
    dal(dal.support_tickets.id == ticket_id).update(
        assignee_user_id=assignee_user_id, updated_at=datetime.utcnow()
    )
    dal.commit()
    return dal.support_tickets[ticket_id]


def update_ticket_priority(
    dal: Any, community_id: int, ticket_id: int, priority: str | None
) -> Any:
    """`PUT /support/tickets/<id>/priority`."""
    _bind(dal)
    if priority not in _VALID_PRIORITIES:
        raise bad_request(f"Invalid priority. Must be one of: {', '.join(_VALID_PRIORITIES)}")
    _get_ticket_in_community(dal, community_id, ticket_id)
    dal(dal.support_tickets.id == ticket_id).update(priority=priority, updated_at=datetime.utcnow())
    dal.commit()
    return dal.support_tickets[ticket_id]


def add_comment(
    dal: Any,
    community_id: int,
    ticket_id: int,
    *,
    content: str | None,
    is_internal: bool,
    author_user_id: int | None,
    author_name: str | None,
    require_reporter_id: int | None = None,
) -> Any:
    """`POST /support/tickets/<id>/comments` (admin) / `.../my-tickets/<id>/comments` (member).

    `require_reporter_id` -- see this module's docstring (SECURITY FIX).
    """
    _bind(dal)
    clean_content = (content or "").strip()
    if not clean_content:
        raise bad_request("Comment content is required")

    ticket = _get_ticket_in_community(dal, community_id, ticket_id)
    if require_reporter_id is not None and ticket.reporter_user_id != require_reporter_id:
        raise not_found("Ticket not found")

    now = datetime.utcnow()
    new_id = int(
        dal.support_ticket_comments.insert(
            ticket_id=ticket_id,
            author_user_id=author_user_id,
            author_name=author_name,
            content=clean_content,
            is_internal=bool(is_internal),
            created_at=now,
        )
    )
    dal(dal.support_tickets.id == ticket_id).update(updated_at=now)
    dal.commit()
    return dal.support_ticket_comments[new_id]


def get_my_tickets(dal: Any, community_id: int, user_id: int) -> list[tuple[Any, str | None]]:
    """`GET /support/my-tickets`."""
    _bind(dal)
    rows = list(
        dal(
            (dal.support_tickets.community_id == community_id)
            & (dal.support_tickets.reporter_user_id == user_id)
        ).select(orderby=~dal.support_tickets.updated_at)
    )
    names = _category_names(dal, {r.category_id for r in rows})
    return [(r, names.get(r.category_id)) for r in rows]


def get_ticket_stats(dal: Any, community_id: int) -> TicketStats:
    """`GET /support/stats` -- Postgres `FILTER`/`AVG(EXTRACT(EPOCH ...))` ported to Python.

    See this module's own docstring (Gotcha #1): fetches the community's
    tickets' `status`/`resolved_at`/`created_at` and aggregates locally
    rather than a Postgres-only `executesql` aggregate query.
    """
    _bind(dal)
    rows = dal(dal.support_tickets.community_id == community_id).select(
        dal.support_tickets.status, dal.support_tickets.resolved_at, dal.support_tickets.created_at
    )
    counts = {status: 0 for status in _VALID_STATUSES}
    durations: list[float] = []
    total = 0
    for row in rows:
        total += 1
        if row.status in counts:
            counts[row.status] += 1
        if row.resolved_at is not None and row.created_at is not None:
            durations.append((row.resolved_at - row.created_at).total_seconds())

    avg_resolution = sum(durations) / len(durations) if durations else None
    return TicketStats(
        total=total,
        open=counts["open"],
        in_progress=counts["in_progress"],
        waiting=counts["waiting"],
        resolved=counts["resolved"],
        closed=counts["closed"],
        avg_resolution_seconds=avg_resolution,
    )
