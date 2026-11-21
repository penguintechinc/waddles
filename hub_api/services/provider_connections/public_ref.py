"""`<puuid:connection_id>` view-arg translation shared by every provider-connection blueprint.

A connection carries no community column (communities reach it through grants), so the generic
`build_view_arg_translator` cannot scope it; this hook resolves the UUID through the grant join in
`store.resolve_public_connection_id`. Unknown, foreign and malformed UUIDs all become `UNKNOWN_ID`
and flow into the handlers' existing not-found answer (identical 404).
"""

from __future__ import annotations

import uuid

from quart import current_app, request

from services.provider_connections import store
from services.public_ids import UNKNOWN_ID, PublicId


async def translate_connection_id() -> None:
    """`before_request`: swap a `<puuid:connection_id>` view arg for the numeric id in place."""
    view_args = request.view_args
    raw = view_args.get("connection_id") if view_args else None
    if view_args is None or not isinstance(raw, PublicId):
        return None
    community_id = view_args.get("community_id")
    resolved: int | None = None
    if isinstance(community_id, int) and community_id > 0:
        resolved = await store.resolve_public_connection_id(
            current_app.config["install_dal"],
            community_id=community_id,
            public_id=uuid.UUID(str(raw)),
        )
    view_args["connection_id"] = UNKNOWN_ID if resolved is None else resolved
    return None
