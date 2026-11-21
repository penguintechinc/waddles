"""Reverse-proxy client for `core-engagement` -- backs polls + forms.

Node's `pollsController.js`/`formsController.js` are themselves pure
reverse proxies to a separate `core-engagement` service (`ENGAGEMENT_
MODULE_URL`, default `http://core-engagement:8091`) -- neither controller
touches Postgres directly. This module is the Python-side equivalent:
forward the caller's bearer token, pass through whatever `core-engagement`
returns. Because the response body's shape is owned by `core-engagement`,
not by a local ORM model, the ported blueprint routes deliberately skip
`quart-schema`'s `@validate_response` here (security.md's output-
validation rule targets accidental over-exposure of *this* service's own
model objects; a scoped pass-through of another service's already-scoped
API response is not that failure mode) and return the proxied JSON as-is.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from services.schema import bind_engagement_ownership_tables

_ENGAGEMENT_URL = os.getenv("ENGAGEMENT_MODULE_URL", "http://core-engagement:8091")
_TIMEOUT_SECONDS = 10.0


def poll_in_community(dal: Any, community_id: int, poll_id: int) -> bool:
    """True iff poll `poll_id` belongs to `community_id` (local check, before any proxying)."""
    bind_engagement_ownership_tables(dal)
    return (
        dal(
            (dal.community_polls.id == poll_id) & (dal.community_polls.community_id == community_id)
        )
        .select(dal.community_polls.id)
        .first()
        is not None
    )


def form_in_community(dal: Any, community_id: int, form_id: int) -> bool:
    """True iff form `form_id` belongs to `community_id` (local check, before any proxying)."""
    bind_engagement_ownership_tables(dal)
    return (
        dal(
            (dal.community_forms.id == form_id) & (dal.community_forms.community_id == community_id)
        )
        .select(dal.community_forms.id)
        .first()
        is not None
    )


async def _forward(
    method: str,
    path: str,
    *,
    authorization: str | None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Forward one request to `core-engagement`; degrade to an empty-list 200 on 404."""
    headers = {"Authorization": authorization} if authorization else {}
    async with httpx.AsyncClient(base_url=_ENGAGEMENT_URL, timeout=_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except httpx.RequestError:
            return {"success": False, "error": "core-engagement unavailable"}, 502

    try:
        data = resp.json()
    except ValueError:
        data = {}
    return data, resp.status_code


async def get_polls(community_id: int, authorization: str | None) -> tuple[dict[str, Any], int]:
    """Proxy `GET /api/v1/polls?community_id=` -- empty list on a 404 (matches Node)."""
    data, status = await _forward(
        "GET", "/api/v1/polls", authorization=authorization, params={"community_id": community_id}
    )
    if status == 404:
        return {"success": True, "polls": []}, 200
    return {"success": status < 400, "polls": data.get("polls", [])}, status


async def get_poll(
    community_id: int, poll_id: int, authorization: str | None
) -> tuple[dict[str, Any], int]:
    """Proxy `GET /api/v1/polls/<id>?community_id=`."""
    data, status = await _forward(
        "GET",
        f"/api/v1/polls/{poll_id}",
        authorization=authorization,
        params={"community_id": community_id},
    )
    return {"success": status < 400, "poll": data.get("poll")}, status


async def create_poll(
    community_id: int, payload: dict[str, Any], authorization: str | None
) -> tuple[dict[str, Any], int]:
    """Proxy `POST /api/v1/polls`, applying Node's default visibility/choice fields."""
    body = {
        "community_id": community_id,
        "title": payload.get("title"),
        "description": payload.get("description"),
        "options": payload.get("options"),
        "view_visibility": payload.get("view_visibility", "community"),
        "submit_visibility": payload.get("submit_visibility", "community"),
        "allow_multiple_choices": payload.get("allow_multiple_choices", False),
        "max_choices": payload.get("max_choices", 1),
        "expires_at": payload.get("expires_at"),
    }
    data, status = await _forward(
        "POST", "/api/v1/polls", authorization=authorization, json_body=body
    )
    return {"success": status < 400, "poll": data.get("poll")}, status


async def delete_poll(
    community_id: int, poll_id: int, authorization: str | None
) -> tuple[dict[str, Any], int]:
    """Proxy `DELETE /api/v1/polls/<id>?community_id=`."""
    data, status = await _forward(
        "DELETE",
        f"/api/v1/polls/{poll_id}",
        authorization=authorization,
        params={"community_id": community_id},
    )
    if status >= 400:
        return {"success": False, "error": data.get("error", "Failed to delete poll")}, status
    return {"success": True, "message": "Poll deleted"}, 200


async def get_forms(community_id: int, authorization: str | None) -> tuple[dict[str, Any], int]:
    """Proxy `GET /api/v1/forms?community_id=` -- empty list on a 404 (matches Node)."""
    data, status = await _forward(
        "GET", "/api/v1/forms", authorization=authorization, params={"community_id": community_id}
    )
    if status == 404:
        return {"success": True, "forms": []}, 200
    return {"success": status < 400, "forms": data.get("forms", [])}, status


async def get_form(
    community_id: int, form_id: int, authorization: str | None
) -> tuple[dict[str, Any], int]:
    """Proxy `GET /api/v1/forms/<id>?community_id=`."""
    data, status = await _forward(
        "GET",
        f"/api/v1/forms/{form_id}",
        authorization=authorization,
        params={"community_id": community_id},
    )
    return {"success": status < 400, "form": data.get("form")}, status


async def create_form(
    community_id: int, payload: dict[str, Any], authorization: str | None
) -> tuple[dict[str, Any], int]:
    """Proxy `POST /api/v1/forms`, applying Node's default visibility fields."""
    body = {
        "community_id": community_id,
        "title": payload.get("title"),
        "description": payload.get("description"),
        "fields": payload.get("fields"),
        "view_visibility": payload.get("view_visibility", "community"),
        "submit_visibility": payload.get("submit_visibility", "community"),
        "results_visibility": payload.get("results_visibility", "submitter_and_admins"),
        "allow_anonymous": payload.get("allow_anonymous", False),
        "submit_once_per_user": payload.get("submit_once_per_user", True),
    }
    data, status = await _forward(
        "POST", "/api/v1/forms", authorization=authorization, json_body=body
    )
    return {"success": status < 400, "form": data.get("form")}, status


async def delete_form(
    community_id: int, form_id: int, authorization: str | None
) -> tuple[dict[str, Any], int]:
    """Proxy `DELETE /api/v1/forms/<id>?community_id=`."""
    data, status = await _forward(
        "DELETE",
        f"/api/v1/forms/{form_id}",
        authorization=authorization,
        params={"community_id": community_id},
    )
    if status >= 400:
        return {"success": False, "error": data.get("error", "Failed to delete form")}, status
    return {"success": True, "message": "Form deleted"}, 200


async def get_form_submissions(
    community_id: int, form_id: int, authorization: str | None
) -> tuple[dict[str, Any], int]:
    """Proxy `GET /api/v1/forms/<id>/submissions?community_id=` -- empty list on a 404."""
    data, status = await _forward(
        "GET",
        f"/api/v1/forms/{form_id}/submissions",
        authorization=authorization,
        params={"community_id": community_id},
    )
    if status == 404:
        return {"success": True, "submissions": []}, 200
    return {"success": status < 400, "submissions": data.get("submissions", [])}, status
