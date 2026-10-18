#!/usr/bin/env python3
"""Idempotent Bar Citizen demo pre-link seed (role sync + event sync).

Pre-links one demo Discord identity + community membership, a Discord-role ->
community-role binding, and an enabled guild pairing on the alpha `waddlebot`
community so the Monday demo needs no live account-linking step. Every write is an
upsert; re-running is a safe rehearsal reset (the demo member's role returns to the
baseline unless it is `community-owner`).

Config (env, never CLI args; no secrets besides DATABASE_URL):
  DATABASE_URL            required, libpq DSN for the hub database
  DEMO_COMMUNITY_ID       default 1
  DEMO_GUILD_ID           default 474965105759748096 (Bar Citizen Discord guild)
  DEMO_DISCORD_USER_ID    Discord snowflake of the demo member (SWAP IN the real one)
  DEMO_DISCORD_ROLE_ID    Discord role snowflake to sync (SWAP IN BC's real role)
  DEMO_COMMUNITY_ROLE     Waddles role the Discord role grants (default `moderator`)
  DEMO_BASELINE_ROLE      role the member is reset to / demoted to (default `member`)
  DEMO_ROLE_NAME_PREFIX   pairing role-name prefix (default `[BC]`)
  DEMO_DIRECTION          pairing direction (default `discord_to_twitch`)

Logging is PII-free: only community_id, op names and counts are ever emitted.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("seed_bc_demo")

#: Roles `community_role_sync_bindings.community_role` CHECK accepts (migration 0036);
#: `community-owner` is deliberately never assignable by sync.
VALID_COMMUNITY_ROLES = ("community-admin", "moderator", "vip", "member")
VALID_DIRECTIONS = ("discord_to_twitch", "twitch_to_discord", "bidirectional")
DISCORD = "discord"

#: table -> columns this script reads/writes; verified against information_schema.
REQUIRED_SCHEMA: Mapping[str, tuple[str, ...]] = {
    "hub_users": ("id", "username", "display_name", "is_active", "uuid"),
    "hub_user_identities": ("hub_user_id", "platform", "platform_user_id", "is_primary"),
    "communities": ("id",),
    "community_roles": ("id", "community_id", "name"),
    "community_members": (
        "community_id", "user_id", "platform", "platform_user_id", "role",
        "community_role_id", "claims_cache", "is_active", "left_at", "updated_at",
    ),
    "guild_tenant_pairings": (
        "id", "community_id", "discord_guild_id", "direction", "sync_enabled",
        "event_sync_enabled", "role_name_prefix", "updated_at",
    ),
    "community_role_sync_bindings": (
        "pairing_id", "sync_scope", "subscriber_tier", "discord_role_id",
        "community_role", "updated_at",
    ),
}


class SeedError(RuntimeError):
    """Raised for any schema or config problem; the CLI turns it into a non-zero exit."""


@dataclass(slots=True, frozen=True)
class DemoConfig:
    """Resolved, validated seed parameters (immutable)."""

    community_id: int
    guild_id: str
    discord_user_id: str
    discord_role_id: str
    community_role: str
    baseline_role: str
    role_name_prefix: str
    direction: str


@dataclass(slots=True, frozen=True)
class SeedResult:
    """Row ids written by one seed run (surrogate ids only, no PII)."""

    hub_user_id: int
    pairing_id: int
    binding_count: int


def load_config(env: Mapping[str, str]) -> DemoConfig:
    """Build a `DemoConfig` from `env`, failing loud on any invalid value."""
    cfg = DemoConfig(
        community_id=_int(env.get("DEMO_COMMUNITY_ID", "1"), "DEMO_COMMUNITY_ID"),
        guild_id=_snowflake(env.get("DEMO_GUILD_ID", "474965105759748096"), "DEMO_GUILD_ID"),
        discord_user_id=_snowflake(
            env.get("DEMO_DISCORD_USER_ID", "900000000000000001"), "DEMO_DISCORD_USER_ID"
        ),
        discord_role_id=_snowflake(
            env.get("DEMO_DISCORD_ROLE_ID", "900000000000000002"), "DEMO_DISCORD_ROLE_ID"
        ),
        community_role=env.get("DEMO_COMMUNITY_ROLE", "moderator"),
        baseline_role=env.get("DEMO_BASELINE_ROLE", "member"),
        role_name_prefix=env.get("DEMO_ROLE_NAME_PREFIX", "[BC]"),
        direction=env.get("DEMO_DIRECTION", "discord_to_twitch"),
    )
    if cfg.community_role not in VALID_COMMUNITY_ROLES:
        raise SeedError(f"DEMO_COMMUNITY_ROLE must be one of {VALID_COMMUNITY_ROLES}")
    if cfg.baseline_role not in (*VALID_COMMUNITY_ROLES, "communicator", "speaker"):
        raise SeedError("DEMO_BASELINE_ROLE is not a known community role")
    if cfg.baseline_role == cfg.community_role:
        raise SeedError("DEMO_BASELINE_ROLE and DEMO_COMMUNITY_ROLE must differ (no visible change)")
    if cfg.direction not in VALID_DIRECTIONS:
        raise SeedError(f"DEMO_DIRECTION must be one of {VALID_DIRECTIONS}")
    if not cfg.role_name_prefix or len(cfg.role_name_prefix) > 50:
        raise SeedError("DEMO_ROLE_NAME_PREFIX must be 1-50 chars")
    return cfg


def _int(raw: str, name: str) -> int:
    """Parse a positive int env value or raise `SeedError` naming the variable."""
    try:
        value = int(raw)
    except ValueError as exc:
        raise SeedError(f"{name} must be an integer") from exc
    if value <= 0:
        raise SeedError(f"{name} must be positive")
    return value


def _snowflake(raw: str, name: str) -> str:
    """Validate a Discord snowflake (all digits, 15-25 chars) and return it unchanged."""
    if not (raw.isascii() and raw.isdigit() and 15 <= len(raw) <= 25):
        raise SeedError(f"{name} must be a Discord snowflake (15-25 digits)")
    return raw


def verify_schema(cur: Any) -> None:
    """Fail loud if any required table/column is missing (names tables only, no data)."""
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
        (list(REQUIRED_SCHEMA),),
    )
    present: dict[str, set[str]] = {}
    for table, column in cur.fetchall():
        present.setdefault(table, set()).add(column)
    missing = [
        f"{table}.{col}" if table in present else f"{table} (table)"
        for table, cols in REQUIRED_SCHEMA.items()
        for col in (cols if table in present else cols[:1])
        if table not in present or col not in present[table]
    ]
    if missing:
        raise SeedError(f"schema check failed, missing: {sorted(set(missing))}")
    logger.info("seed_bc_demo.schema_ok tables=%d", len(REQUIRED_SCHEMA))


def verify_community(cur: Any, cfg: DemoConfig) -> None:
    """Require the community and its baseline/target `community_roles` rows to exist."""
    cur.execute("SELECT 1 FROM communities WHERE id = %s", (cfg.community_id,))
    if cur.fetchone() is None:
        raise SeedError(f"community_id={cfg.community_id} does not exist")
    cur.execute(
        "SELECT name FROM community_roles WHERE community_id = %s AND name = ANY(%s)",
        (cfg.community_id, [cfg.baseline_role, cfg.community_role]),
    )
    found = {row[0] for row in cur.fetchall()}
    for role in (cfg.baseline_role, cfg.community_role):
        if role not in found:
            # Demote aborts without a baseline row; a missing target row only loses scopes.
            logger.warning(
                "seed_bc_demo.role_row_missing community_id=%s role=%s", cfg.community_id, role
            )
    if cfg.baseline_role not in found:
        raise SeedError(
            f"baseline role {cfg.baseline_role!r} has no community_roles row in "
            f"community_id={cfg.community_id}; demote would abort"
        )


def upsert_identity(cur: Any, cfg: DemoConfig) -> int:
    """Upsert the demo hub user and its Discord `hub_user_identities` link; return hub_user id."""
    cur.execute(
        "SELECT hub_user_id FROM hub_user_identities WHERE platform = %s AND platform_user_id = %s",
        (DISCORD, cfg.discord_user_id),
    )
    row = cur.fetchone()
    if row is not None:
        hub_user_id = int(row[0])
    else:
        cur.execute(
            "INSERT INTO hub_users (username, display_name, is_active) VALUES (%s, %s, TRUE) "
            "ON CONFLICT (username) DO UPDATE SET is_active = TRUE RETURNING id",
            (f"bc-demo-{cfg.discord_user_id}", "BC Demo Member"),
        )
        hub_user_id = int(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id, is_primary) "
            "VALUES (%s, %s, %s, TRUE) ON CONFLICT (platform, platform_user_id) DO NOTHING",
            (hub_user_id, DISCORD, cfg.discord_user_id),
        )
    logger.info("seed_bc_demo.identity_ok op=upsert_identity")
    return hub_user_id


def upsert_membership(cur: Any, cfg: DemoConfig, hub_user_id: int) -> None:
    """Upsert the demo member into the community at the baseline role (owner never touched)."""
    cur.execute(
        "INSERT INTO community_members (community_id, user_id, platform, platform_user_id, "
        "role, community_role_id, is_active) VALUES (%s, %s, %s, %s, %s, "
        "(SELECT id FROM community_roles WHERE community_id = %s AND name = %s), TRUE) "
        "ON CONFLICT (community_id, platform, platform_user_id) DO UPDATE SET "
        "user_id = EXCLUDED.user_id, role = EXCLUDED.role, "
        "community_role_id = EXCLUDED.community_role_id, claims_cache = NULL, "
        "is_active = TRUE, left_at = NULL, updated_at = NOW() "
        "WHERE community_members.role <> 'community-owner'",
        (
            cfg.community_id, str(hub_user_id), DISCORD, cfg.discord_user_id,
            cfg.baseline_role, cfg.community_id, cfg.baseline_role,
        ),
    )
    logger.info(
        "seed_bc_demo.membership_ok op=upsert_membership community_id=%s", cfg.community_id
    )


def upsert_pairing(cur: Any, cfg: DemoConfig) -> int:
    """Upsert the guild pairing with role sync + event sync enabled; return its id."""
    cur.execute(
        "INSERT INTO guild_tenant_pairings (community_id, discord_guild_id, direction, "
        "sync_enabled, event_sync_enabled, role_name_prefix) VALUES (%s, %s, %s, TRUE, TRUE, %s) "
        "ON CONFLICT (community_id, discord_guild_id) DO UPDATE SET "
        "direction = EXCLUDED.direction, sync_enabled = TRUE, event_sync_enabled = TRUE, "
        "role_name_prefix = EXCLUDED.role_name_prefix, updated_at = NOW() RETURNING id",
        (cfg.community_id, cfg.guild_id, cfg.direction, cfg.role_name_prefix),
    )
    pairing_id = int(cur.fetchone()[0])
    logger.info("seed_bc_demo.pairing_ok op=upsert_pairing community_id=%s", cfg.community_id)
    return pairing_id


def upsert_binding(cur: Any, cfg: DemoConfig, pairing_id: int) -> int:
    """Upsert the Discord role -> community role binding; return bindings now on the pairing."""
    cur.execute(
        "INSERT INTO community_role_sync_bindings (pairing_id, sync_scope, subscriber_tier, "
        "discord_role_id, community_role) VALUES (%s, 'community_role', NULL, %s, %s) "
        "ON CONFLICT (pairing_id, discord_role_id) WHERE sync_scope = 'community_role' "
        "DO UPDATE SET community_role = EXCLUDED.community_role, updated_at = NOW()",
        (pairing_id, cfg.discord_role_id, cfg.community_role),
    )
    cur.execute(
        "SELECT count(*) FROM community_role_sync_bindings WHERE pairing_id = %s", (pairing_id,)
    )
    count = int(cur.fetchone()[0])
    logger.info("seed_bc_demo.binding_ok op=upsert_binding bindings=%d", count)
    return count


def seed(conn: Any, cfg: DemoConfig) -> SeedResult:
    """Run every preflight check and upsert in one transaction (all-or-nothing)."""
    try:
        with conn.cursor() as cur:
            verify_schema(cur)
            verify_community(cur, cfg)
            hub_user_id = upsert_identity(cur, cfg)
            upsert_membership(cur, cfg, hub_user_id)
            pairing_id = upsert_pairing(cur, cfg)
            bindings = upsert_binding(cur, cfg, pairing_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return SeedResult(hub_user_id=hub_user_id, pairing_id=pairing_id, binding_count=bindings)


def main() -> int:
    """CLI entry: read env, connect, seed, print the pairing id the runbook needs."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dsn = os.environ.get("DATABASE_URL", "")
    try:
        if not dsn:
            raise SeedError("DATABASE_URL is required")
        cfg = load_config(os.environ)
        import psycopg2  # noqa: PLC0415 -- only the CLI path needs the driver

        conn = psycopg2.connect(dsn)
        try:
            result = seed(conn, cfg)
        finally:
            conn.close()
    except SeedError as exc:
        logger.error("seed_bc_demo.failed exc_type=SeedError message=%s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001 -- surfaced loudly with traceback, never swallowed
        logger.exception("seed_bc_demo.failed exc_type=%s", type(exc).__name__)
        return 1
    logger.info(
        "seed_bc_demo.done community_id=%s pairing_id=%s bindings=%d "
        "(set pipeline.roleSync.demotePairingIds=%s)",
        cfg.community_id, result.pairing_id, result.binding_count, result.pairing_id,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
