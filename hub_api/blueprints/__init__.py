"""hub-api blueprint mounting registry.

`register_blueprints(app)` is the single call that wires every versioned
API group onto the Quart app. `routers/v1.py`/`routers/v2.py` themselves
auto-discover their groups (`routers/_discovery.py`) -- the 54 remaining
controllers (docs/plans/2026-08-31-hubapi-node-to-quart-migration.md's
per-phase sequence, M1..M9) land by dropping ONE new module,
`blueprints/v1/<group>.py` or `blueprints/v2/<group>.py`, exposing a
module-level `BLUEPRINTS: list[Blueprint]` -- never by editing
`routers/v1.py`, `routers/v2.py`, or this file. That's the whole
extension point: no shared file for ~10 parallel port agents to collide
on. Health, MCP, and the two OpenAPI documents are infra-level
blueprints wired directly in `app.py` alongside this call, not through
here, since they aren't versioned business API groups.
"""

from __future__ import annotations

from quart import Quart

from routers.v1 import register_v1
from routers.v2 import register_v2
from services.community_public_id import register_default_converter

# `<cuuid:community_id>` rules must parse for any app that registers a v1 blueprint.
register_default_converter()


def register_blueprints(app: Quart) -> None:
    """Mount every API version group (v1 frozen, v2 additive) onto `app`."""
    register_v1(app)
    register_v2(app)
