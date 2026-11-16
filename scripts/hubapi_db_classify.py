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
    "live_status": "overlay poll: lag only delays a live flip; community gate read stays primary",
    "platform": "scope-gated aggregate/append-only dashboard; no DB authz in handler",
    "analytics": "scope-gated aggregate over the bundle store; no DB authz in handler",
    "community_loyalty": "aggregate/shop read on replica; authz/existence gate + lazy config INSERT stay primary",
    "overlay": "aggregate stats on replica; `_require_admin` authz read stays primary",
    "marketplace_billing": "public pricing config; lag tolerated",
    "marketplace_vendor": "unauthenticated published-module listing",
    "support": "aggregate counts; scope + tenant checked before the read",
}


#: Per-route lag review: pure GETs that were examined and deliberately kept on the primary.
KEPT_REVIEWED = {
    (
        "community_profile",
        "get_community_profile",
    ): "visibility gate and data are one row; a stale row would leak a just-privatised community",
    (
        "public_music_queue",
        "get_public_queue",
    ): "joins live Redis playback with DB rows; lag can show an inconsistent now-playing",
    (
        "marketplace_vendor",
        "get_vendor_dashboard",
    ): "seller just registered/submitted lands here; money + pending counts need freshness",
    (
        "marketplace_vendor",
        "get_sales_metrics",
    ): "seller ownership lookup in the service shares the DAL (new vendor would 404 under lag)",
    (
        "marketplace_admin_review",
        "get_vendor_requests",
    ): "review queue: admin acts then refetches the same rows",
    (
        "marketplace_admin_review",
        "get_submissions",
    ): "review queue: admin acts then refetches the same rows",
    ("platform", "get_system_health"): "probes primary database liveness on purpose",
    ("platform", "list_users"): "admin edits a role then refetches the list",
    ("platform", "get_user"): "admin edits then refetches the row",
    ("platform", "list_communities"): "admin edits then refetches the list",
    ("platform", "get_community"): "admin edits then refetches the row",
    (
        "community_loyalty",
        "internal_get_balance",
    ): "currency balance: money-like, freshness",
}

#: Why each remaining non-auth GET module stays primary (edit-then-refetch or state-bearing).
MODULE_REASON = {
    "app_commands_internal": "reconciler reads desired state it just wrote; freshness",
    "community_announcements": "announcement/broadcast status tracks a just-sent write",
    "community_interaction": "admin role/permission config (authz-bearing) + edit-then-refetch",
    "community_inventory": "checkout/return flows refetch; inventory state is authz-bearing",
    "community_music_queue": "joins live Redis playback with DB rows",
    "eventsub_reconcile": "secret material",
    "github_sync": "connection state: connect then refetch",
    "guild_pairing": "pairing state: pair then refetch",
    "marketplace_billing": "admin edits discount codes then refetches",
    "marketplace_lifecycle": "admin installs/toggles then refetches the same listing",
    "marketplace_vendor": "seller ownership lookup in the service + register/submit then view",
    "support": "tickets: mutate then refetch; per-user ticket state",
    "tenant_reputation": "tenant config: edit then refetch",
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
    if (mod, str(row["fn"])) in KEPT_REVIEWED:
        return (
            "READ-ONLY, kept primary (reviewed)",
            "primary",
            KEPT_REVIEWED[(mod, str(row["fn"]))],
        )
    if mod in AUTH_MODULES:
        return "AUTH/SESSION/SECURITY", "primary", AUTH_MODULES[mod]
    if AUTHZ_RE.search(seg):
        return (
            "AUTH/SESSION/SECURITY",
            "primary",
            "in-handler tenant/membership authz check",
        )
    return (
        "READ-AFTER-WRITE (reviewed)",
        "primary",
        MODULE_REASON.get(mod, "edit-then-refetch or state-bearing GET (reviewed)"),
    )


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
