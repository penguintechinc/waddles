"""REAL `hub_users.uuid` resolution for `<uuuid:user_id>` routes (no deterministic fake).

The rest of the suite swaps the DB lookups for `tests/user_ids.py`; these tests opt out
(`real_user_ids`) and seed actual `hub_users` rows to prove: a numeric id no longer routes, a known
UUID resolves to the row's numeric id before the handler runs, an unknown UUID resolves to the
never-valid sentinel (uniform "not found", no existence oracle), and list responses emit the row's
UUID (never the numeric PK).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Blueprint, Quart, jsonify

from services.user_public_id import (
    UNKNOWN_USER_ID,
    lookup_user_public_ids,
    public_id_for,
    register_default_converter,
    use_user_public_ids,
)
from tests.user_ids import row_uuid

pytestmark = pytest.mark.real_user_ids

register_default_converter()
_probe_bp = Blueprint("user_public_id_probe", __name__, url_prefix="/probe")
use_user_public_ids(_probe_bp)


@_probe_bp.route("/users/<uuuid:user_id>", methods=["GET"])
async def _probe(user_id: int) -> Any:
    """Echo the view arg the handler receives (must be the translated numeric id)."""
    return jsonify({"type": type(user_id).__name__, "value": user_id})


def _seed_user(auth_db: Any, email: str) -> int:
    user_id: int = auth_db.dal.hub_users.insert(
        email=email,
        username=email,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    auth_db.dal.commit()
    return user_id


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    quart_app.register_blueprint(_probe_bp)
    return quart_app


async def test_known_uuid_is_translated_to_the_numeric_id(app: Quart, auth_db: Any) -> None:
    user_id = _seed_user(auth_db, "a@example.com")
    response = await app.test_client().get(f"/probe/users/{row_uuid(auth_db, user_id)}")
    assert response.status_code == 200
    assert await response.get_json() == {"type": "int", "value": user_id}


async def test_unknown_uuid_resolves_to_the_sentinel(app: Quart, auth_db: Any) -> None:
    _seed_user(auth_db, "b@example.com")
    response = await app.test_client().get(f"/probe/users/{uuid.uuid4()}")
    assert (await response.get_json())["value"] == UNKNOWN_USER_ID


async def test_numeric_id_no_longer_routes(app: Quart, auth_db: Any) -> None:
    user_id = _seed_user(auth_db, "c@example.com")
    response = await app.test_client().get(f"/probe/users/{user_id}")
    assert response.status_code == 404


async def test_uppercase_uuid_is_normalised(app: Quart, auth_db: Any) -> None:
    user_id = _seed_user(auth_db, "d@example.com")
    response = await app.test_client().get(f"/probe/users/{row_uuid(auth_db, user_id).upper()}")
    assert (await response.get_json())["value"] == user_id


async def test_public_id_lookup_emits_uuids_and_skips_unresolvable(
    app: Quart, auth_db: Any
) -> None:
    one = _seed_user(auth_db, "e@example.com")
    two = _seed_user(auth_db, "f@example.com")
    async with app.app_context():
        mapping = await lookup_user_public_ids([str(one), two, "legacy-platform-id", 999999])
    assert mapping == {one: row_uuid(auth_db, one), two: row_uuid(auth_db, two)}
    assert public_id_for(mapping, str(one)) == row_uuid(auth_db, one)
    assert public_id_for(mapping, "legacy-platform-id") is None
    assert public_id_for(mapping, 999999) is None
