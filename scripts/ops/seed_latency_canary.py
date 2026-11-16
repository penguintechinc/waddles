#!/usr/bin/env python3
r"""Idempotent alpha-only seeder for the command-latency canary community (testing guild).

Creates (or finds) the dedicated canary community through the hub-api CONTROL PLANE -- the same
superadmin/activation REST routes the web UI uses, never direct SQL -- binds it to the dedicated
testing Discord guild, activates the Rust `!ping` bundle, and switches ALL accrual off for that
community. Reputation and economy are SERVICE bundles (activated tenant-wide, so a per-community
deactivation cannot stop them): the supported per-community off switch is the bundle community
config `accrual_enabled: false` (bundle-admin gateway ``PUT /api/v1/apps/community/<id>/bundles/
<app_id>/config``), honoured by the reputation-accrual app and the reputation and economy service
bundles (versions >= 1.0.1 / 1.1.2 / 1.0.2). The seeder sets it on all three and verifies it by reading
the saved config back. Game/loyalty APP bundles that mint are additionally deactivated here if the
community has them activated. Prints the numeric community id the owner puts in
``latencyCanary.communityId``.

    kubectl --context dal2alpha -n waddlebot port-forward svc/waddlebot-hub-api-v3 18080:8080 &
    export WADDLES_ADMIN_TOKEN=...            # default-tenant admin JWT; read from env, never an argument
    scripts/ops/seed_latency_canary.py --hub-api-url http://127.0.0.1:18080 \
        --guild-id <testing guild snowflake> --confirm-alpha

Safety: refuses any hub-api URL that is not loopback or an explicit ``alpha`` host, requires
``--confirm-alpha``, never prints the token, and reads the token only from the environment.
Re-running is a no-op (everything is looked up first). ``--dry-run`` performs GETs only.

Exit status: 0 = community ready and verified; 1 = a step failed or verification found a denied
bundle still active or `accrual_enabled` could not be confirmed false; 2 = refused (bad arguments / non-alpha target / token missing).
Zero activations or zero accrual configs examined after seeding is a failure, never a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 2

PING_APP_ID = "waddles.core.example.ping"
COMMUNITY_NAME = "latency-canary"
# Reputation / economy accrual: the reputation-accrual APP plus the two SERVICE bundles. Their
# per-community off switch is the bundle community config key `accrual_enabled`.
ACCRUAL_BUNDLES = (
    "waddles.core.app.reputation-accrual",
    "waddles.core.service.reputation",
    "waddles.core.service.economy",
)
ACCRUAL_KEY = "accrual_enabled"
# Substrings of APP bundle ids that mint/accrue and must NOT be active in the canary community.
# Service bundles (`.service.`) are never touched here: they are tenant-wide and are switched off
# through ACCRUAL_BUNDLES' community config instead.
DENY_PATTERNS = (
    "reputation",
    "waddlebuck",
    "loyalty",
    "raffle",
    "giveaway",
    "dungeon",
    "lottery",
    "jackpot",
    "blackjack",
)
TOKEN_ENV = "WADDLES_ADMIN_TOKEN"

Http = Callable[[str, str, dict[str, object] | None], tuple[int, dict[str, object]]]


class Refused(Exception):
    """The invocation is not allowed (wrong target, missing confirmation or token)."""


@dataclass(frozen=True)
class Result:
    """What the seeder did, for the summary line and tests."""

    community_id: int
    created: bool
    ping_activated: bool
    deactivated: tuple[str, ...]
    examined: int
    accrual_off: tuple[str, ...] = ()
    accrual_changed: tuple[str, ...] = ()


def check_target(url: str) -> None:
    """Allows only loopback (a port-forward) or a hostname containing the label `alpha`."""
    host = (urlparse(url).hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return
    if "alpha" in host.split(".")[0] or "-alpha" in host or host.startswith("alpha"):
        return
    raise Refused(f"refusing hub-api host {host!r}: the canary seeder is alpha-only")


def make_http(base: str, token: str) -> Http:
    """A tiny JSON client; the bearer token never appears in any message."""

    def call(method: str, path: str, body: dict[str, object] | None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            base.rstrip("/") + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"error": "non-json response"}
        return status, parsed if isinstance(parsed, dict) else {"value": parsed}

    return call


def _ok(status: int, what: str) -> None:
    if not 200 <= status < 300:
        raise RuntimeError(f"{what} failed with HTTP {status}")


def find_community(http: Http, guild_id: str) -> int | None:
    """The existing discord community bound to this guild (any name), if any."""
    status, body = http(
        "GET", "/api/v1/superadmin/communities?platform=discord&limit=100&search=", None
    )
    _ok(status, "list communities")
    rows = body.get("communities")
    if not isinstance(rows, list):
        raise RuntimeError("list communities: unexpected response shape")  # noqa: TRY004
    for row in rows:
        if isinstance(row, dict) and str(row.get("platformServerId")) == guild_id:
            cid = row.get("id")
            if isinstance(cid, int):
                return cid
    return None


def create_community(http: Http, guild_id: str) -> int:
    """Creates the dedicated canary community bound to the testing guild (private, non-public)."""
    status, body = http(
        "POST",
        "/api/v1/superadmin/communities",
        {
            "name": COMMUNITY_NAME,
            "displayName": "Latency Canary (testing guild)",
            "description": "Synthetic !ping latency canary. Alpha only. Do not use.",
            "platform": "discord",
            "platformServerId": guild_id,
            "isPublic": False,
        },
    )
    _ok(status, "create community")
    community = body.get("community")
    cid = community.get("id") if isinstance(community, dict) else None
    if not isinstance(cid, int):
        raise RuntimeError("create community: no id in response")  # noqa: TRY004
    return cid


def active_app_ids(http: Http, community_id: int) -> list[str]:
    """App ids currently activated for the community."""
    status, body = http(
        "GET", f"/api/v1/apps/community/{community_id}/activation", None
    )
    _ok(status, "list activations")
    bundles = body.get("bundles")
    if not isinstance(bundles, list):
        raise RuntimeError("list activations: unexpected response shape")  # noqa: TRY004
    return [str(b["appId"]) for b in bundles if isinstance(b, dict) and "appId" in b]


def is_denied_app(app_id: str, deny: Sequence[str]) -> bool:
    """Whether an activated APP bundle should be deactivated (service bundles never are)."""
    low = app_id.lower()
    return ".service." not in low and any(p in low for p in deny)


def _bundle_config_path(community_id: int, app_id: str) -> str:
    return f"/api/v1/apps/community/{community_id}/bundles/{app_id}/config"


def read_accrual_config(
    http: Http, community_id: int, app_id: str
) -> tuple[int, dict[str, object]] | None:
    """The saved (version, config) of one accrual bundle, or None when it is not active here.

    A bundle without a `community_config_schema` (an old, pre-switch version) is a hard failure:
    the off switch cannot exist until the new bundle versions are deployed.
    """
    status, body = http("GET", _bundle_config_path(community_id, app_id), None)
    text = json.dumps(body)
    if status == 404 and "bundle_not_active" in text:
        return None
    if status == 404 and "bundle_has_no_config" in text:
        raise RuntimeError(
            f"{app_id} declares no community_config_schema: deploy the bundle versions that add "
            f"`{ACCRUAL_KEY}` (reputation-accrual >= 1.0.1, reputation >= 1.1.2, economy >= 1.0.2) first"
        )
    _ok(status, f"read config of {app_id}")
    version = body.get("version")
    config = body.get("config")
    if not isinstance(version, int) or not isinstance(config, dict):
        raise RuntimeError(f"read config of {app_id}: unexpected response shape")  # noqa: TRY004
    return version, config


def disable_accrual(
    http: Http, community_id: int, *, dry_run: bool
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Sets `accrual_enabled: false` on every accrual bundle active here, then reads each back.

    Returns (confirmed off, changed by this run). A bundle that is not active for the community
    accrues nothing and is skipped; zero bundles examined is a failure.
    """
    off: list[str] = []
    changed: list[str] = []
    examined = 0
    for app_id in ACCRUAL_BUNDLES:
        cur = read_accrual_config(http, community_id, app_id)
        if cur is None:
            continue
        examined += 1
        version, config = cur
        if config.get(ACCRUAL_KEY) is not False:
            if dry_run:
                raise RuntimeError(
                    f"dry-run: {app_id} accrual is not disabled (would set {ACCRUAL_KEY}=false)"
                )
            status, _ = http(
                "PUT",
                _bundle_config_path(community_id, app_id),
                {"config": {**config, ACCRUAL_KEY: False}, "expectedVersion": version},
            )
            _ok(status, f"disable accrual on {app_id}")
            changed.append(app_id)
            after = read_accrual_config(http, community_id, app_id)
            config = after[1] if after else {}
        if config.get(ACCRUAL_KEY) is not False:
            raise RuntimeError(
                f"verification: {app_id} {ACCRUAL_KEY} is not false after seeding"
            )
        off.append(app_id)
    if examined == 0:
        raise RuntimeError(
            "verification examined 0 accrual bundles -- zero examined is a failure "
            "(none of the reputation/economy bundles is active for this community's tenant)"
        )
    return tuple(off), tuple(changed)


def seed(
    http: Http, guild_id: str, *, dry_run: bool, deny: Sequence[str] = DENY_PATTERNS
) -> Result:
    """Runs every step idempotently and verifies the end state."""
    cid = find_community(http, guild_id)
    created = False
    if cid is None:
        if dry_run:
            raise RuntimeError(
                "dry-run: the canary community does not exist yet (would create it)"
            )
        cid = create_community(http, guild_id)
        created = True
    active = active_app_ids(http, cid)
    ping_activated = False
    if PING_APP_ID not in active:
        if dry_run:
            raise RuntimeError(
                f"dry-run: {PING_APP_ID} is not activated (would activate it)"
            )
        status, body = http(
            "POST",
            f"/api/v1/apps/community/{cid}/activation",
            {"appId": PING_APP_ID, "acknowledgedAccessGrants": []},
        )
        if status == 422:
            raise RuntimeError(
                f"{PING_APP_ID} needs access-grant acknowledgement ({body.get('error', 'see hub-api')}); "
                "ping must not require dangerous grants -- investigate before seeding"
            )
        _ok(status, f"activate {PING_APP_ID}")
        ping_activated = True
    deactivated: list[str] = []
    for app_id in active_app_ids(http, cid):
        if is_denied_app(app_id, deny):
            if dry_run:
                raise RuntimeError(f"dry-run: {app_id} is active (would deactivate it)")
            status, _ = http(
                "DELETE", f"/api/v1/apps/community/{cid}/activation/{app_id}", None
            )
            _ok(status, f"deactivate {app_id}")
            deactivated.append(app_id)
    final = active_app_ids(http, cid)
    if not final:
        raise RuntimeError(
            "verification examined 0 activations -- zero examined is a failure"
        )
    if PING_APP_ID not in final:
        raise RuntimeError(f"verification: {PING_APP_ID} is not active after seeding")
    still = [a for a in final if is_denied_app(a, deny)]
    if still:
        raise RuntimeError(f"verification: denied bundles still active: {still}")
    off, changed = disable_accrual(http, cid, dry_run=dry_run)
    return Result(
        cid, created, ping_activated, tuple(deactivated), len(final), off, changed
    )


def main(
    argv: Sequence[str] | None = None,
    http_factory: Callable[[str, str], Http] = make_http,
) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--hub-api-url", required=True, help="loopback port-forward or an alpha host"
    )
    ap.add_argument(
        "--guild-id", required=True, help="the dedicated testing guild snowflake"
    )
    ap.add_argument(
        "--confirm-alpha", action="store_true", help="required: acknowledges alpha-only"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="GETs only; fails if anything would change",
    )
    args = ap.parse_args(argv)
    try:
        if not args.confirm_alpha:
            raise Refused("--confirm-alpha is required")
        if not re.fullmatch(r"[0-9]{1,20}", args.guild_id):
            raise Refused("--guild-id must be a numeric Discord snowflake")
        check_target(args.hub_api_url)
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            raise Refused(f"{TOKEN_ENV} is not set (read from the environment only)")
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        res = seed(
            http_factory(args.hub_api_url, token), args.guild_id, dry_run=args.dry_run
        )
    except (RuntimeError, urllib.error.URLError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_FAIL
    print(
        f"OK community_id={res.community_id} created={res.created} ping_activated={res.ping_activated} "
        f"deactivated={list(res.deactivated)} activations_examined={res.examined} "
        f"accrual_off={len(res.accrual_off)}/{len(ACCRUAL_BUNDLES)} accrual_changed={list(res.accrual_changed)}"
    )
    print(f"latencyCanary.communityId: {res.community_id}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
