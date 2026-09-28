"""The Android-style permission catalog (spec Sec1) -- stable ids, risk, family ids.

`docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
Sec1: every permission has a stable id and a risk level (`normal` shown but
never blocked on; `dangerous` requires an explicit per-id reviewer
acknowledgement at every tier that sees it for the first time, Sec3.1/3.3).
This module is the single source of truth both `bundle_manifest_v2.py`
(manifest-parse-time validation) and `bundle_permission_service.py`
(consent-flow validation) import from -- the catalog is closed, never
per-bundle-extensible (Sec2.3: "Reject an unknown permission id outright").
"""

from __future__ import annotations

import re
from typing import Literal

Risk = Literal["normal", "dangerous"]

#: Static (non-parameterized) catalog ids -> risk level. Every id in Sec1's
#: table that is NOT one of the `<family>:<param>` shapes below.
STATIC_PERMISSIONS: dict[str, Risk] = {
    "storage.kv": "normal",
    "storage.tables": "normal",
    "storage.objects": "normal",
    "overlay.media": "dangerous",
    "ai.generate": "dangerous",
    "users.profile.read": "normal",
    "telemetry.logs": "normal",
    "telemetry.metrics": "normal",
    "reputation.read": "normal",
    "reputation.community.write": "dangerous",
    "reputation.tenant.write": "dangerous",
    "flags.read": "normal",
    "platform.scheduled": "normal",
    "platform.context": "normal",
    "platform.clock": "normal",
    "platform.log": "normal",
}

#: Compiled-in relay providers `chat.send:<platform>`/`moderation.<platform>`
#: may name -- mirrors `bundle_manifest_v2`'s own provider allowlist scope
#: (kept independent here so this module has no import-cycle onto it).
RELAY_PROVIDERS = frozenset({"twitch", "discord", "kick", "youtube", "slack", "mattermost"})

_HOST_RE = re.compile(
    r"^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_CHAT_SEND_RE = re.compile(r"^chat\.send:(?P<platform>[a-z0-9_-]+)$")
_MODERATION_RE = re.compile(r"^moderation\.(?P<platform>[a-z0-9_-]+)$")
_NET_HTTP_RE = re.compile(r"^net\.http:(?P<host>.+)$")


def resolve_risk(permission_id: str) -> Risk | None:
    """The risk level for `permission_id`, or `None` if it is not a catalog member at all.

    Checks the static table first, then the three parameterized families
    (`net.http:<host>`, `chat.send:<platform>`, `moderation.<platform>`) --
    `chat.send:<platform>` is `normal` (Sec1), `net.http:<host>` and
    `moderation.<platform>` are `dangerous`.
    """
    if permission_id in STATIC_PERMISSIONS:
        return STATIC_PERMISSIONS[permission_id]

    match = _NET_HTTP_RE.match(permission_id)
    if match and _HOST_RE.match(match.group("host")) and "://" not in match.group("host"):
        return "dangerous"

    match = _CHAT_SEND_RE.match(permission_id)
    if match and match.group("platform") in RELAY_PROVIDERS:
        return "normal"

    match = _MODERATION_RE.match(permission_id)
    if match and match.group("platform") in RELAY_PROVIDERS:
        return "dangerous"

    return None


def is_valid_egress_host(host: str, *, allow_wildcard: bool = True) -> bool:
    """`True` iff `host` matches the shared egress-host grammar (`_HOST_RE`), no `://` scheme.

    `allow_wildcard=False` additionally rejects a leading `*.` label --
    used by contexts (e.g. `overlay.media.allowed_hosts`, spec Sec2.2)
    where a wildcard host is a real supply-chain risk (an iframe/overlay
    source can point anywhere under it) rather than a narrow outbound
    egress rule.
    """
    if not isinstance(host, str) or not host or "://" in host:
        return False
    if not allow_wildcard and "*" in host:
        return False
    return bool(_HOST_RE.match(host))


def is_known_permission(permission_id: str) -> bool:
    """`True` iff `permission_id` resolves to a risk level -- i.e. is a catalog member."""
    return resolve_risk(permission_id) is not None


def is_dangerous(permission_id: str) -> bool:
    """`True` iff `permission_id` is a catalog member AND `dangerous` risk.

    `False` for an unknown id -- callers that need to distinguish
    "unknown" from "normal" must call `resolve_risk` directly.
    """
    return resolve_risk(permission_id) == "dangerous"


#: `waddles.core.*` -- the reserved first-party namespace pre-granted at
#: catalog approval by `seed_core_bundles.py` (spec Sec3.6). Re-exported
#: here (not re-derived) from `vendor_bundle_authz` to avoid two sources
#: of truth for the same literal string; imported lazily to dodge any
#: import cycle since `vendor_bundle_authz` does not import this module.
CORE_NAMESPACE_PREFIX = "waddles.core."
