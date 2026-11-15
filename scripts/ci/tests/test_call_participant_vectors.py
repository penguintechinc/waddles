"""Pins the per-community call-participant pseudonym shared by the Rust host and hub-api.

The Rust host (`core/svc_process/src/identity.rs::call_participant_id`) and hub-api
(`hub_api/services/call_participants.py::identity_uuid`) must derive the same UUID for the
same (tenant, community, user); both are checked against one vector file.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VECTORS = json.loads((ROOT / "docs/contracts/call_participant_id_vectors.json").read_text("utf-8"))
HUB_API = ROOT / "hub_api/services/call_participants.py"


def test_vectors_match_python_uuid5() -> None:
    """Every vector equals the hub-api formula."""
    ns = uuid.UUID(VECTORS["namespace"])
    assert VECTORS["vectors"], "zero vectors examined"
    for v in VECTORS["vectors"]:
        got = uuid.uuid5(ns, f"{v['tenant']}:{v['community_id']}:{v['user_id']}")
        assert str(got) == v["participant"]


def test_namespace_matches_hub_api_source_when_present() -> None:
    """When hub-api's module is on this branch its namespace literal must equal the vector's."""
    if not HUB_API.exists():  # stacked on #1326; the Rust test still pins the vector
        return
    m = re.search(r'_PARTICIPANT_NS\s*=\s*uuid\.UUID\("([0-9a-f-]{36})"\)', HUB_API.read_text("utf-8"))
    assert m, "namespace literal not found in hub_api/services/call_participants.py"
    assert m.group(1) == VECTORS["namespace"]
