"""Analytics foundation (Phase 1): two schemas with a HARD wall, governance tables, DB roles.

LAYER A `analytics_fp` -- first-party, per-community analytics. Owned by the community/tenant,
NEVER sold. Free tier reads basic counts (`community_hourly`, `member_daily`); Premium reads the
in-depth tables (`member_sessions`, `cohort_weekly`, `member_signals_daily`). The tier gate is
enforced server-side by the read endpoints (later work item); the schema holds both.

LAYER B `analytics_trend` -- the only dataset that may ever be sellable: tenant-wide anonymized
trend metadata, tenant x week or coarser, NO community/user/channel dimension, no user-level or
event-level rows. `tenant_trend.cohort_size >= 50` (k-anonymity) is a CHECK, so a below-threshold
cell cannot be stored. No export code path exists in v3.0 (`export_runs` is an audit log only).

Governance (public schema): `analytics_dataset_registry` (every dataset, purpose, classification,
retention, lineage), `data_use_exclusions` (do-not-sell / GPC / objection / erased / not-adult),
`tenant_data_programme_optin` (no row = OFF), `export_recipients`, `platform_clearance`
(empty by default), `export_runs`. Nothing is seeded that enables anything.

PII boundary: analytics stores hold only numeric internal ids and user UUID tokens -- never
handles, usernames, message text or free text. `member_signals_daily` is PROFILING (first-party,
Premium-only, admin-only, never used for automated legal-effect decisions); recorded in the
registry lineage.

ROLES (NOLOGIN group roles; the LOGIN roles + passwords are provisioned later by Helm):
`analytics_writer` DML on analytics_fp + SELECT data_use_exclusions; `analytics_reader` SELECT on
analytics_fp; `trend_builder` SELECT member_daily/data_use_exclusions/tenant_data_programme_optin +
DML on analytics_trend; `trend_exporter` SELECT analytics_trend ONLY + INSERT export_runs. PUBLIC
is revoked on both schemas. hub-api (if its role exists) gets SELECT/DELETE only on the three
user-keyed analytics_fp tables, so the Art. 17 erasure executor can reach them.

Downgrade drops every table/schema but REFUSES while any data table is non-empty unless
`ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE=1`. Roles are cluster-wide and are left in place (re-created
idempotently on the next upgrade). All DDL is idempotent; `SET LOCAL lock_timeout` bounds waits.

Revision ID: 0129_analytics_foundation
Revises: 0127_tenant_license_key
Create Date: 2026-11-18
"""

from __future__ import annotations

import os

import sqlalchemy as sa

from alembic import op

revision = "0129_analytics_foundation"
down_revision = "0127_tenant_license_key"
branch_labels = None
depends_on = None

#: Governance columns every layer A table carries (codes are int enums, see docs/analytics-foundation.md).
_GOV = """
    purpose_code        SMALLINT NOT NULL DEFAULT 1 CHECK (purpose_code = 1),
    classification_code SMALLINT NOT NULL DEFAULT {cls} CHECK (classification_code IN (1, 2)),
    retention_days      INTEGER  NOT NULL DEFAULT 90 CHECK (retention_days BETWEEN 1 AND 400),
    provenance_code     SMALLINT NOT NULL DEFAULT 1 CHECK (provenance_code BETWEEN 1 AND 5),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
"""
_GOV_AGG = _GOV.format(cls=1)
_GOV_PSEUDO = _GOV.format(cls=2)

_SCHEMAS = (
    "CREATE SCHEMA IF NOT EXISTS analytics_fp",
    "CREATE SCHEMA IF NOT EXISTS analytics_trend",
)

_TABLES = (
    f"""
CREATE TABLE IF NOT EXISTS analytics_fp.community_hourly (
    tenant_id     INTEGER NOT NULL,
    community_id  INTEGER NOT NULL,
    hour_ts       TIMESTAMPTZ NOT NULL,
    metric_code   SMALLINT NOT NULL CHECK (metric_code > 0),
    platform_code SMALLINT NOT NULL CHECK (platform_code BETWEEN 1 AND 5),
    value         BIGINT NOT NULL CHECK (value >= 0),
    {_GOV_AGG},
    PRIMARY KEY (community_id, hour_ts, metric_code, platform_code)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS analytics_fp.member_daily (
    community_id INTEGER NOT NULL,
    user_uuid    UUID NOT NULL,
    day          DATE NOT NULL,
    msgs         INTEGER NOT NULL DEFAULT 0 CHECK (msgs >= 0),
    cmds         INTEGER NOT NULL DEFAULT 0 CHECK (cmds >= 0),
    presence_s   INTEGER NOT NULL DEFAULT 0 CHECK (presence_s >= 0),
    first_ts     TIMESTAMPTZ,
    last_ts      TIMESTAMPTZ,
    {_GOV_PSEUDO},
    PRIMARY KEY (community_id, user_uuid, day)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS analytics_fp.member_sessions (
    community_id  INTEGER NOT NULL,
    user_uuid     UUID NOT NULL,
    session_start TIMESTAMPTZ NOT NULL,
    session_end   TIMESTAMPTZ,
    platform_code SMALLINT NOT NULL CHECK (platform_code BETWEEN 1 AND 5),
    {_GOV_PSEUDO},
    PRIMARY KEY (community_id, user_uuid, session_start, platform_code),
    CONSTRAINT ck_member_sessions_order CHECK (session_end IS NULL OR session_end >= session_start)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS analytics_fp.cohort_weekly (
    community_id   INTEGER NOT NULL,
    cohort_week    DATE NOT NULL,
    week_offset    SMALLINT NOT NULL CHECK (week_offset >= 0),
    active_members INTEGER NOT NULL CHECK (active_members >= 0),
    cohort_size    INTEGER NOT NULL CHECK (cohort_size >= 0),
    {_GOV_AGG},
    PRIMARY KEY (community_id, cohort_week, week_offset)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS analytics_fp.member_signals_daily (
    community_id INTEGER NOT NULL,
    user_uuid    UUID NOT NULL,
    day          DATE NOT NULL,
    signal_code  SMALLINT NOT NULL CHECK (signal_code > 0),
    score        SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
    {_GOV_PSEUDO},
    PRIMARY KEY (community_id, user_uuid, day, signal_code)
)
""",
    """
CREATE TABLE IF NOT EXISTS analytics_trend.tenant_trend (
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    week            DATE NOT NULL,
    metric_code     SMALLINT NOT NULL CHECK (metric_code > 0),
    dim_code        SMALLINT NOT NULL CHECK (dim_code >= 0),
    dim_value_code  SMALLINT NOT NULL CHECK (dim_value_code >= 0),
    value           BIGINT NOT NULL CHECK (value >= 0),
    cohort_size     INTEGER NOT NULL,
    provenance_code SMALLINT NOT NULL CHECK (provenance_code BETWEEN 1 AND 5),
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, week, metric_code, dim_code, dim_value_code),
    CONSTRAINT ck_tenant_trend_k_anonymity CHECK (cohort_size >= 50)
)
""",
    """
CREATE TABLE IF NOT EXISTS analytics_dataset_registry (
    dataset             TEXT PRIMARY KEY,
    layer               SMALLINT NOT NULL CHECK (layer IN (1, 2)),
    purpose_code        SMALLINT NOT NULL CHECK (purpose_code IN (1, 2)),
    classification_code SMALLINT NOT NULL CHECK (classification_code IN (1, 2)),
    retention_days      INTEGER NOT NULL CHECK (retention_days >= 1),
    min_k               INTEGER NOT NULL DEFAULT 0 CHECK (min_k >= 0),
    lineage             JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes               TEXT NOT NULL DEFAULT '',
    CONSTRAINT ck_adr_layer_purpose CHECK (
        (layer = 1 AND purpose_code = 1) OR (layer = 2 AND purpose_code = 2 AND min_k >= 50)
    )
)
""",
    """
CREATE TABLE IF NOT EXISTS data_use_exclusions (
    user_uuid   UUID NOT NULL,
    reason_code SMALLINT NOT NULL CHECK (reason_code BETWEEN 1 AND 5),
    since       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_uuid, reason_code)
)
""",
    """
CREATE TABLE IF NOT EXISTS tenant_data_programme_optin (
    tenant_id     INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    terms_version INTEGER NOT NULL CHECK (terms_version >= 1),
    status_code   SMALLINT NOT NULL DEFAULT 0 CHECK (status_code BETWEEN 0 AND 2),
    signer_uuid   UUID,
    signed_at     TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, terms_version)
)
""",
    """
CREATE TABLE IF NOT EXISTS export_recipients (
    recipient_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id             UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    contract_ref          TEXT,
    no_reidentify_no_join BOOLEAN NOT NULL DEFAULT FALSE,
    region_code           SMALLINT,
    active                BOOLEAN NOT NULL DEFAULT FALSE
)
""",
    """
CREATE TABLE IF NOT EXISTS platform_clearance (
    platform_code SMALLINT PRIMARY KEY CHECK (platform_code BETWEEN 1 AND 5),
    doc_ref       TEXT NOT NULL,
    cleared_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
""",
    """
CREATE TABLE IF NOT EXISTS export_runs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id       UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_uuid      UUID,
    dataset         TEXT NOT NULL REFERENCES analytics_dataset_registry(dataset),
    recipient_id    BIGINT REFERENCES export_recipients(recipient_id),
    mode_code       SMALLINT NOT NULL CHECK (mode_code IN (1, 2)),
    rows_considered BIGINT NOT NULL DEFAULT 0 CHECK (rows_considered >= 0),
    rows_suppressed BIGINT NOT NULL DEFAULT 0 CHECK (rows_suppressed >= 0),
    rows_emitted    BIGINT NOT NULL DEFAULT 0 CHECK (rows_emitted >= 0),
    decision_code   SMALLINT NOT NULL CHECK (decision_code IN (1, 2)),
    deny_codes      TEXT[] NOT NULL DEFAULT ARRAY[]::text[]
)
""",
)

#: Retention-purge and main-read-path indexes.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_ch_hour ON analytics_fp.community_hourly (hour_ts)",
    "CREATE INDEX IF NOT EXISTS ix_ch_tenant ON analytics_fp.community_hourly (tenant_id, hour_ts)",
    "CREATE INDEX IF NOT EXISTS ix_md_day ON analytics_fp.member_daily (day)",
    "CREATE INDEX IF NOT EXISTS ix_md_user ON analytics_fp.member_daily (user_uuid)",
    "CREATE INDEX IF NOT EXISTS ix_ms_start ON analytics_fp.member_sessions (session_start)",
    "CREATE INDEX IF NOT EXISTS ix_ms_user ON analytics_fp.member_sessions (user_uuid)",
    "CREATE INDEX IF NOT EXISTS ix_cw_week ON analytics_fp.cohort_weekly (cohort_week)",
    "CREATE INDEX IF NOT EXISTS ix_msd_day ON analytics_fp.member_signals_daily (day)",
    "CREATE INDEX IF NOT EXISTS ix_msd_user ON analytics_fp.member_signals_daily (user_uuid)",
    "CREATE INDEX IF NOT EXISTS ix_tt_week ON analytics_trend.tenant_trend (week)",
    "CREATE INDEX IF NOT EXISTS ix_er_decided ON export_runs (decided_at)",
)

_FP_LINEAGE = (
    "jsonb_build_object('source', 'valkey stream waddles\\:analytics', 'writer', "
    "'svc_analytics', 'tier', '{tier}', 'pii', '{pii}')"
)
_PROFILING_LINEAGE = (
    "jsonb_build_object('source', 'valkey stream waddles\\:analytics', 'writer', "
    "'svc_analytics', 'tier', 'premium', 'pii', 'user_uuid token', 'profiling', true, "
    "'scope', 'first_party', 'premium_only', true, 'admin_only', true, "
    "'automated_legal_effect_decisions', false)"
)
_TREND_LINEAGE = (
    "jsonb_build_object('input', 'analytics_fp.member_daily', 'consent_filtered', true, "
    "'one_way_job', 'trend_builder', 'excludes', jsonb_build_array('do_not_sell', 'gpc', "
    "'objection', 'erased', 'age_not_adult', 'no_age_signal', 'eu_tenants'), "
    "'export_enabled', false)"
)

#: (dataset, layer, purpose, classification, retention, min_k, lineage SQL, notes)
_SEED = (
    (
        "analytics_fp.community_hourly",
        1,
        1,
        1,
        400,
        0,
        _FP_LINEAGE.format(tier="free", pii="none (numeric ids only)"),
        "Per-community hourly counters. Free = basic counts (90d), Premium retention 400d.",
    ),
    (
        "analytics_fp.member_daily",
        1,
        1,
        2,
        400,
        0,
        _FP_LINEAGE.format(tier="free", pii="user_uuid token"),
        "Per-member daily activity. Free 90d, Premium 400d. Sole input to layer B.",
    ),
    (
        "analytics_fp.member_sessions",
        1,
        1,
        2,
        400,
        0,
        _FP_LINEAGE.format(tier="premium", pii="user_uuid token"),
        "Premium-only presence sessions.",
    ),
    (
        "analytics_fp.cohort_weekly",
        1,
        1,
        1,
        400,
        0,
        _FP_LINEAGE.format(tier="premium", pii="none (aggregate)"),
        "Premium-only retention cohorts.",
    ),
    (
        "analytics_fp.member_signals_daily",
        1,
        1,
        2,
        400,
        0,
        _PROFILING_LINEAGE,
        "PROFILING (bad-actor/bot signals): first-party, Premium-only, admin-only, never used "
        "for automated decisions with legal or similarly significant effect.",
    ),
    (
        "analytics_trend.tenant_trend",
        2,
        2,
        1,
        400,
        50,
        _TREND_LINEAGE,
        "Tenant-wide anonymized trend metadata, k>=50. Retention never longer than input. "
        "No export path exists in v3.0.",
    ),
)

_ROLES = ("analytics_writer", "analytics_reader", "trend_builder", "trend_exporter")
_USER_KEYED = ("member_daily", "member_sessions", "member_signals_daily")

_DATA_TABLES = (
    "analytics_fp.community_hourly",
    "analytics_fp.member_daily",
    "analytics_fp.member_sessions",
    "analytics_fp.cohort_weekly",
    "analytics_fp.member_signals_daily",
    "analytics_trend.tenant_trend",
    "export_runs",
    "export_recipients",
    "platform_clearance",
    "tenant_data_programme_optin",
    "data_use_exclusions",
)
_DROP_ORDER = (*_DATA_TABLES, "analytics_dataset_registry")

#: Set to `1` to knowingly discard analytics data on downgrade.
_ALLOW_ENV = "ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"


def _role_sql(role: str) -> str:
    """Idempotent NOLOGIN least-privilege group role (re-asserts attributes every run)."""
    return (
        f"DO $$ BEGIN\n"  # nosec B608 -- fixed role literals
        f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"    CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;\n"
        f"  END IF;\n"
        f"  ALTER ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;\n"
        f"END $$"
    )


_GOV_TABLES = (
    "analytics_dataset_registry",
    "data_use_exclusions",
    "tenant_data_programme_optin",
    "export_recipients",
    "platform_clearance",
    "export_runs",
)

_GRANTS = (
    "REVOKE ALL ON SCHEMA analytics_fp FROM PUBLIC",
    "REVOKE ALL ON SCHEMA analytics_trend FROM PUBLIC",
    "REVOKE ALL ON ALL TABLES IN SCHEMA analytics_fp FROM PUBLIC",
    "REVOKE ALL ON ALL TABLES IN SCHEMA analytics_trend FROM PUBLIC",
    *(f"REVOKE ALL ON {t} FROM PUBLIC" for t in _GOV_TABLES),
    # governance tables live in public: the roles need USAGE to resolve them (public may be
    # locked down on hardened clusters); this grants name resolution only, no table access
    *(
        f"GRANT USAGE ON SCHEMA public TO {r}"
        for r in (
            "analytics_writer",
            "analytics_reader",
            "trend_builder",
            "trend_exporter",
        )
    ),
    # analytics_writer
    "GRANT USAGE ON SCHEMA analytics_fp TO analytics_writer",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA analytics_fp TO analytics_writer",
    "GRANT SELECT ON data_use_exclusions TO analytics_writer",
    # analytics_reader
    "GRANT USAGE ON SCHEMA analytics_fp TO analytics_reader",
    "GRANT SELECT ON ALL TABLES IN SCHEMA analytics_fp TO analytics_reader",
    # trend_builder
    "GRANT USAGE ON SCHEMA analytics_fp, analytics_trend TO trend_builder",
    "GRANT SELECT ON analytics_fp.member_daily TO trend_builder",
    "GRANT SELECT ON data_use_exclusions, tenant_data_programme_optin TO trend_builder",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA analytics_trend TO trend_builder",
    # trend_exporter: nothing per-user, nothing in analytics_fp
    "GRANT USAGE ON SCHEMA analytics_trend TO trend_exporter",
    "GRANT SELECT ON ALL TABLES IN SCHEMA analytics_trend TO trend_exporter",
    "GRANT INSERT ON export_runs TO trend_exporter",
)

#: Art. 17 erasure executor (hub-api) must reach the user-keyed tables; guarded so the minimal CI
#: chain (no hub_api group role) still applies.
_HUB_API_ERASURE = (
    "DO $$ BEGIN\n"
    "  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN\n"
    "    GRANT USAGE ON SCHEMA analytics_fp TO hub_api;\n"
    + "".join(
        f"    GRANT SELECT, DELETE ON analytics_fp.{t} TO hub_api;\n"
        for t in _USER_KEYED
    )
    + "  END IF;\n"
    "END $$"
)


def _seed_sql() -> list[str]:
    """INSERT ... ON CONFLICT DO NOTHING for every analytics dataset (fixed literals only)."""
    out = []
    for ds, layer, purpose, cls, ret, k, lineage, notes in _SEED:
        notes_q = notes.replace("'", "''")
        out.append(
            "INSERT INTO analytics_dataset_registry "  # nosec B608 -- literals
            "(dataset, layer, purpose_code, classification_code, retention_days, min_k, "
            "lineage, notes) VALUES "
            f"('{ds}', {layer}, {purpose}, {cls}, {ret}, {k}, {lineage}, '{notes_q}') "
            "ON CONFLICT (dataset) DO NOTHING"
        )
    return out


def upgrade() -> None:
    """Create schemas, tables, indexes, registry seed, roles and grants (idempotent)."""
    op.execute("SET LOCAL lock_timeout = '10s'")
    for statement in (*_SCHEMAS, *_TABLES, *_INDEXES, *_seed_sql()):
        op.execute(statement)
    for role in _ROLES:
        op.execute(_role_sql(role))
    for statement in _GRANTS:
        op.execute(statement)
    op.execute(_HUB_API_ERASURE)


def _held() -> dict[str, int]:
    """Row counts of every non-empty data table (the registry seed is excluded)."""
    bind = op.get_bind()
    held: dict[str, int] = {}
    for table in _DATA_TABLES:
        count = (
            bind.execute(
                sa.text(
                    f"SELECT CASE WHEN to_regclass('{table}') IS NULL THEN 0 "  # nosec B608
                    f"ELSE (SELECT count(*) FROM {table}) END"
                )
            ).scalar()
            or 0
        )
        if count:
            held[table] = int(count)
    return held


def downgrade() -> None:
    """Drop tables + schemas; REFUSES while any holds rows unless the env override is set."""
    held = _held()
    if held and os.environ.get(_ALLOW_ENV) != "1":
        raise RuntimeError(
            f"Refusing to drop analytics tables holding data: {held}. "
            f"Preserve them, or set {_ALLOW_ENV}=1."
        )
    op.execute("SET LOCAL lock_timeout = '10s'")
    for table in _DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP SCHEMA IF EXISTS analytics_trend")
    op.execute("DROP SCHEMA IF EXISTS analytics_fp")
