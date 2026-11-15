#!/usr/bin/env python3
"""Classify hub-api route handlers into DB routing classes and render the doc tables.

Input: JSON from `scripts/hubapi_db_inventory.py`. Classes (see `docs/hub-api-db-routing.md`):
READ-ONLY (replica), READ-AFTER-WRITE (primary), WRITE (primary), AUTH/SESSION/... (primary),
WATERMARK (primary). Handlers marked `@reads_from_replica` are MOVED; other eligible reads are
listed as `READ-ONLY (eligible, not moved)`. Usage: `hubapi_db_classify.py routes.json OUT.md`.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HUB_API = Path(__file__).resolve().parent.parent / "hub_api"

#: Modules whose reads are auth/session/OAuth-state/MFA/token/consent/idempotency/secret material.
AUTH_MODULES = {
    "auth": "login/session/OAuth-state/MFA: freshness + security",
    "sso": "SSO/OAuth state: single-use state must read primary",
    "passkey": "WebAuthn challenge/credential: security",
    "access_token": "API tokens: revocation must be immediate",
    "one_time_secrets": "single-use secret: consume-once semantics",
    "identity": "identity linking/verification codes: security + read-after-write",
    "profile": "caller's own profile: read-after-write after edit",
    "cookie_consent": "consent record: statutory, read-after-write",
    "data_privacy": "DSAR/erasure: statutory, freshness",
    "admin_data_privacy": "DSAR/erasure admin: statutory, freshness",
    "marketplace_webhooks": "webhook idempotency: freshness",
    "tenant_kms": "key material state: security",
    "tenant_discord_install": "OAuth install state: security",
    "tenant_twitch_install": "OAuth install state: security",
    "discord_bot_install": "OAuth install state: security",
    "provider_connections": "provider credentials/OAuth: security",
    "youtube_connections": "provider credentials/OAuth: security",
    "gameserver_connections": "connection credentials: security",
    "community_connections": "connection credentials: security",
    "media_byok": "BYOK key material: security",
    "bundle_permissions": "permission grants: authz freshness",
    "bundle_approvals": "approval gate: authz freshness",
    "bundle_approvals_bulk": "approval gate: authz freshness",
    "join_request": "membership gate: authz freshness",
    "community_join": "membership gate: authz freshness",
    "tenant": "tenant/admin membership: authz",
    "user_management": "user/role admin: authz + read-after-write",
    "superadmin": "global admin: authz + read-after-write",
    "admin": "admin console: authz + read-after-write",
    "platform_config": "platform/OAuth client config: security-relevant",
    "token_billing": "metered balance/ledger: money, freshness",
}
#: In-handler authz helpers: the same `dal` serves a tenant/membership check, so it stays primary.
AUTHZ_RE = re.compile(
    r"community_in_tenant|community_authz|require_community|is_community_admin|"
    r"community_member|ensure_|authorize|_admin_or|assert_",
)
READ_ONLY_REASON = {
    "marketplace_catalog": "catalog browse: seconds of staleness tolerated",
    "public": "anonymous public page: lag tolerated",
    "live_status": "overlay poll: lag delays a live flip by replication lag only",
}


#: Reads that are lag-tolerant in principle but were NOT migrated in this change: each shares one
#: `dal` with an authz helper or with edit-then-refetch UI, so moving it needs a per-route split.
ELIGIBLE_NOT_MOVED = {
    (
        "community_profile",
        "get_community_profile",
    ): "public profile; needs tenant/visibility helper split",
    (
        "public_music_queue",
        "get_public_queue",
    ): "public queue; reads Valkey playback + DB, review pending",
    (
        "analytics",
        "get_platform_overview",
    ): "aggregate dashboard; authz helper shares the dal",
    (
        "analytics",
        "get_platform_reputation",
    ): "aggregate dashboard; authz helper shares the dal",
    (
        "community_loyalty",
        "get_leaderboard",
    ): "leaderboard aggregate; authz helper shares the dal",
    ("community_loyalty", "get_stats"): "stats aggregate; authz helper shares the dal",
    ("overlay", "get_overlay_stats"): "stats aggregate; authz helper shares the dal",
    ("platform", "get_stats"): "platform stats dashboard; authz helper shares the dal",
    (
        "platform",
        "get_audit_log",
    ): "append-only audit listing; authz helper shares the dal",
    (
        "marketplace_vendor",
        "get_vendor_dashboard",
    ): "vendor dashboard; edit-then-refetch risk",
    ("marketplace_vendor", "get_sales_metrics"): "vendor analytics aggregate",
    (
        "marketplace_admin_review",
        "get_vendor_requests",
    ): "review queue: act-then-refetch risk",
    (
        "marketplace_admin_review",
        "get_submissions",
    ): "review queue: act-then-refetch risk",
}


def classify(row: dict[str, object], src_cache: dict[str, str]) -> tuple[str, str, str]:
    """Return `(class, target, reason)` for one inventory row."""
    mod = Path(str(row["file"])).stem
    methods = set(row["methods"])  # type: ignore[call-overload]
    seg = _segment(row, src_cache)
    if not row["uses_dal"]:
        return (
            "NO-DB",
            "n/a",
            "no DAL access in the handler (proxy / cache / flag / static)",
        )
    if methods - {"GET", "HEAD", "OPTIONS"} or row["writes"]:
        return "WRITE", "primary", "mutating route"
    if "reads_from_replica" in seg:
        return (
            "READ-ONLY",
            "replica (moved)",
            READ_ONLY_REASON.get(mod, "pure GET read"),
        )
    if mod == "distribution":
        return (
            "READ-ONLY",
            "replica (pre-existing)",
            "already routed to `read_dal` before this change",
        )
    if (mod, str(row["fn"])) in ELIGIBLE_NOT_MOVED:
        return (
            "READ-ONLY (eligible, not moved)",
            "primary",
            ELIGIBLE_NOT_MOVED[(mod, str(row["fn"]))],
        )
    if mod in AUTH_MODULES:
        return "AUTH/SESSION/SECURITY", "primary", AUTH_MODULES[mod]
    if AUTHZ_RE.search(seg):
        return (
            "AUTH/SESSION/SECURITY",
            "primary",
            "in-handler tenant/membership authz check",
        )
    if mod.startswith(
        (
            "community_",
            "bundle_",
            "marketplace_",
            "platform",
            "ai_routing",
            "stream",
            "music",
            "overlay",
            "support",
            "workflow",
            "workstream",
            "github_sync",
            "guild_pairing",
            "ingest",
            "event",
            "bot",
            "app_commands",
            "calls",
            "compliance",
            "analytics",
            "live_activity",
            "flags",
            "distribution",
        )
    ):
        return (
            "READ-AFTER-WRITE / UNREVIEWED",
            "primary",
            "authenticated console/dashboard GET: its UI edits then re-fetches the same rows",
        )
    return (
        "READ-AFTER-WRITE / UNREVIEWED",
        "primary",
        "unreviewed GET: kept primary until lag review",
    )


_SEG_RE_CACHE: dict[str, list[str]] = {}


def _segment(row: dict[str, object], cache: dict[str, str]) -> str:
    """Source text of the handler (decorators + body) for regex checks."""
    file = str(row["file"])
    if file not in cache:
        cache[file] = (HUB_API / file).read_text()
    lines = cache[file].split("\n")
    start = int(row["line"]) - 1
    # include up to 8 decorator lines above and the body until the next top-level def/decorator
    top = start
    while top > 0 and lines[top - 1].startswith("@"):
        top -= 1
    end = start + 1
    while end < len(lines) and not (
        lines[end].startswith(("@", "def ", "async def ", "class "))
    ):
        end += 1
    return "\n".join(lines[top:end])


def main() -> int:
    """Render the per-route table + counts to argv[2]."""
    rows = json.loads(Path(sys.argv[1]).read_text())
    if not rows:
        print("FAIL: zero rows", file=sys.stderr)
        return 1
    cache: dict[str, str] = {}
    out: list[tuple[str, str, str, str, str]] = []
    for r in rows:
        cls, target, why = classify(r, cache)
        out.append(
            (
                str(r["file"]),
                f"{'/'.join(r['methods'])} {r['path'] or '/'} ({r['fn']})",
                cls,
                target,
                why,
            )
        )
    counts = Counter(o[2] for o in out)
    moved = sum(1 for o in out if o[3] == "replica (moved)")
    by_mod: dict[str, list[tuple[str, str, str, str, str]]] = defaultdict(list)
    for o in out:
        by_mod[o[0]].append(o)
    lines = [
        f"<!-- generated by scripts/hubapi_db_classify.py: {len(out)} handlers -->",
        "",
    ]
    lines += ["| class | handlers | moved to replica |", "|---|---|---|"]
    for cls, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(
            f"| {cls} | {n} | {sum(1 for o in out if o[2] == cls and o[3] == 'replica (moved)')} |"
        )
    lines += [f"| **total examined** | **{len(out)}** | **{moved}** |", ""]
    lines += [
        "| module | route/function | class | target | reason |",
        "|---|---|---|---|---|",
    ]
    for mod in sorted(by_mod):
        # collapse identical (class,target,reason) within a module
        groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for _f, route, cls, target, why in by_mod[mod]:
            groups[(cls, target, why)].append(route)
        for (cls, target, why), routes in sorted(groups.items()):
            shown = "; ".join(routes[:3]) + (
                f" (+{len(routes) - 3} more)" if len(routes) > 3 else ""
            )
            lines.append(
                f"| `{mod.replace('blueprints/', '')}` | {len(routes)}: {shown} | {cls} | {target} | {why} |"
            )
    Path(sys.argv[2]).write_text("\n".join(lines) + "\n")
    print(dict(counts), "moved", moved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
