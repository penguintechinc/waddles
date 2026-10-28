//! Live-Postgres integration for the three DB-backed command groups.
//!
//! `#[ignore]`d by default (needs a database); run with
//! `WADDLES_OPS_TEST_DATABASE_URL=postgres://... cargo test --test db_integration -- --ignored`.
//! Python-produced vectors are inserted as DB rows, so this proves the *reader* side too:
//! JSONB round trip + `timestamptz` -> canonical timestamp for the audit chain, and Python
//! codes matching through `find`/`lift`. Everything in one test (process env is global).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;

use object_store::{path::Path, ObjectStoreExt};
use serde::Deserialize;
use serde_json::Value;
use sqlx::postgres::PgPoolOptions;
use waddles_ops_cli::{audit_cli, sign_backfill, suppression, suppression_cli};

#[derive(Deserialize)]
struct AuditV {
    chains: Vec<Chain>,
}
#[derive(Deserialize)]
struct Chain {
    name: String,
    records: Vec<Value>,
}
#[derive(Deserialize)]
struct SignV {
    seed_b64: String,
    app_id: String,
    version: String,
    digest: String,
    approval_id: i64,
    sidecar: String,
}
#[derive(Deserialize)]
struct SuppV {
    kek_hex: String,
    kek_previous_hex: String,
    codes: Vec<SuppCase>,
}
#[derive(Deserialize)]
struct SuppCase {
    tenant_id: i64,
    platform: String,
    platform_user_id: String,
    codes: Vec<SuppCode>,
}
#[derive(Deserialize)]
struct SuppCode {
    key_ver: String,
    hmac: String,
}

const SCHEMA: &str = "
DROP TABLE IF EXISTS audit_events, suppression_records, app_versions, app_install_approvals CASCADE;
CREATE TABLE audit_events (
  chain_id VARCHAR(64) NOT NULL, seq BIGINT NOT NULL CHECK (seq >= 1), event_id UUID NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL, actor_uuid UUID, actor_kind VARCHAR(16) NOT NULL,
  category VARCHAR(16) NOT NULL, action VARCHAR(100) NOT NULL, outcome VARCHAR(16) NOT NULL,
  target_type VARCHAR(50), target_id VARCHAR(128), details JSONB NOT NULL DEFAULT '{}'::jsonb,
  prev_hash VARCHAR(64) NOT NULL, record_hash VARCHAR(64) NOT NULL,
  hash_version VARCHAR(16) NOT NULL DEFAULT 'sha256-v1', PRIMARY KEY (chain_id, seq));
CREATE TABLE suppression_records (
  id BIGSERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL, platform VARCHAR(50) NOT NULL,
  key_ver VARCHAR(32) NOT NULL, hmac VARCHAR(64) NOT NULL, reason_enum VARCHAR(16) NOT NULL,
  expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, platform, key_ver, hmac));
CREATE TABLE app_install_approvals (
  id BIGINT PRIMARY KEY, app_id TEXT NOT NULL, version TEXT NOT NULL, superseded_by BIGINT);
CREATE TABLE app_versions (
  id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, version TEXT NOT NULL, artifact_digest TEXT,
  artifact_signature TEXT, artifact_signature_key_id VARCHAR(100),
  artifact_signed_approval_id BIGINT REFERENCES app_install_approvals(id),
  artifact_signed_at TIMESTAMPTZ);
";

fn s(v: &Value, k: &str) -> String {
    v[k].as_str().unwrap().to_string()
}

#[tokio::test]
#[ignore = "needs WADDLES_OPS_TEST_DATABASE_URL (live Postgres)"]
async fn db_flows_match_python() {
    let url =
        std::env::var("WADDLES_OPS_TEST_DATABASE_URL").expect("set WADDLES_OPS_TEST_DATABASE_URL");
    let pool = PgPoolOptions::new()
        .max_connections(2)
        .connect(&url)
        .await
        .unwrap();
    sqlx::raw_sql(SCHEMA).execute(&pool).await.unwrap();

    audit_section(&pool).await;
    suppression_section(&pool).await;
    signing_section(&pool).await;
    cli_entrypoints_section(&url, &pool).await;
}

/// The `run()` entry points (what the binary calls): exit codes + env wiring against the DB.
async fn cli_entrypoints_section(url: &str, pool: &sqlx::PgPool) {
    use suppression_cli::Command;
    std::env::set_var("DATABASE_URL", url);
    // audit_events is empty here: zero examined = 2, --allow-empty = 0.
    assert_eq!(audit_cli::run(vec![], vec![], false).await, 2);
    assert_eq!(audit_cli::run(vec![], vec![], true).await, 0);
    assert_eq!(suppression_cli::run(Command::Stats, String::new()).await, 0);
    assert_eq!(suppression_cli::run(Command::Purge, String::new()).await, 0);
    // No rows + no key: lift/check report "nothing matched" (1), never a key error.
    std::env::remove_var(suppression::ENV_CURRENT);
    let lift = || Command::Lift {
        tenant_id: 1,
        platform: "discord".into(),
    };
    let check = || Command::Check {
        tenant_id: 1,
        platform: "discord".into(),
    };
    assert_eq!(suppression_cli::run(lift(), "123".into()).await, 1);
    assert_eq!(suppression_cli::run(check(), "123".into()).await, 1);
    // A live row + missing key => 3; with the key => matched (0) / lifted (0).
    let v: SuppV = serde_json::from_str(include_str!("suppression_vectors.json")).unwrap();
    let c0 = &v.codes[0];
    sqlx::query("INSERT INTO suppression_records (tenant_id, platform, key_ver, hmac, reason_enum) VALUES (1,'discord',$1,$2,'ban')")
        .bind(&c0.codes[0].key_ver).bind(&c0.codes[0].hmac).execute(pool).await.unwrap();
    assert_eq!(
        suppression_cli::run(check(), c0.platform_user_id.clone()).await,
        3
    );
    kek_env(&v);
    assert_eq!(
        suppression_cli::run(check(), c0.platform_user_id.clone()).await,
        0
    );
    assert_eq!(
        suppression_cli::run(lift(), c0.platform_user_id.clone()).await,
        0
    );
    assert_eq!(
        suppression_cli::run(lift(), c0.platform_user_id.clone()).await,
        1
    );
    // audit tamper through run(): a broken chain exits 1.
    let a: AuditV = serde_json::from_str(include_str!("audit_vectors.json")).unwrap();
    let intact = a
        .chains
        .iter()
        .find(|c| c.name == "tampered_field")
        .unwrap();
    for r in &intact.records {
        insert_audit(pool, r).await;
    }
    assert_eq!(audit_cli::run(vec![], vec![], false).await, 1);
}

async fn insert_audit(pool: &sqlx::PgPool, r: &Value) {
    sqlx::query(
        "INSERT INTO audit_events (chain_id, seq, event_id, occurred_at, actor_uuid, actor_kind, \
         category, action, outcome, target_type, target_id, details, prev_hash, record_hash, hash_version) \
         VALUES ($1,$2,$3::uuid,$4::timestamptz,$5::uuid,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15)",
    )
    .bind(s(r, "chain_id"))
    .bind(r["seq"].as_i64().unwrap())
    .bind(s(r, "event_id"))
    .bind(s(r, "occurred_at"))
    .bind(r["actor_uuid"].as_str().map(str::to_string))
    .bind(s(r, "actor_kind"))
    .bind(s(r, "category"))
    .bind(s(r, "action"))
    .bind(s(r, "outcome"))
    .bind(r["target_type"].as_str().map(str::to_string))
    .bind(r["target_id"].as_str().map(str::to_string))
    .bind(r["details"].to_string())
    .bind(s(r, "prev_hash"))
    .bind(s(r, "record_hash"))
    .bind(s(r, "hash_version"))
    .execute(pool)
    .await
    .unwrap();
}

async fn audit_section(pool: &sqlx::PgPool) {
    let v: AuditV = serde_json::from_str(include_str!("audit_vectors.json")).unwrap();
    let intact = v.chains.iter().find(|c| c.name == "intact").unwrap();
    assert_eq!(intact.records.len(), 5);
    for r in &intact.records {
        insert_audit(pool, r).await;
    }
    let h = intact.records[4]["record_hash"]
        .as_str()
        .unwrap()
        .to_string();

    // Python-sealed chain read back through JSONB + timestamptz must verify intact.
    let pins = audit_cli::parse_pins(&[format!("tenant:7=5:{h}")]).unwrap();
    let (lines, sum) = audit_cli::verify_chains(pool, &[], &pins).await.unwrap();
    assert_eq!(sum.chains, 1);
    assert_eq!((sum.intact, sum.broken, sum.records), (1, 0, 5));
    assert_eq!(sum.exit_code(false), 0);
    assert_eq!(
        lines[0],
        format!("chain=tenant:7 status=intact examined=5 complete=true head_seq=5 head_hash={h}")
    );

    // A wrong pin is detected (truncated-tail defence).
    let bad = audit_cli::parse_pins(&[format!("tenant:7=4:{h}")]).unwrap();
    let (_, sum) = audit_cli::verify_chains(pool, &[], &bad).await.unwrap();
    assert_eq!((sum.broken, sum.exit_code(true)), (1, 1));

    // Tamper with a stored field: HASH_MISMATCH at seq 3, exit 1.
    sqlx::query("UPDATE audit_events SET outcome = 'failure' WHERE chain_id='tenant:7' AND seq=3")
        .execute(pool)
        .await
        .unwrap();
    let (lines, sum) = audit_cli::verify_chains(pool, &[], &HashMap::new())
        .await
        .unwrap();
    assert_eq!(
        lines[0],
        "chain=tenant:7 status=broken examined=3 complete=false first_bad_seq=3 reason=hash_mismatch"
    );
    assert_eq!(sum.exit_code(false), 1);

    // No chains at all: zero examined fails (2) unless --allow-empty.
    sqlx::query("DELETE FROM audit_events")
        .execute(pool)
        .await
        .unwrap();
    let (_, sum) = audit_cli::verify_chains(pool, &[], &HashMap::new())
        .await
        .unwrap();
    assert_eq!(
        (sum.chains, sum.exit_code(false), sum.exit_code(true)),
        (0, 2, 0)
    );
}

fn kek_env(v: &SuppV) {
    std::env::set_var(suppression::ENV_CURRENT, &v.kek_hex);
    std::env::set_var(suppression::ENV_PREVIOUS, &v.kek_previous_hex);
}

async fn suppression_section(pool: &sqlx::PgPool) {
    let v: SuppV = serde_json::from_str(include_str!("suppression_vectors.json")).unwrap();
    kek_env(&v);
    let kek = suppression::PlatformKek::from_env;
    let c0 = &v.codes[0]; // tenant 1 discord, current + previous key codes
    let (t, p, id) = (
        c0.tenant_id,
        c0.platform.as_str(),
        c0.platform_user_id.as_str(),
    );
    sqlx::query(
        "INSERT INTO suppression_records (tenant_id, platform, key_ver, hmac, reason_enum, expires_at) VALUES ($1,$2,$3,$4,'auto_ban',NULL)",
    )
    .bind(t as i32).bind("discord").bind(&c0.codes[0].key_ver).bind(&c0.codes[0].hmac)
    .execute(pool).await.unwrap();
    sqlx::query(
        "INSERT INTO suppression_records (tenant_id, platform, key_ver, hmac, reason_enum, expires_at) VALUES ($1,$2,$3,$4,'ban', now() + interval '1 day')",
    )
    .bind(t as i32).bind("discord").bind(&c0.codes[1].key_ver).bind(&c0.codes[1].hmac)
    .execute(pool).await.unwrap();
    // An expired row for another identity, to exercise purge/stats.
    sqlx::query(
        "INSERT INTO suppression_records (tenant_id, platform, key_ver, hmac, reason_enum, expires_at) VALUES (9,'kick','k',$1,'ban', now() - interval '1 day')",
    )
    .bind("0".repeat(64)).execute(pool).await.unwrap();

    // Python-computed codes match; BAN outranks AUTO_BAN; later expiry / NULL rules apply.
    let m = suppression_cli::find(pool, t, p, id, &kek)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(m.reason, "ban");
    assert!(
        m.expires_at.is_none(),
        "NULL (until lifted) outranks any date"
    );
    // Padded / mixed-case platform + id normalise to the same identity.
    let m2 = suppression_cli::find(pool, t, " Discord ", &format!("  {id}\t"), &kek)
        .await
        .unwrap();
    assert!(m2.is_some());
    // Other tenant / other id: no match.
    assert!(suppression_cli::find(pool, 2, p, id, &kek)
        .await
        .unwrap()
        .is_none());
    assert!(suppression_cli::find(pool, t, p, "999", &kek)
        .await
        .unwrap()
        .is_none());

    // Missing key + live rows => key error (exit 3), never a silent no-match.
    let nokey = || suppression::PlatformKek::from_values(None, None);
    let err = suppression_cli::find(pool, t, p, id, &nokey)
        .await
        .err()
        .unwrap();
    assert_eq!(err.exit_code(), 3);
    // ...but a tenant/platform with no rows needs no key at all (Python cheap path).
    assert!(suppression_cli::find(pool, 77, "twitch", "x", &nokey)
        .await
        .unwrap()
        .is_none());

    // Stats counts + purge.
    let lines = suppression_cli::stats(pool).await.unwrap();
    assert!(
        lines.last().unwrap().starts_with("TOTAL rows=3 groups="),
        "{lines:?}"
    );
    assert!(lines.iter().any(|l| l.contains("state=expired rows=1")));
    assert_eq!(suppression_cli::purge(pool).await.unwrap(), 1);

    // Lift deletes BOTH key versions in one transaction; a repeat matches nothing (exit 1).
    assert_eq!(
        suppression_cli::lift(pool, t, p, id, &kek).await.unwrap(),
        2
    );
    assert_eq!(
        suppression_cli::lift(pool, t, p, id, &kek).await.unwrap(),
        0
    );
    let left: i64 = sqlx::query_scalar("SELECT count(*) FROM suppression_records")
        .fetch_one(pool)
        .await
        .unwrap();
    assert_eq!(left, 0);
}

async fn signing_section(pool: &sqlx::PgPool) {
    let v: Vec<SignV> = serde_json::from_str(include_str!("python_vectors.json")).unwrap();
    let v0 = &v[0]; // core-ping 1.0.0 approval 42
    std::env::set_var("BUNDLE_SIGNING_PRIVATE_KEY", &v0.seed_b64);
    std::env::set_var("BUNDLE_SIGNING_KEY_ID", "k1");

    // approvals: 42 + 50 current for core-ping, 60 superseded-only for 'old', 70 for 'nodigest',
    // 80 for 'signed' (already signed).
    for (id, app, ver, sup) in [
        (42i64, "core-ping", "1.0.0", None),
        (50, "core-ping", "1.0.0", None),
        (60, "old", "1.0.0", Some(42i64)),
        (70, "nodigest", "1.0.0", None),
        (80, "signed", "1.0.0", None),
    ] {
        sqlx::query("INSERT INTO app_install_approvals VALUES ($1,$2,$3,$4)")
            .bind(id)
            .bind(app)
            .bind(ver)
            .bind(sup)
            .execute(pool)
            .await
            .unwrap();
    }
    for (id, app, ver, dig, sig) in [
        (1, "core-ping", "1.0.0", Some(v0.digest.as_str()), None),
        (2, "old", "1.0.0", Some("sha256:aa"), None),
        (3, "nodigest", "1.0.0", None, None),
        (4, "signed", "1.0.0", Some("sha256:bb"), Some("already")),
    ] {
        sqlx::query("INSERT INTO app_versions (id, app_id, version, artifact_digest, artifact_signature) VALUES ($1,$2,$3,$4,$5)")
            .bind(id).bind(app).bind(ver).bind(dig).bind(sig).execute(pool).await.unwrap();
    }

    // Selection: exactly ONE row for core-ping (MIN approval = 42) -- the duplicate-approval
    // regression from the Python CLI.
    let rows = sign_backfill::select_unsigned_approved_versions(pool)
        .await
        .unwrap();
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!((rows[0].version_id, rows[0].approval_id), (1, 42));

    // Missing key: the row FAILS, the transaction rolls back (no partial signature), exit 1.
    std::env::remove_var("BUNDLE_SIGNING_PRIVATE_KEY");
    let store = object_store::memory::InMemory::new();
    let sum = sign_backfill::run(pool, &store).await.unwrap();
    assert_eq!(
        (sum.examined, sum.signed, sum.failures, sum.exit_code()),
        (1, 0, 1, 1)
    );
    let sig: Option<String> =
        sqlx::query_scalar("SELECT artifact_signature FROM app_versions WHERE id=1")
            .fetch_one(pool)
            .await
            .unwrap();
    assert!(sig.is_none(), "failed row must not be left half-signed");

    // With the key: signed; DB signature == Python's, sidecar bytes == Python's, idempotent re-run.
    std::env::set_var("BUNDLE_SIGNING_PRIVATE_KEY", &v0.seed_b64);
    let sum = sign_backfill::run(pool, &store).await.unwrap();
    assert_eq!(
        (sum.examined, sum.signed, sum.failures, sum.exit_code()),
        (1, 1, 0, 0)
    );
    let (sig, kid, appr): (String, String, i64) = sqlx::query_as(
        "SELECT artifact_signature, artifact_signature_key_id, artifact_signed_approval_id FROM app_versions WHERE id=1",
    )
    .fetch_one(pool).await.unwrap();
    let py: Value = serde_json::from_str(&v0.sidecar).unwrap();
    assert_eq!(
        (sig.as_str(), kid.as_str(), appr),
        (py["signature"].as_str().unwrap(), "k1", v0.approval_id)
    );
    let key = format!(
        "bundles/{}/{}/{}.json",
        v0.app_id,
        v0.version,
        v0.digest.strip_prefix("sha256:").unwrap()
    );
    let body = store
        .get(&Path::parse(&key).unwrap())
        .await
        .unwrap()
        .bytes()
        .await
        .unwrap();
    assert_eq!(body.as_ref(), v0.sidecar.as_bytes());
    let again = sign_backfill::run(pool, &store).await.unwrap();
    assert_eq!(
        (again.examined, again.exit_code()),
        (0, 0),
        "second run is a true no-op"
    );
}
