"""Platform secret store: the `platform_vault` schema (secrets, events, root marker, public-key view).

The platform's own generated secrets (JWT / bundle / precompile signing keys, HMAC keys, DEKs,
service passwords) are persisted sealed in Postgres. Design + owner decisions: the 2026-11-14
platform secret store design; security review blockers B1/B2 and findings H4/L1 shape this file.

Why a dedicated schema (B1). `public` is the schema every `public_dml` role (hub-api and about 13
legacy pods) receives DML on automatically, so a table there would be fail-open. `platform_vault` is
reachable only through explicit grants, and the grants are the whole access model:

  =========================  ==========================================================
  role                       privileges
  =========================  ==========================================================
  waddles_hub_api            USAGE on the schema; `secrets` SELECT/INSERT/UPDATE;
                             `secret_events` SELECT/INSERT; `root_marker`
                             SELECT/INSERT/UPDATE; identity sequences USAGE/SELECT.
                             NEVER DELETE or TRUNCATE on anything here.
  svc_ingest/process/action  USAGE on the schema; SELECT on the VIEW `public_keys` only.
  /streaming/presentation    Never a table, never `sealed_value`.
  everyone else              nothing (the migration revokes from every non-superuser role,
                             including NOLOGIN groups and default-privilege grantees).
  =========================  ==========================================================

The tables and the schema are also listed in `legacy_deny_tables` of
`config/postgres/service-roles.yaml` (defense in depth: a future move into `public` still denies the
legacy roles), and the real-Postgres grant-matrix test fails if any role strays from the table above.

Tables (BIGINT identity PK for joins; `public_id` UUID is the only external reference and is bound
into the ciphertext AAD; every status / type / kind column is an int code, never a string or boolean):

  secrets.state               1 pending, 2 active, 3 verify-only, 4 retired, 5 destroyed
  secrets.algorithm           1 Ed25519, 2 ES256 (P-256), 3 random 256-bit, 4 password (32 bytes, base64url)
  secrets.external_sync_state 0 not applicable, 1 pending, 2 applied, 3 failed
  secrets.reason              1 generated, 2 imported, 3 restored from escrow, 4 rotated, 5 retired,
                              6 destroyed, 7 rewrapped
  secrets.secret_type         positive code from the hub-api registry (`services/platform_secrets`, T3)
  secrets.seal_version        envelope layout version (1) -- header byte 4 of `sealed_value`
  secret_events.event_type    1 generated, 2 imported, 3 restored, 4 activated, 5 demoted to
                              verify-only, 6 retired, 7 destroyed, 8 rotation requested, 9 open
                              failed, 10 drift detected, 11 escrowed, 12 external apply done,
                              13 rewrapped, 14 boot refused (wiped store or changed root)
  secret_events.outcome       1 success, 2 failure

`sealed_value` is the vault envelope `"WPS" || seal_version(1) || kek_id(8) || iv(12) || ct+tag`
(`provider_connections/vault.py::seal_platform_secret`); a CHECK pins the prefix, the version byte and
that the stored `kek_id` equals the header's. A destroyed secret (state 5) is crypto-shredded:
`sealed_value` NULL, enforced both ways. One ACTIVE row per (type, scope) via a partial unique index;
NULL scope (global) is distinct from scope 0 (the default tenant) and `NULLS NOT DISTINCT` keeps
(type, scope, version) unique for global secrets too (Postgres 15+; CI and prod run 17).

Wipe / rollback guards (B2, H4), enforced by triggers because the roles that matter hold no DELETE:
  - DELETE and TRUNCATE on `secrets`, `secret_events` and `root_marker` raise for every role,
    the owner included (downgrade drops the schema, which does not fire row triggers).
  - `secrets` state only moves forward (1 -> 2 -> 3 -> 4 -> 5), a destroyed row is immutable, the
    identity / public material / created_at columns never change (the keyed fingerprint only
    together with a re-wrap, i.e. a kek_id change), `activated_at` is set-once, and activating (or inserting active) a version lower than an already activated one for
    the same (type, scope) is refused -- a retired or compromised key can never be revived.
  - `secret_events` is append-only (UPDATE and DELETE raise) and hash-chained: `chain_hash` is a keyed
    HMAC the application computes (`platform_event_hash`), the database only forces a single linear
    chain (unique `prev_chain_hash`, one genesis row).
  - `root_marker` is a one-row table (kek id + keyed MAC under the platform KEK). "First run" is only
    ever "no marker AND no secrets"; a marker with an empty `secrets` table means the store was wiped
    and hub-api refuses to start (T3). Downgrading while sealed secrets exist is refused here unless
    `WADDLES_ALLOW_PLATFORM_VAULT_DROP=1` -- a replay must never silently empty the store.

`public_keys` is a plain `security_barrier` view owned by the migration owner (so the data-plane
roles need no table privilege): only active (2) and verify-only (3) rows that carry public material,
and only public columns -- no `sealed_value`, `fingerprint` or `kek_id`.

Revision ids are <= 32 chars. `down_revision` is 0107: the `0108_revoke_cfg_communities` follow-up is
on an unmerged branch; whichever lands second must re-point its `down_revision` to keep one head.

Revision ID: 0109_platform_vault
Revises: 0107_bundle_config_db_reads
Create Date: 2026-11-14
"""

from __future__ import annotations

import os

from alembic import op

revision = "0109_platform_vault"
down_revision = "0107_bundle_config_db_reads"
branch_labels = None
depends_on = None

SCHEMA = "platform_vault"
HUB_API_ROLE = "waddles_hub_api"
#: Data-plane roles that may verify signatures against `public_keys` (SELECT on the view only).
PUBLIC_KEY_READERS = (
    "waddles_svc_ingest",
    "waddles_svc_process",
    "waddles_svc_action",
    "waddles_svc_streaming",
    "waddles_svc_presentation",
)
#: Exact privileges `waddles_hub_api` holds, relation -> privileges (no DELETE/TRUNCATE anywhere).
HUB_API_GRANTS: dict[str, tuple[str, ...]] = {
    "secrets": ("SELECT", "INSERT", "UPDATE"),
    "secret_events": ("SELECT", "INSERT"),
    "root_marker": ("SELECT", "INSERT", "UPDATE"),
}
VIEW = "public_keys"
ALLOW_DROP_ENV = "WADDLES_ALLOW_PLATFORM_VAULT_DROP"
#: Shortest valid envelope: magic(3) + version(1) + kek_id(8) + iv(12) + tag(16) + 1 plaintext byte.
ENVELOPE_MIN_BYTES = 41

_SECRETS = f"""
CREATE TABLE platform_vault.secrets (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id           UUID NOT NULL DEFAULT gen_random_uuid(),
    secret_type         SMALLINT NOT NULL,
    scope_id            BIGINT,
    version             INTEGER NOT NULL,
    state               SMALLINT NOT NULL DEFAULT 1,
    algorithm           SMALLINT NOT NULL,
    sealed_value        BYTEA,
    kek_id              TEXT NOT NULL,
    seal_version        SMALLINT NOT NULL DEFAULT 1,
    kid                 TEXT,
    public_material     BYTEA,
    fingerprint         BYTEA NOT NULL,
    external_sync_state SMALLINT NOT NULL DEFAULT 0,
    external_synced_at  TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at        TIMESTAMPTZ,
    rotated_at          TIMESTAMPTZ,
    retired_at          TIMESTAMPTZ,
    actor_uuid          UUID,
    reason              SMALLINT NOT NULL,
    CONSTRAINT secrets_public_id_key UNIQUE (public_id),
    CONSTRAINT secrets_identity_key UNIQUE NULLS NOT DISTINCT (secret_type, scope_id, version),
    CONSTRAINT secrets_type_ck CHECK (secret_type >= 1),
    CONSTRAINT secrets_scope_ck CHECK (scope_id IS NULL OR scope_id >= 0),
    CONSTRAINT secrets_version_ck CHECK (version >= 1),
    CONSTRAINT secrets_state_ck CHECK (state BETWEEN 1 AND 5),
    CONSTRAINT secrets_algorithm_ck CHECK (algorithm BETWEEN 1 AND 4),
    CONSTRAINT secrets_sync_state_ck CHECK (external_sync_state BETWEEN 0 AND 3),
    CONSTRAINT secrets_reason_ck CHECK (reason BETWEEN 1 AND 7),
    CONSTRAINT secrets_seal_version_ck CHECK (seal_version >= 1),
    CONSTRAINT secrets_kek_id_ck CHECK (kek_id ~ '^[0-9a-f]{{16}}$'),
    CONSTRAINT secrets_fingerprint_ck CHECK (octet_length(fingerprint) = 32),
    CONSTRAINT secrets_kid_ck CHECK (kid IS NULL OR kid ~ '^[A-Za-z0-9_-]{{1,64}}$'),
    CONSTRAINT secrets_public_material_ck CHECK (
        (algorithm IN (1, 2) AND kid IS NOT NULL AND public_material IS NOT NULL
            AND octet_length(public_material) BETWEEN 32 AND 512)
        OR (algorithm IN (3, 4) AND kid IS NULL AND public_material IS NULL)
    ),
    CONSTRAINT secrets_envelope_ck CHECK (
        (state = 5 AND sealed_value IS NULL)
        OR (
            state <> 5
            AND sealed_value IS NOT NULL
            AND octet_length(sealed_value) >= {ENVELOPE_MIN_BYTES}
            AND substring(sealed_value FROM 1 FOR 3) = decode('575053', 'hex')
            AND get_byte(sealed_value, 3) = seal_version
            AND encode(substring(sealed_value FROM 5 FOR 8), 'hex') = kek_id
        )
    ),
    CONSTRAINT secrets_state_timestamps_ck CHECK (
        (state NOT IN (2, 3, 4) OR activated_at IS NOT NULL)
        AND (state <> 4 OR retired_at IS NOT NULL)
    )
)
"""

_EVENTS = f"""
CREATE TABLE platform_vault.secret_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id       UUID NOT NULL DEFAULT gen_random_uuid(),
    secret_id       BIGINT REFERENCES {SCHEMA}.secrets (id) ON DELETE RESTRICT,
    secret_type     SMALLINT NOT NULL,
    scope_id        BIGINT,
    version         INTEGER,
    event_type      SMALLINT NOT NULL,
    outcome         SMALLINT NOT NULL,
    reason          SMALLINT,
    actor_uuid      UUID,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    prev_chain_hash BYTEA,
    chain_hash      BYTEA NOT NULL,
    CONSTRAINT secret_events_public_id_key UNIQUE (public_id),
    CONSTRAINT secret_events_chain_hash_key UNIQUE (chain_hash),
    CONSTRAINT secret_events_type_ck CHECK (secret_type >= 1),
    CONSTRAINT secret_events_scope_ck CHECK (scope_id IS NULL OR scope_id >= 0),
    CONSTRAINT secret_events_version_ck CHECK (version IS NULL OR version >= 1),
    CONSTRAINT secret_events_event_type_ck CHECK (event_type BETWEEN 1 AND 14),
    CONSTRAINT secret_events_outcome_ck CHECK (outcome IN (1, 2)),
    CONSTRAINT secret_events_reason_ck CHECK (reason IS NULL OR reason BETWEEN 1 AND 7),
    CONSTRAINT secret_events_prev_hash_ck CHECK (
        prev_chain_hash IS NULL OR octet_length(prev_chain_hash) = 32
    ),
    CONSTRAINT secret_events_chain_hash_ck CHECK (octet_length(chain_hash) = 32)
)
"""

_ROOT_MARKER = """
CREATE TABLE platform_vault.root_marker (
    id           SMALLINT PRIMARY KEY DEFAULT 1,
    kek_id       TEXT NOT NULL,
    check_mac    BYTEA NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    reinit_count INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT root_marker_singleton_ck CHECK (id = 1),
    CONSTRAINT root_marker_kek_id_ck CHECK (kek_id ~ '^[0-9a-f]{16}$'),
    CONSTRAINT root_marker_mac_ck CHECK (octet_length(check_mac) = 32),
    CONSTRAINT root_marker_reinit_ck CHECK (reinit_count >= 0)
)
"""

_INDEXES = (
    # One ACTIVE secret per (type, scope); NULL scope (global) collides with NULL scope.
    (
        f"CREATE UNIQUE INDEX secrets_one_active_idx ON {SCHEMA}.secrets (secret_type, scope_id) "
        "NULLS NOT DISTINCT WHERE state = 2"
    ),
    # A public key id names exactly one key within its (type, scope).
    (
        f"CREATE UNIQUE INDEX secrets_kid_idx ON {SCHEMA}.secrets (secret_type, scope_id, kid) "
        "NULLS NOT DISTINCT WHERE kid IS NOT NULL"
    ),
    # Linear event chain: each event extends exactly one predecessor, and there is one genesis.
    (
        f"CREATE UNIQUE INDEX secret_events_prev_idx ON {SCHEMA}.secret_events (prev_chain_hash) "
        "WHERE prev_chain_hash IS NOT NULL"
    ),
    (
        f"CREATE UNIQUE INDEX secret_events_genesis_idx ON {SCHEMA}.secret_events ((true)) "
        "WHERE prev_chain_hash IS NULL"
    ),
    f"CREATE INDEX secret_events_secret_idx ON {SCHEMA}.secret_events (secret_id)",
)

_REJECT_FN = f"""
CREATE FUNCTION {SCHEMA}.reject_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $fn$
BEGIN
    RAISE EXCEPTION 'platform_vault tables refuse this operation (append-only or undeletable)'
        USING ERRCODE = '23000';
END
$fn$
"""

_GUARD_FN = f"""
CREATE FUNCTION {SCHEMA}.secrets_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $fn$
DECLARE
    activating boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        activating := (NEW.state = 2);
        IF NEW.state NOT IN (1, 2) THEN
            RAISE EXCEPTION 'a platform secret is created pending or active only'
                USING ERRCODE = '23000';
        END IF;
    ELSE
        activating := (NEW.state = 2 AND OLD.state <> 2);
        IF OLD.state = 5 THEN
            RAISE EXCEPTION 'a destroyed platform secret is immutable' USING ERRCODE = '23000';
        END IF;
        IF NEW.state < OLD.state THEN
            RAISE EXCEPTION 'platform secret state only moves forward' USING ERRCODE = '23000';
        END IF;
        IF OLD.state = 1 AND NEW.state NOT IN (1, 2, 5) THEN
            RAISE EXCEPTION 'a pending platform secret is activated or destroyed, never skipped ahead'
                USING ERRCODE = '23000';
        END IF;
        IF NEW.id <> OLD.id
            OR NEW.public_id <> OLD.public_id
            OR NEW.secret_type <> OLD.secret_type
            OR NEW.scope_id IS DISTINCT FROM OLD.scope_id
            OR NEW.version <> OLD.version
            OR NEW.algorithm <> OLD.algorithm
            OR NEW.kid IS DISTINCT FROM OLD.kid
            OR NEW.public_material IS DISTINCT FROM OLD.public_material
            OR (NEW.fingerprint <> OLD.fingerprint AND NEW.kek_id = OLD.kek_id)
            OR NEW.created_at <> OLD.created_at
            OR (OLD.activated_at IS NOT NULL AND NEW.activated_at IS DISTINCT FROM OLD.activated_at)
        THEN
            RAISE EXCEPTION 'platform secret identity and public columns are immutable'
                USING ERRCODE = '23000';
        END IF;
    END IF;
    IF activating THEN
        IF EXISTS (
            SELECT 1 FROM {SCHEMA}.secrets s
            WHERE s.secret_type = NEW.secret_type
              AND s.scope_id IS NOT DISTINCT FROM NEW.scope_id
              AND s.version > NEW.version
              AND s.state >= 2
        ) THEN
            RAISE EXCEPTION 'activating an older platform secret version is refused'
                USING ERRCODE = '23000';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$
"""  # nosec B608 -- fixed literals

_TRIGGERS = (
    (
        f"CREATE TRIGGER secrets_guard BEFORE INSERT OR UPDATE ON {SCHEMA}.secrets "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.secrets_guard()"
    ),
    (
        f"CREATE TRIGGER secrets_no_delete BEFORE DELETE ON {SCHEMA}.secrets "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
    ),
    (
        f"CREATE TRIGGER secrets_no_truncate BEFORE TRUNCATE ON {SCHEMA}.secrets "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
    ),
    (
        f"CREATE TRIGGER secret_events_append_only BEFORE UPDATE OR DELETE ON {SCHEMA}.secret_events "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
    ),
    (
        f"CREATE TRIGGER secret_events_no_truncate BEFORE TRUNCATE ON {SCHEMA}.secret_events "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
    ),
    (
        f"CREATE TRIGGER root_marker_no_delete BEFORE DELETE ON {SCHEMA}.root_marker "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
    ),
    (
        f"CREATE TRIGGER root_marker_no_truncate BEFORE TRUNCATE ON {SCHEMA}.root_marker "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
    ),
)

_VIEW = f"""
CREATE VIEW platform_vault.public_keys WITH (security_barrier = true) AS
SELECT secret_type, scope_id, version, kid, state, public_material, activated_at, rotated_at
FROM {SCHEMA}.secrets
WHERE public_material IS NOT NULL AND state IN (2, 3)
"""  # nosec B608 -- fixed literals

#: COMMENTs document the int-code mappings next to the columns (no colons: this goes through
#: `sa.text`, where `:word` would read as a bind parameter).
_COMMENTS = (
    (
        "secrets",
        "secret_type",
        "positive code from the hub-api platform secret registry",
    ),
    ("secrets", "state", "1 pending, 2 active, 3 verify-only, 4 retired, 5 destroyed"),
    (
        "secrets",
        "algorithm",
        "1 Ed25519, 2 ES256 P-256, 3 random 256-bit, 4 password 32 bytes base64url",
    ),
    (
        "secrets",
        "external_sync_state",
        "0 not applicable, 1 pending, 2 applied, 3 failed",
    ),
    (
        "secrets",
        "reason",
        "1 generated, 2 imported, 3 restored, 4 rotated, 5 retired, 6 destroyed, 7 rewrapped",
    ),
    (
        "secrets",
        "seal_version",
        "envelope layout version, equals header byte 4 of sealed_value",
    ),
    (
        "secret_events",
        "event_type",
        (
            "1 generated, 2 imported, 3 restored, 4 activated, 5 verify-only, 6 retired, "
            "7 destroyed, 8 rotation requested, 9 open failed, 10 drift, 11 escrowed, "
            "12 external applied, 13 rewrapped, 14 boot refused"
        ),
    ),
    ("secret_events", "outcome", "1 success, 2 failure"),
)


def _revoke_all_from_others(*, only_if_schema_exists: bool) -> str:
    """DO block revoking every privilege on the schema from every non-superuser, non-owner role.

    Covers LOGIN roles, NOLOGIN groups and anything default privileges reached. With
    `only_if_schema_exists` (downgrade) the loop is skipped when the schema is already gone.
    """
    loop = (
        "FOR r IN SELECT rolname FROM pg_roles "  # nosec B608
        "WHERE NOT rolsuper AND rolname <> current_user AND rolname !~ '^pg_' LOOP "
        f"EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM %I', r.rolname); "
        f"EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA {SCHEMA} FROM %I', r.rolname); "
        f"EXECUTE format('REVOKE ALL ON SCHEMA {SCHEMA} FROM %I', r.rolname); "
        "END LOOP;"
    )
    if only_if_schema_exists:
        loop = (
            f"IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '{SCHEMA}') THEN "  # nosec B608
            f"{loop} END IF;"
        )
    return f"DO $do$ DECLARE r record; BEGIN {loop} END $do$"


def _guarded(role: str, body: str) -> str:
    """Run `body` only if `role` exists (a fresh-replay / minimal-chain safe grant)."""
    return (
        f"DO $do$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "  # nosec B608
        f"{body} END IF; END $do$"
    )


def upgrade() -> None:
    """Create the schema, tables, guards and view, then grant exactly the access model."""
    op.execute(f"CREATE SCHEMA {SCHEMA}")
    op.execute(_SECRETS)
    op.execute(_EVENTS)
    op.execute(_ROOT_MARKER)
    for statement in _INDEXES:
        op.execute(statement)
    op.execute(_REJECT_FN)
    op.execute(_GUARD_FN)
    for statement in _TRIGGERS:
        op.execute(statement)
    op.execute(_VIEW)
    for table, column, text in _COMMENTS:
        op.execute(f"COMMENT ON COLUMN {SCHEMA}.{table}.{column} IS '{text}'")
    # Deny by default: nothing for PUBLIC, and nothing for any other role that default privileges
    # or group membership might already have reached (the legacy public_dml roles, NOLOGIN groups).
    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(_revoke_all_from_others(only_if_schema_exists=False))
    hub_grants = "".join(
        f"GRANT {', '.join(privs)} ON {SCHEMA}.{table} TO {HUB_API_ROLE}; "
        for table, privs in HUB_API_GRANTS.items()
    )
    op.execute(
        _guarded(
            HUB_API_ROLE,
            f"GRANT USAGE ON SCHEMA {SCHEMA} TO {HUB_API_ROLE}; {hub_grants}"
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO {HUB_API_ROLE};",
        )
    )
    for role in PUBLIC_KEY_READERS:
        op.execute(
            _guarded(
                role,
                f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}; "
                f"GRANT SELECT ON {SCHEMA}.{VIEW} TO {role};",
            )
        )


def downgrade() -> None:
    """Drop the whole schema -- refused while sealed secrets exist, unless explicitly confirmed.

    Every grant is revoked first, so no ACL entry is left behind to make a later `DROP ROLE`
    fail with "privileges for schema/table ...". Dropping the schema removes the tables, the
    view, the functions and the triggers; DROP does not fire the DELETE/TRUNCATE guards.
    """
    if os.environ.get(ALLOW_DROP_ENV) != "1":
        op.execute(
            "DO $do$ BEGIN "  # nosec B608
            f"IF to_regclass('{SCHEMA}.secrets') IS NOT NULL THEN "
            f"IF EXISTS (SELECT 1 FROM {SCHEMA}.secrets) THEN "
            f"RAISE EXCEPTION 'refusing to drop {SCHEMA}: it holds sealed platform secrets "
            f"(set {ALLOW_DROP_ENV}=1 to confirm)'; "
            "END IF; END IF; END $do$"
        )
    op.execute(_revoke_all_from_others(only_if_schema_exists=True))
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
