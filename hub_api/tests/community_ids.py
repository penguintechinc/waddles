"""Test helpers for community PUBLIC-UUID URLs.

Client-facing community routes take `communities.public_id` (`<cuuid:community_id>`), never the
numeric id. Tests still seed communities by numeric id; these helpers translate at the URL
boundary so a converted test changes only its URL construction:

    from tests.community_ids import cpath, public_id

    url = cpath(db, "/api/v1/admin/{community_id}/members", cid)   # UUID in the path
    pid = public_id(db, cid)                                       # the community's UUID
    install_community_ids(app, dal)   # in the app fixture, BEFORE register_blueprint(...)
    assert body["community_id"] == pid                             # response DTO check

`db` may be the async DAL wrapper fixture (has `.dal`) or the raw pydal `dal`. A missing
`public_id` (sqlite has no DB default) is generated and committed once, so it stays stable.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydal import Field
from quart import Quart

from services.community_public_id import install_community_public_ids

#: Valid-shape UUID guaranteed to match no seeded community (unknown-community 404 tests).
UNKNOWN_PUBLIC_ID = "00000000-0000-4000-8000-00000000dead"


def _raw_dal(db: Any) -> Any:
    """The raw pydal DAL for either the async wrapper fixture or the DAL itself."""
    return getattr(db, "dal", db)


def _ensure_public_id_column(dal: Any) -> None:
    """Give a minimal sqlite `communities` fixture table the `public_id` column + binding."""
    if "public_id" in dal.communities.fields:
        return
    physical = [row[1] for row in dal.executesql("PRAGMA table_info(communities)")]
    if "public_id" not in physical:
        dal.executesql("ALTER TABLE communities ADD COLUMN public_id VARCHAR(36)")
        dal.commit()
    dal.define_table(
        "communities",
        *[field.clone() for field in dal.communities if field.name != "id"],
        Field("public_id", "string", length=36),
        redefine=True,
        migrate=False,
    )


def public_id(db: Any, community_id: int) -> str:
    """`communities.public_id` for `community_id`, generating + committing one when NULL."""
    dal = _raw_dal(db)
    _ensure_public_id_column(dal)
    row = (
        dal(dal.communities.id == community_id)
        .select(dal.communities.id, dal.communities.public_id)
        .first()
    )
    assert row is not None, f"community {community_id} is not seeded in this test DB"
    if row.public_id:
        return str(row.public_id)
    generated = str(uuid.uuid4())
    dal(dal.communities.id == community_id).update(public_id=generated)
    dal.commit()
    return generated


def cpath(db: Any, template: str, community_id: int, **fmt: Any) -> str:
    """Format `template`'s `{community_id}` placeholder with the community's public UUID.

    Extra `{name}` placeholders are filled from `fmt`, e.g.
    `cpath(db, "/api/v1/admin/{community_id}/members/{user_id}", cid, user_id=7)`.
    """
    return template.format(community_id=public_id(db, community_id), **fmt)


class _SyncAsyncDal:
    """`select_async` over a plain in-thread pydal DAL (bare-app test fixtures set only `dal`)."""

    def __init__(self, dal: Any) -> None:
        self.dal = dal

    async def select_async(self, query: Any, *fields: Any, **kwargs: Any) -> Any:
        """Run the select inline; sqlite `:memory:` DALs are only visible to their own thread."""
        return query.select(*fields, **kwargs)


def install_community_ids(app: Quart, dal: Any) -> None:
    """Install the `cuuid` converter + id-translation hook on a bare test app.

    Call it before `app.register_blueprint(...)` (the converter must exist when rules register).
    Also adds the `public_id` column to a minimal fixture `communities` table. Sets
    `config["async_dal"]` to an in-thread adapter only when the fixture has not set one, so
    fixtures that already provide a real `AsyncDAL` keep it.
    """
    _ensure_public_id_column(dal)
    if "async_dal" not in app.config:
        app.config["async_dal"] = _SyncAsyncDal(dal)
    app.config.setdefault("dal", dal)
    install_community_public_ids(app)


_COMMUNITY_KEYS = frozenset({"communityId", "community_id"})
_UUID_RE = __import__("re").compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", __import__("re").I
)


def assert_no_numeric_community_ids(body: Any, *, expect_at_least: int = 1) -> int:
    """Walk a JSON response: every `communityId`/`community_id` must be a UUID string (or null).

    Fails when a numeric community id leaks, and when fewer than `expect_at_least` community id
    keys were examined (a response with none proves nothing -- zero examined is a failure).
    Returns the number of community id keys examined.
    """
    examined = 0

    def walk(node: Any, path: str) -> None:
        nonlocal examined
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}"
                if key in _COMMUNITY_KEYS and value is not None:
                    examined += 1
                    assert isinstance(value, str) and _UUID_RE.match(
                        value
                    ), f"{here} must be a community UUID string, got {value!r}"
                walk(value, here)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                walk(item, f"{path}[{idx}]")

    walk(body, "$")
    assert (
        examined >= expect_at_least
    ), f"examined {examined} community id keys, expected >= {expect_at_least}"
    return examined
