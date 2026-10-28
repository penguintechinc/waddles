//! `verify-audit-chain`: recompute the tamper-evident audit chains straight from the database.
//!
//! Port of `hub_api/cli/verify_audit_chain.py` + the read/verify path of
//! `hub_api/services/audit_service.py`. Exit codes (zero examined is a failure, not a pass):
//! `0` all examined chains intact, `1` at least one BROKEN, `2` nothing examined without
//! `--allow-empty` (or bad arguments), `3` the check could not run.

use std::collections::HashMap;
use std::fmt::Write as _;

use serde_json::{Map, Value};
use sqlx::{PgPool, Row};

use crate::audit_chain::{
    ChainRecord, ChainStatus, ChainVerification, ChainVerifier, GENESIS_HASH,
};
use crate::db::DbError;

pub const EXIT_OK: u8 = 0;
pub const EXIT_BROKEN: u8 = 1;
pub const EXIT_NOTHING_EXAMINED: u8 = 2;
pub const EXIT_CANNOT_RUN: u8 = 3;

/// Keyset page size (same as the Python `VERIFY_PAGE_SIZE`).
pub const PAGE_SIZE: i64 = 500;

/// An externally pinned chain head.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pin {
    pub seq: i128,
    pub record_hash: String,
}

/// Parses repeated `--expect CHAIN=SEQ:HASH` (regex
/// `^[A-Za-z0-9:_.\-]{1,64}=[1-9][0-9]*:[0-9a-f]{64}$`, where Python's `$` also admits one
/// trailing newline). Later duplicates override earlier ones.
pub fn parse_pins(values: &[String]) -> Result<HashMap<String, Pin>, String> {
    let mut pins = HashMap::new();
    for value in values {
        let bad = || {
            format!(
                "--expect must look like CHAIN=SEQ:HASH (got {})",
                py_repr_lossy(value)
            )
        };
        let v = value.strip_suffix('\n').unwrap_or(value);
        let (chain, rest) = v.split_once('=').ok_or_else(bad)?;
        let (seq, hash) = rest.split_once(':').ok_or_else(bad)?;
        let chain_ok = (1..=64).contains(&chain.len())
            && chain
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b':' | b'_' | b'.' | b'-'));
        let seq_ok =
            !seq.is_empty() && !seq.starts_with('0') && seq.bytes().all(|b| b.is_ascii_digit());
        let hash_ok = crate::audit_chain::is_hex_digest(hash);
        if !(chain_ok && seq_ok && hash_ok) {
            return Err(bad());
        }
        let seq: i128 = seq.parse().map_err(|_| bad())?;
        pins.insert(
            chain.to_string(),
            Pin {
                seq,
                record_hash: hash.to_string(),
            },
        );
    }
    Ok(pins)
}

fn py_repr_lossy(s: &str) -> String {
    format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
}

/// Canonical UTC timestamp string that is hashed (microsecond precision, `Z` suffix).
pub fn canonical_timestamp(t: chrono::DateTime<chrono::Utc>) -> String {
    t.format("%Y-%m-%dT%H:%M:%S.%6fZ").to_string()
}

/// Python truthiness of a JSON value (`details or {}`).
fn json_falsy(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::Bool(b) => !b,
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
    }
}

fn row_to_record(row: &sqlx::postgres::PgRow) -> Result<ChainRecord, DbError> {
    let details: Value = row.try_get("details")?;
    let details: Map<String, Value> = if json_falsy(&details) {
        Map::new()
    } else {
        match details {
            Value::Object(m) => m,
            _ => {
                return Err(DbError::Sql(
                    "audit_events.details is not a JSON object".into(),
                ))
            }
        }
    };
    let occurred: chrono::DateTime<chrono::Utc> = row.try_get("occurred_at")?;
    Ok(ChainRecord {
        chain_id: row.try_get("chain_id")?,
        seq: row.try_get("seq")?,
        event_id: row.try_get("event_id")?,
        occurred_at: canonical_timestamp(occurred),
        actor_uuid: row.try_get("actor_uuid")?,
        actor_kind: row.try_get("actor_kind")?,
        category: row.try_get("category")?,
        action: row.try_get("action")?,
        outcome: row.try_get("outcome")?,
        target_type: row.try_get("target_type")?,
        target_id: row.try_get("target_id")?,
        details,
        prev_hash: row.try_get("prev_hash")?,
        record_hash: row.try_get("record_hash")?,
        hash_version: row.try_get("hash_version")?,
    })
}

/// Every chain with at least one record, sorted.
pub async fn chain_ids(pool: &PgPool) -> Result<Vec<String>, DbError> {
    let mut ids: Vec<String> = sqlx::query_scalar("SELECT DISTINCT chain_id FROM audit_events")
        .fetch_all(pool)
        .await?;
    ids.sort();
    Ok(ids)
}

/// Records with `seq > after_seq`, ascending (keyset page).
pub async fn read_range(
    pool: &PgPool,
    chain_id: &str,
    after_seq: i64,
    limit: i64,
) -> Result<Vec<ChainRecord>, DbError> {
    let rows = sqlx::query(
        "SELECT chain_id, seq, event_id::text AS event_id, occurred_at, \
         actor_uuid::text AS actor_uuid, actor_kind, category, action, outcome, target_type, \
         target_id, details, prev_hash, record_hash, hash_version \
         FROM audit_events WHERE chain_id = $1 AND seq > $2 ORDER BY seq ASC LIMIT $3",
    )
    .bind(chain_id)
    .bind(after_seq)
    .bind(limit)
    .fetch_all(pool)
    .await?;
    rows.iter().map(row_to_record).collect()
}

/// Verification of one chain plus whether the end was reached.
pub struct Report {
    pub chain_id: String,
    pub verification: ChainVerification,
    pub complete: bool,
}

/// Recomputes one chain from genesis; pinned-head checks apply once the end is reached.
pub async fn verify_chain(
    pool: &PgPool,
    chain_id: &str,
    pin: Option<&Pin>,
) -> Result<Report, DbError> {
    let mut verifier =
        ChainVerifier::new(chain_id, 1, GENESIS_HASH).map_err(|e| DbError::Sql(e.to_string()))?;
    let mut after = 0i64;
    let mut complete = false;
    while verifier.broken.is_none() {
        let page = read_range(pool, chain_id, after, PAGE_SIZE).await?;
        let Some(last) = page.last() else {
            complete = true;
            break;
        };
        after = last.seq;
        for record in &page {
            if !verifier.feed(record) {
                break;
            }
        }
    }
    let result = verifier.finish(
        pin.filter(|_| complete).map(|p| p.seq),
        pin.filter(|_| complete).map(|p| p.record_hash.as_str()),
    );
    if result.status == ChainStatus::Broken {
        if let Some(b) = &result.break_ {
            tracing::error!(chain = chain_id, first_bad_seq = ?b.seq, reason = b.reason.as_str(),
                examined = result.examined, "AUDIT CHAIN TAMPER DETECTED");
        }
    }
    Ok(Report {
        chain_id: chain_id.to_string(),
        verification: result,
        complete,
    })
}

/// One human/grep-friendly line per chain (ids, counts, hashes only).
pub fn report_line(r: &Report) -> String {
    let v = &r.verification;
    let mut s = format!(
        "chain={} status={} examined={} complete={}",
        r.chain_id,
        v.status.as_str(),
        v.examined,
        r.complete
    );
    if v.status == ChainStatus::Broken {
        if let Some(b) = &v.break_ {
            let seq = b.seq.map_or("None".to_string(), |x| x.to_string());
            let _ = write!(s, " first_bad_seq={seq} reason={}", b.reason.as_str());
            return s;
        }
    }
    if let (Some(seq), Some(hash)) = (v.head_seq, &v.head_hash) {
        let _ = write!(s, " head_seq={seq} head_hash={hash}");
    }
    s
}

/// Totals for one run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RunSummary {
    pub chains: usize,
    pub intact: usize,
    pub broken: usize,
    pub empty: usize,
    pub records: u64,
}

impl RunSummary {
    /// Maps totals to the documented exit code.
    pub fn exit_code(&self, allow_empty: bool) -> u8 {
        if self.broken > 0 {
            EXIT_BROKEN
        } else if self.intact == 0 && !allow_empty {
            EXIT_NOTHING_EXAMINED
        } else {
            EXIT_OK
        }
    }
}

/// Verifies the requested (or every) chain; returns report lines + totals.
pub async fn verify_chains(
    pool: &PgPool,
    chains: &[String],
    pins: &HashMap<String, Pin>,
) -> Result<(Vec<String>, RunSummary), DbError> {
    let targets = if chains.is_empty() {
        chain_ids(pool).await?
    } else {
        chains.to_vec()
    };
    let mut lines = Vec::new();
    let (mut intact, mut broken, mut empty, mut records) = (0, 0, 0, 0u64);
    for chain in &targets {
        let report = verify_chain(pool, chain, pins.get(chain)).await?;
        lines.push(report_line(&report));
        records += report.verification.examined;
        match report.verification.status {
            ChainStatus::Intact => intact += 1,
            ChainStatus::Broken => broken += 1,
            ChainStatus::Empty => empty += 1,
        }
    }
    Ok((
        lines,
        RunSummary {
            chains: targets.len(),
            intact,
            broken,
            empty,
            records,
        },
    ))
}

/// CLI body: returns the process exit code and writes the Python-identical output.
pub async fn run(chains: Vec<String>, expect: Vec<String>, allow_empty: bool) -> u8 {
    let pins = match parse_pins(&expect) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("error: {e}");
            return EXIT_NOTHING_EXAMINED;
        }
    };
    let pool = match crate::db::connect_from_env().await {
        Ok(p) => p,
        Err(e) => {
            eprintln!("error: audit verification could not run: {e}");
            return EXIT_CANNOT_RUN;
        }
    };
    let (lines, summary) = match verify_chains(&pool, &chains, &pins).await {
        Ok(x) => x,
        Err(e) => {
            eprintln!("error: audit verification could not run: {e}");
            return EXIT_CANNOT_RUN;
        }
    };
    for l in &lines {
        println!("{l}");
    }
    println!(
        "TOTAL chains={} intact={} broken={} empty={} records_examined={}",
        summary.chains, summary.intact, summary.broken, summary.empty, summary.records
    );
    let code = summary.exit_code(allow_empty);
    if code == EXIT_NOTHING_EXAMINED {
        eprintln!("error: no audit records were examined; a zero-denominator check fails");
    }
    code
}
