"""Shared helpers for the entity public-UUID route tests (convert-routes group B).

`second_community` seeds a sibling community in the SAME tenant so a test can prove that an
entity UUID owned by community B is a plain 404 through community A's URL (scope, not tenancy,
is what rejects it). `assert_uuid` is the shape check every converted response id must pass.
"""

from __future__ import annotations

import re
from typing import Any

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def assert_uuid(value: Any) -> str:
    """Fail unless `value` is a dashed UUID string (a numeric id leaking out is a defect)."""
    assert isinstance(value, str) and _UUID_RE.match(value), f"not a public UUID: {value!r}"
    return value


def second_community(dal: Any, community_id: int, name: str = "other-community") -> int:
    """Insert a sibling community under the same tenant as `community_id`; returns its id."""
    tenant_id = dal.communities[community_id].tenant_id
    other = int(dal.communities.insert(name=name, tenant_id=tenant_id))
    dal.commit()
    return other
