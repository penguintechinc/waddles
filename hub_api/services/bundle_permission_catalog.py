"""The Android-style permission catalog (spec Sec1) -- stable ids, risk, family ids.

`docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
Sec1: every permission has a stable id and a risk level (`normal` shown but
never blocked on; `dangerous` requires an explicit per-id reviewer
acknowledgement at every tier that sees it for the first time, Sec3.1/3.3).
This module is the single source of truth both `bundle_manifest_v2.py`
(manifest-parse-time validation) and `bundle_permission_service.py`
(consent-flow validation) import from -- the catalog is closed, never
per-bundle-extensible (Sec2.3: "Reject an unknown permission id outright").

Outbound HTTP is three separate, separately-approvable permission families
(2026-09-28 decision), never one bare `net.http:<host>`:
`net.http.fqdn:<host>` is the preferred, `normal`-risk form; `net.http.
public-ip:<ip>` (a bare public IP, no FQDN) and `net.http.private-ip:
<ip|cidr>` are both `dangerous` -- highlighted on every consent screen the
same as any other dangerous permission. Loopback, link-local/metadata, and
this cluster's own pod/service/node CIDRs are unconditionally denied for
both IP families regardless of consent -- a bundle can never grant itself
egress to the platform's own infrastructure.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from typing import Literal

logger = logging.getLogger(__name__)

Risk = Literal["normal", "dangerous"]

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

#: No `net.http.private-ip` grant may be coarser than this -- a /8 would
#: hand a bundle the whole RFC1918 `10.0.0.0/8` block; /16 is the widest
#: block an operator should ever need to hand a single bundle (justification
#: required on every entry regardless, Sec2.3).
_MAX_PRIVATE_PREFIX_V4 = 16
_MAX_PRIVATE_PREFIX_V6 = 64

_ALWAYS_DENIED_STATIC: tuple[IpNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("::1/128"),  # loopback (v6)
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, incl. 169.254.169.254 cloud metadata
    ipaddress.ip_network("fe80::/10"),  # link-local (v6)
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDSv2 (v6) -- outside fe80::/10
)


def _cluster_deny_networks() -> tuple[IpNetwork, ...]:
    """This cluster's own pod/service/node CIDRs -- env-configured, never hardcoded.

    Sourced from `CLUSTER_POD_CIDR`/`CLUSTER_SERVICE_CIDR`/`CLUSTER_NODE_CIDR`
    (comma-separated within each) since these differ per deployment; a
    bundle must never be able to grant itself egress to the platform's own
    infrastructure even via an otherwise-valid `net.http.private-ip` entry.
    """
    raw = ",".join(
        v
        for v in (
            os.environ.get("CLUSTER_POD_CIDR", ""),
            os.environ.get("CLUSTER_SERVICE_CIDR", ""),
            os.environ.get("CLUSTER_NODE_CIDR", ""),
        )
        if v
    )
    networks: list[IpNetwork] = []
    for cidr in raw.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _overlaps_always_denied(network: IpNetwork) -> bool:
    """`True` iff `network` overlaps loopback, link-local/metadata, or a cluster CIDR."""
    for denied in (*_ALWAYS_DENIED_STATIC, *_cluster_deny_networks()):
        if network.version == denied.version and network.overlaps(denied):
            return True
    return False


#: Static (non-parameterized) catalog ids -> risk level. Every id in Sec1's
#: table that is NOT one of the `<family>:<param>` shapes below.
STATIC_PERMISSIONS: dict[str, Risk] = {
    "storage.kv": "normal",
    "storage.tables": "normal",
    "storage.objects": "normal",
    "overlay.media": "dangerous",
    #: Read-only subscribe to svc-streaming-rust's stream-lifecycle hooks
    #: (`on-start`/`on-stop`/`on-segment`/`on-recording-ready`,
    #: `wit/waddle-bundle/stage.wit`'s `streaming-lifecycle` export,
    #: issue #456). `normal`: no outbound call, no write capability of its
    #: own -- the bundle only receives host-pushed stream metadata (stream
    #: id, platform, segment/recording URLs). Host wiring (actually linking
    #: the export, enforcing the grant) is deferred past this catalog entry.
    "streaming.lifecycle.subscribe": "normal",
    "ai.generate": "dangerous",
    #: Raw PII (not just a tenant-tokenized UUID) embedded in a form/modal/
    #: interaction input the bundle receives -- e.g. a free-text field the
    #: user typed a name/email/phone into. DEFAULT NO, `dangerous`: without
    #: this permission granted, the host filters PII out of interaction
    #: inputs before they ever reach the bundle (best-effort filtering --
    #: the host-side filter implementation is separate work, not this
    #: catalog entry). Subject to the instance-wide deny policy like any
    #: other permission (`_reject_instance_denied` in `bundle_permissions.py`).
    "interaction.pii.receive": "dangerous",
    "users.profile.read": "normal",
    "telemetry.logs": "normal",
    "telemetry.metrics": "normal",
    "reputation.read": "normal",
    "reputation.community.write": "dangerous",
    "reputation.tenant.write": "dangerous",
    #: Shared community currency (issue #714), `core/bundle_capability_gate`'s
    #: `economy.*` families. `read` = balance/leaderboard; `wager` = atomic
    #: stake-debit/payout-credit (+ `max-bet`); `transfer` = member-to-member.
    #: The two money-moving ids are `dangerous` and carry their own amount
    #: ceilings (a separate quota family from reputation's point caps).
    "economy.read": "normal",
    "economy.wager": "dangerous",
    "economy.transfer": "dangerous",
    #: Resolves the invocation's triggering actor (and an @mention target the
    #: triggering message carried) to the community `user_uuid` that
    #: `economy.*`/`reputation.*` name their targets by -- UUID-only, never a
    #: platform id/handle/name (`core/bundle_capability_gate`'s
    #: `identity.resolve`). `normal`: it exposes nothing a bundle does not
    #: already hold (the `{user:<uuid>}` placeholder), grants no write, and is
    #: rate limited; the host fails closed on unlinked/non-member/ambiguous.
    "identity.resolve": "normal",
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
_CHAT_DELETE_RE = re.compile(r"^chat\.delete:(?P<platform>[a-z0-9_-]+)$")
_DM_SEND_RE = re.compile(r"^dm\.send:(?P<platform>[a-z0-9_-]+)$")
_MODERATION_RE = re.compile(r"^moderation\.(?P<platform>[a-z0-9_-]+)$")
_NET_HTTP_FQDN_RE = re.compile(r"^net\.http\.fqdn:(?P<host>.+)$")
_NET_HTTP_PUBLIC_IP_RE = re.compile(r"^net\.http\.public-ip:(?P<ip>.+)$")
_NET_HTTP_PRIVATE_IP_RE = re.compile(r"^net\.http\.private-ip:(?P<value>.+)$")

#: Every `net.http.*` permission-id prefix, in one place -- used by
#: `bundle_manifest_v2.py` for the shared `params.methods` gate and by the
#: IP-literal advisory-warning check.
NET_HTTP_PREFIXES = ("net.http.fqdn:", "net.http.public-ip:", "net.http.private-ip:")
NET_HTTP_IP_PREFIXES = ("net.http.public-ip:", "net.http.private-ip:")


def resolve_risk(permission_id: str) -> Risk | None:
    """The risk level for `permission_id`, or `None` if it is not a catalog member at all.

    Checks the static table first, then the parameterized families.
    `chat.send:<platform>` is `normal` (Sec1); `moderation.<platform>` is
    `dangerous`. Outbound HTTP is three separate families (2026-09-28):
    `net.http.fqdn:<host>` is `normal` (the preferred form); `net.http.
    public-ip:<ip>` and `net.http.private-ip:<ip|cidr>` are both
    `dangerous` -- a bare IP with no FQDN is inherently higher-risk
    (opaque, cache-poisoning/rebinding-prone, harder to audit) whether or
    not the address itself is externally routable.
    """
    if permission_id in STATIC_PERMISSIONS:
        return STATIC_PERMISSIONS[permission_id]

    match = _NET_HTTP_FQDN_RE.match(permission_id)
    if match and is_valid_fqdn(match.group("host")):
        return "normal"

    match = _NET_HTTP_PUBLIC_IP_RE.match(permission_id)
    if match and is_valid_public_ip(match.group("ip")):
        return "dangerous"

    match = _NET_HTTP_PRIVATE_IP_RE.match(permission_id)
    if match and is_valid_private_ip_or_cidr(match.group("value")):
        return "dangerous"

    match = _CHAT_SEND_RE.match(permission_id)
    if match and match.group("platform") in RELAY_PROVIDERS:
        return "normal"

    # `chat.delete:<platform>` (destructive moderation-class) and
    # `dm.send:<platform>` (reaches a user outside any public channel,
    # PII-adjacent) are both `dangerous` -- mirrors the Rust catalog in
    # `core/bundle_capability_gate/src/permission.rs` (provider-framework
    # Step 0, issue #719). Keep the two in sync.
    for pattern in (_CHAT_DELETE_RE, _DM_SEND_RE):
        match = pattern.match(permission_id)
        if match and match.group("platform") in RELAY_PROVIDERS:
            return "dangerous"

    match = _MODERATION_RE.match(permission_id)
    if match and match.group("platform") in RELAY_PROVIDERS:
        return "dangerous"

    return None


def permission_family(permission_id: str) -> str:
    """The instance-policy family key for `permission_id` -- a *type*, never one instance.

    Static ids are their own family (`storage.objects`); every
    parameterized family (`net.http.fqdn:<host>`, `net.http.public-ip:
    <ip>`, `net.http.private-ip:<ip|cidr>`, `chat.send:<platform>`,
    `moderation.<platform>`) collapses to its bare family prefix -- the
    instance policy layer (2026-09-28 decision) allows/denies a
    permission *type* instance-wide, never a single parameterized value.
    """
    for prefix in NET_HTTP_PREFIXES:
        if permission_id.startswith(prefix):
            return prefix.rstrip(":")
    if _CHAT_SEND_RE.match(permission_id):
        return "chat.send"
    if _CHAT_DELETE_RE.match(permission_id):
        return "chat.delete"
    if _DM_SEND_RE.match(permission_id):
        return "dm.send"
    if _MODERATION_RE.match(permission_id):
        return "moderation"
    return permission_id


def is_valid_fqdn(host: str) -> bool:
    """`True` iff `host` is a valid public hostname for `net.http.fqdn` -- no wildcard, no IP.

    Wildcards are rejected here (unlike the general egress-host grammar
    used elsewhere) because `net.http.fqdn` names exactly one allowlisted
    egress host per grant, not a pattern; an IP literal must instead use
    the dedicated `net.http.public-ip`/`net.http.private-ip` family so its
    risk is assessed as an IP, not smuggled in as a "hostname".
    """
    if not is_valid_egress_host(host, allow_wildcard=False):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        # Expected: a valid hostname never parses as an IP literal -- this
        # is the normal, non-error path for every genuine FQDN.
        logger.debug("is_valid_fqdn: %r is not an IP literal (%s), treating as hostname", host, exc)
        return True
    return False  # parses as a bare IP -- not a hostname


def is_valid_public_ip(value: str) -> bool:
    """`True` iff `value` is a single, globally-routable IP address (never a CIDR).

    Rejects loopback, link-local/metadata, and this cluster's own CIDRs
    even though `ip.is_global` would already exclude most of those --
    the explicit deny-list check also catches an operator-configured
    cluster CIDR that happens to use globally-routable addresses (some
    cloud CNIs assign public IPs to pod/node ranges).
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        # Expected: a caller-supplied net.http.public-ip value that isn't a
        # valid IP literal is routine rejection, not a system fault.
        logger.debug("is_valid_public_ip: %r does not parse as an IP (%s)", value, exc)
        return False
    single = ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}")
    if _overlaps_always_denied(single):
        return False
    return ip.is_global


def is_valid_private_ip_or_cidr(value: str) -> bool:
    """`True` iff `value` is a private IP or CIDR, never coarser than `_MAX_PRIVATE_PREFIX_V4/6`.

    Rejects loopback, link-local/metadata, and this cluster's own
    pod/service/node CIDRs unconditionally -- Sec("always denied"): a
    bundle can never grant itself egress to the platform's own
    infrastructure via this family, no matter how the request is scoped.
    """
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        # Expected: a caller-supplied net.http.private-ip value that isn't
        # a valid IP/CIDR is routine rejection, not a system fault.
        logger.debug(
            "is_valid_private_ip_or_cidr: %r does not parse as an IP/CIDR (%s)", value, exc
        )
        return False
    if _overlaps_always_denied(network):
        return False
    max_prefix = _MAX_PRIVATE_PREFIX_V4 if network.version == 4 else _MAX_PRIVATE_PREFIX_V6
    if network.prefixlen < max_prefix:
        return False
    return network.is_private


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
