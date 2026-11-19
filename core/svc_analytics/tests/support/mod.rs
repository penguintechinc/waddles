//! Shared test fixtures: an isolated real-Postgres database per test (schema mirrors
//! alembic 0129 + 0131, roles mirror the migration grants), real Valkey streams under
//! unique names, and a scriptable fake [`AnalyticsDb`]. `TEST_DATABASE_URL` and
//! `VALKEY_URL` must be set: tests FAIL loudly (never skip) without them.
#![allow(dead_code)]

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

use sea_orm::{ConnectionTrait, Database, DatabaseConnection, DbBackend, Statement};
use svc_analytics::aggregate::{Accepted, EntryId};
use svc_analytics::config::Config;
use svc_analytics::contract::{Event, Kind};
use svc_analytics::db::{AnalyticsDb, ApplyReport, DbFault, PgDb, PgStore, PurgeReport};
use uuid::Uuid;

static SEQ: AtomicU32 = AtomicU32::new(0);

pub const NOW_S: i64 = 1_800_000_000; // 2027-01-15 08:00:00 UTC
pub const DAY: i64 = 86_400;

pub fn user(n: u128) -> Uuid {
    Uuid::from_u128(0x0b9f_2c1e_7d3a_4c58_9a1e_0000_0000_0000 + n)
}

const DDL: &str = r#"
CREATE SCHEMA analytics_fp;
CREATE SCHEMA analytics_ops;
CREATE TABLE analytics_fp.community_hourly (
    tenant_id INTEGER NOT NULL, community_id INTEGER NOT NULL, hour_ts TIMESTAMPTZ NOT NULL,
    metric_code SMALLINT NOT NULL CHECK (metric_code > 0),
    platform_code SMALLINT NOT NULL CHECK (platform_code BETWEEN 1 AND 5),
    value BIGINT NOT NULL CHECK (value >= 0),
    purpose_code SMALLINT NOT NULL DEFAULT 1 CHECK (purpose_code = 1),
    classification_code SMALLINT NOT NULL DEFAULT 1 CHECK (classification_code IN (1, 2)),
    retention_days INTEGER NOT NULL DEFAULT 90 CHECK (retention_days BETWEEN 1 AND 400),
    provenance_code SMALLINT NOT NULL DEFAULT 1 CHECK (provenance_code BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (community_id, hour_ts, metric_code, platform_code));
CREATE TABLE analytics_fp.member_daily (
    community_id INTEGER NOT NULL, user_uuid UUID NOT NULL, day DATE NOT NULL,
    msgs INTEGER NOT NULL DEFAULT 0 CHECK (msgs >= 0), cmds INTEGER NOT NULL DEFAULT 0 CHECK (cmds >= 0),
    presence_s INTEGER NOT NULL DEFAULT 0 CHECK (presence_s >= 0),
    first_ts TIMESTAMPTZ, last_ts TIMESTAMPTZ,
    purpose_code SMALLINT NOT NULL DEFAULT 1 CHECK (purpose_code = 1),
    classification_code SMALLINT NOT NULL DEFAULT 2 CHECK (classification_code IN (1, 2)),
    retention_days INTEGER NOT NULL DEFAULT 90 CHECK (retention_days BETWEEN 1 AND 400),
    provenance_code SMALLINT NOT NULL DEFAULT 1 CHECK (provenance_code BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (community_id, user_uuid, day));
CREATE TABLE analytics_fp.member_sessions (
    community_id INTEGER NOT NULL, user_uuid UUID NOT NULL, session_start TIMESTAMPTZ NOT NULL,
    session_end TIMESTAMPTZ, platform_code SMALLINT NOT NULL CHECK (platform_code BETWEEN 1 AND 5),
    purpose_code SMALLINT NOT NULL DEFAULT 1 CHECK (purpose_code = 1),
    classification_code SMALLINT NOT NULL DEFAULT 2 CHECK (classification_code IN (1, 2)),
    retention_days INTEGER NOT NULL DEFAULT 90 CHECK (retention_days BETWEEN 1 AND 400),
    provenance_code SMALLINT NOT NULL DEFAULT 1 CHECK (provenance_code BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (community_id, user_uuid, session_start, platform_code),
    CONSTRAINT ck_member_sessions_order CHECK (session_end IS NULL OR session_end >= session_start));
CREATE TABLE analytics_fp.cohort_weekly (
    community_id INTEGER NOT NULL, cohort_week DATE NOT NULL,
    week_offset SMALLINT NOT NULL CHECK (week_offset >= 0),
    active_members INTEGER NOT NULL CHECK (active_members >= 0), cohort_size INTEGER NOT NULL CHECK (cohort_size >= 0),
    purpose_code SMALLINT NOT NULL DEFAULT 1 CHECK (purpose_code = 1),
    classification_code SMALLINT NOT NULL DEFAULT 1 CHECK (classification_code IN (1, 2)),
    retention_days INTEGER NOT NULL DEFAULT 90 CHECK (retention_days BETWEEN 1 AND 400),
    provenance_code SMALLINT NOT NULL DEFAULT 1 CHECK (provenance_code BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (community_id, cohort_week, week_offset));
CREATE TABLE analytics_fp.member_signals_daily (
    community_id INTEGER NOT NULL, user_uuid UUID NOT NULL, day DATE NOT NULL,
    signal_code SMALLINT NOT NULL CHECK (signal_code > 0), score SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
    purpose_code SMALLINT NOT NULL DEFAULT 1 CHECK (purpose_code = 1),
    classification_code SMALLINT NOT NULL DEFAULT 2 CHECK (classification_code IN (1, 2)),
    retention_days INTEGER NOT NULL DEFAULT 90 CHECK (retention_days BETWEEN 1 AND 400),
    provenance_code SMALLINT NOT NULL DEFAULT 1 CHECK (provenance_code BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (community_id, user_uuid, day, signal_code));
CREATE TABLE public.data_use_exclusions (
    user_uuid UUID NOT NULL, reason_code SMALLINT NOT NULL CHECK (reason_code BETWEEN 1 AND 5),
    since TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (user_uuid, reason_code));
CREATE TABLE public.community_tier_allocations (
    id BIGSERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL, community_id INTEGER NOT NULL,
    tier_code SMALLINT NOT NULL CHECK (tier_code = 2), released_at TIMESTAMPTZ);
CREATE TABLE analytics_ops.applied_entries (
    entry_ms BIGINT NOT NULL CHECK (entry_ms >= 0), entry_seq BIGINT NOT NULL CHECK (entry_seq >= 0),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (entry_ms, entry_seq));
"#;

/// An isolated database plus a LOGIN role holding exactly the writer grants.
pub struct Fixture {
    pub admin: DatabaseConnection,
    pub owner_url: String,
    pub writer_url: String,
    /// A writer role WITHOUT the proposed `community_tier_allocations` column grant.
    pub ungranted_url: String,
    db_name: String,
    admin_root: DatabaseConnection,
}

pub fn admin_url() -> String {
    std::env::var("TEST_DATABASE_URL")
        .expect("TEST_DATABASE_URL must be set: these tests need a REAL Postgres (no silent skip)")
}

pub fn valkey_url() -> String {
    std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: these tests need a REAL Valkey (no silent skip)")
}

fn with_db(url: &str, db: &str, user: Option<(&str, &str)>) -> String {
    let (scheme, rest) = url.split_once("://").expect("url");
    let after_auth = rest.rsplit_once('@').map_or(rest, |(_, h)| h);
    let host = after_auth.split('/').next().expect("host");
    let (u, p) = user.map_or_else(
        || {
            let auth = rest.rsplit_once('@').map_or("", |(a, _)| a);
            let (u, p) = auth.split_once(':').unwrap_or((auth, ""));
            (u.to_string(), p.to_string())
        },
        |(u, p)| (u.to_string(), p.to_string()),
    );
    format!("{scheme}://{u}:{p}@{host}/{db}")
}

/// Creates (once, serialized by an advisory lock) a template database holding the schema, named
/// after a hash of the DDL so a schema change never reuses a stale template.
async fn ensure_template() -> String {
    use sea_orm::ConnectOptions;
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    DDL.hash(&mut h);
    let name = format!("an_template_{:x}", h.finish());
    // ONE connection: a session-level advisory lock must be taken and released on the same
    // session, which a multi-connection pool does not guarantee.
    let mut o = ConnectOptions::new(admin_url());
    o.max_connections(1).min_connections(1).sqlx_logging(false);
    let root = Database::connect(o).await.expect("template admin connect");
    root.execute_unprepared("SELECT pg_advisory_lock(727001)")
        .await
        .expect("lock");
    let exists = root
        .query_one_raw(Statement::from_string(
            DbBackend::Postgres,
            format!("SELECT 1::bigint FROM pg_database WHERE datname = '{name}'"),
        ))
        .await
        .expect("probe")
        .is_some();
    if !exists {
        root.execute_unprepared(&format!("CREATE DATABASE {name}"))
            .await
            .expect("create template");
        let t = Database::connect(with_db(&admin_url(), &name, None))
            .await
            .expect("template connect");
        t.execute_unprepared(DDL).await.expect("ddl");
        t.close().await.expect("close");
    }
    root.execute_unprepared("SELECT pg_advisory_unlock(727001)")
        .await
        .expect("unlock");
    root.close().await.expect("close");
    name
}

impl Fixture {
    pub async fn new() -> Self {
        let n = SEQ.fetch_add(1, Ordering::SeqCst);
        let tag = format!("{}_{n}", std::process::id());
        let db_name = format!("an_{tag}");
        let writer = format!("an_writer_{tag}");
        let ungranted = format!("an_ungranted_{tag}");
        let writer_grp = format!("an_wgrp_{tag}");
        let ungranted_grp = format!("an_ugrp_{tag}");
        let admin_root = Database::connect(admin_url()).await.expect("admin connect");
        let template = ensure_template().await;
        admin_root
            .execute_unprepared(&format!("CREATE DATABASE {db_name} TEMPLATE {template}"))
            .await
            .expect("create db");
        let owner_url = with_db(&admin_url(), &db_name, None);
        let admin = Database::connect(owner_url.clone())
            .await
            .expect("owner connect");
        // Production shape: NOLOGIN group role (analytics_writer) holds the grants; the service
        // connects as a LOGIN role that is a MEMBER of it and inherits them.
        for (login, group, with_tier) in [
            (&writer, &writer_grp, true),
            (&ungranted, &ungranted_grp, false),
        ] {
            admin_root
                .execute_unprepared(&format!(
                    "CREATE ROLE {group} NOLOGIN; CREATE ROLE {login} LOGIN PASSWORD 'ci-only' IN ROLE {group}"
                ))
                .await
                .expect("role");
            let mut grants = format!(
                "GRANT CONNECT ON DATABASE {db_name} TO {login};
                 GRANT USAGE ON SCHEMA public, analytics_fp, analytics_ops TO {group};
                 GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA analytics_fp TO {group};
                 GRANT SELECT, INSERT, DELETE ON analytics_ops.applied_entries TO {group};
                 GRANT SELECT ON public.data_use_exclusions TO {group};"
            );
            if with_tier {
                grants.push_str(&format!(
                    "GRANT SELECT (community_id, tier_code, released_at) ON public.community_tier_allocations TO {group};"
                ));
            }
            admin.execute_unprepared(&grants).await.expect("grants");
        }
        Self {
            writer_url: with_db(&admin_url(), &db_name, Some((&writer, "ci-only"))),
            ungranted_url: with_db(&admin_url(), &db_name, Some((&ungranted, "ci-only"))),
            owner_url,
            admin,
            db_name,
            admin_root,
        }
    }

    pub async fn store(&self) -> PgStore {
        PgStore {
            db: PgDb::connect(&self.writer_url)
                .await
                .expect("writer connect"),
            session_max_secs: 43_200,
        }
    }

    pub async fn store_with(&self, url: &str, session_max_secs: i64) -> PgStore {
        PgStore {
            db: PgDb::connect(url).await.expect("connect"),
            session_max_secs,
        }
    }

    pub async fn q(&self, sql: &str) -> Vec<sea_orm::QueryResult> {
        self.admin
            .query_all_raw(Statement::from_string(DbBackend::Postgres, sql))
            .await
            .expect("query")
    }

    pub async fn scalar(&self, sql: &str) -> i64 {
        self.q(sql).await[0]
            .try_get_by_index::<i64>(0)
            .expect("scalar")
    }

    pub async fn exec(&self, sql: &str) {
        self.admin.execute_unprepared(sql).await.expect("exec");
    }

    pub async fn premium(&self, community: i32) {
        self.exec(&format!(
            "INSERT INTO public.community_tier_allocations (tenant_id, community_id, tier_code) VALUES (1, {community}, 2)"
        ))
        .await;
    }

    pub async fn exclude(&self, u: Uuid) {
        self.exec(&format!(
            "INSERT INTO public.data_use_exclusions (user_uuid, reason_code) VALUES ('{u}', 4)"
        ))
        .await;
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        // Best-effort cleanup of the throwaway database and roles (runs on a side thread so it
        // works from inside a tokio test).
        let root = self.admin_root.clone();
        let db = self.db_name.clone();
        let tag = db.trim_start_matches("an_").to_string();
        let _ = std::thread::spawn(move || {
            if let Ok(rt) = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
            {
                rt.block_on(async {
                    let _ = root
                        .execute_unprepared(&format!("DROP DATABASE IF EXISTS {db} WITH (FORCE)"))
                        .await;
                    let _ = root
                        .execute_unprepared(&format!("DROP ROLE IF EXISTS an_writer_{tag}"))
                        .await;
                    let _ = root
                        .execute_unprepared(&format!(
                            "DROP ROLE IF EXISTS an_ungranted_{tag}; DROP ROLE IF EXISTS an_wgrp_{tag}; DROP ROLE IF EXISTS an_ugrp_{tag}"
                        ))
                        .await;
                });
            }
        })
        .join();
    }
}

pub fn acc(
    entry_seq: i64,
    community: i32,
    u: Uuid,
    kind: Kind,
    ts_s: i64,
    premium: bool,
) -> Accepted {
    Accepted {
        entry: EntryId {
            ms: ts_s * 1000,
            seq: entry_seq,
        },
        event: Event {
            tenant_id: 1,
            community_id: community,
            user_uuid: u,
            platform_code: 2,
            kind,
            ts_ms: ts_s * 1000,
            provenance_code: 2,
        },
        premium,
    }
}

pub fn test_config(stream: &str) -> Config {
    let s = stream.to_string();
    Config::from_lookup(move |k| match k {
        "VALKEY_URL" => Some(valkey_url()),
        "DATABASE_URL" => Some("postgres://unused".into()),
        "ANALYTICS_STREAM_KEY" => Some(s.clone()),
        "ANALYTICS_DLQ_STREAM" => Some(format!("{s}:dlq")),
        "ANALYTICS_BLOCK_MS" => Some("50".into()),
        "ANALYTICS_CLAIM_IDLE_SECS" => Some("1".into()),
        "ANALYTICS_MAX_ATTEMPTS" => Some("3".into()),
        "ANALYTICS_EXCLUSION_TTL_SECS" => Some("5".into()),
        "ANALYTICS_IDLE_POLL_SECS" => Some("1".into()),
        _ => None,
    })
    .expect("config")
}

pub fn unique_stream() -> String {
    format!(
        "t:{}:{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    )
}

/// Scriptable fake database.
#[derive(Default)]
pub struct FakeDb {
    pub exclusions: Mutex<HashSet<Uuid>>,
    pub premium: Mutex<HashSet<i32>>,
    pub fail_exclusions: Mutex<bool>,
    pub fail_tier: Mutex<bool>,
    /// Users whose events trigger a Data fault (single or batch).
    pub poison_users: Mutex<HashSet<Uuid>>,
    pub transient_apply: Mutex<bool>,
    pub applied: Mutex<Vec<Accepted>>,
    pub seen: Mutex<HashMap<EntryId, ()>>,
    pub exclusion_lookups: Mutex<u32>,
    pub fail_jobs: Mutex<bool>,
    pub purged: Mutex<u32>,
}

impl AnalyticsDb for FakeDb {
    async fn load_exclusions(&self, cap: usize) -> Result<Vec<Uuid>, DbFault> {
        if *self.fail_exclusions.lock().unwrap() {
            return Err(DbFault::transient("down"));
        }
        Ok(self
            .exclusions
            .lock()
            .unwrap()
            .iter()
            .take(cap)
            .copied()
            .collect())
    }
    async fn lookup_exclusions(&self, ids: &[Uuid]) -> Result<Vec<Uuid>, DbFault> {
        *self.exclusion_lookups.lock().unwrap() += 1;
        if *self.fail_exclusions.lock().unwrap() {
            return Err(DbFault::transient("down"));
        }
        let ex = self.exclusions.lock().unwrap();
        Ok(ids.iter().filter(|i| ex.contains(i)).copied().collect())
    }
    async fn premium_communities(&self, ids: &[i32]) -> Result<Vec<i32>, DbFault> {
        if *self.fail_tier.lock().unwrap() {
            return Err(DbFault::transient("42501"));
        }
        let p = self.premium.lock().unwrap();
        Ok(ids.iter().filter(|i| p.contains(i)).copied().collect())
    }
    async fn apply(&self, batch: &[Accepted]) -> Result<ApplyReport, DbFault> {
        if *self.transient_apply.lock().unwrap() {
            return Err(DbFault::transient("08006"));
        }
        let poison = self.poison_users.lock().unwrap();
        if batch.iter().any(|a| poison.contains(&a.event.user_uuid)) {
            return Err(DbFault::data("23514"));
        }
        let mut rep = ApplyReport::default();
        let mut seen = self.seen.lock().unwrap();
        for a in batch {
            if seen.insert(a.entry, ()).is_some() {
                rep.duplicates += 1;
            } else {
                rep.applied += 1;
                self.applied.lock().unwrap().push(a.clone());
            }
        }
        Ok(rep)
    }
    async fn close_stale_sessions(&self, _: i64, _: i64, _: i64) -> Result<u64, DbFault> {
        if *self.fail_jobs.lock().unwrap() {
            return Err(DbFault::transient("down"));
        }
        Ok(0)
    }
    async fn refresh_cohorts(&self, _: &[i32], _: i64) -> Result<u64, DbFault> {
        if *self.fail_jobs.lock().unwrap() {
            return Err(DbFault::transient("down"));
        }
        Ok(3)
    }
    async fn purge(&self, _: i64, _: i64, _: u32) -> Result<PurgeReport, DbFault> {
        if *self.fail_jobs.lock().unwrap() {
            return Err(DbFault::transient("down"));
        }
        *self.purged.lock().unwrap() += 1;
        Ok(PurgeReport {
            tables: vec![("analytics_fp.community_hourly", 4)],
        })
    }
}

/// Shareable handle to a [`FakeDb`] that is itself an [`AnalyticsDb`] (so a test keeps a
/// reference while the consumer owns another).
pub struct Shared(pub std::sync::Arc<FakeDb>);

impl AnalyticsDb for Shared {
    async fn load_exclusions(&self, cap: usize) -> Result<Vec<Uuid>, DbFault> {
        self.0.load_exclusions(cap).await
    }
    async fn lookup_exclusions(&self, ids: &[Uuid]) -> Result<Vec<Uuid>, DbFault> {
        self.0.lookup_exclusions(ids).await
    }
    async fn premium_communities(&self, ids: &[i32]) -> Result<Vec<i32>, DbFault> {
        self.0.premium_communities(ids).await
    }
    async fn apply(&self, b: &[Accepted]) -> Result<ApplyReport, DbFault> {
        self.0.apply(b).await
    }
    async fn close_stale_sessions(&self, a: i64, b: i64, c: i64) -> Result<u64, DbFault> {
        self.0.close_stale_sessions(a, b, c).await
    }
    async fn refresh_cohorts(&self, a: &[i32], b: i64) -> Result<u64, DbFault> {
        self.0.refresh_cohorts(a, b).await
    }
    async fn purge(&self, a: i64, b: i64, c: u32) -> Result<PurgeReport, DbFault> {
        self.0.purge(a, b, c).await
    }
}
