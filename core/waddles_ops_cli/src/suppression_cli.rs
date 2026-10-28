//! `suppression` operator tooling (lift / check / purge / stats).
//!
//! Port of `hub_api/cli/suppression.py` + the DB paths of `hub_api/services/ban_suppression.py`.
//! Every output line carries counts and enums only -- never a platform id or a code. The
//! platform user id is read from STDIN (never argv). Exit codes: `0` ok / matched / lifted,
//! `1` nothing matched, `2` invalid arguments, `3` the command could not run (key missing,
//! database unreachable).

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use sqlx::{PgPool, Row};

use crate::db::DbError;
use crate::suppression::{
    lookup_codes, normalize_identity, validate_tenant_id, PlatformKek, SuppressionInputError,
    SuppressionKeyError,
};

pub const EXIT_OK: u8 = 0;
pub const EXIT_NOTHING_MATCHED: u8 = 1;
pub const EXIT_BAD_ARGS: u8 = 2;
pub const EXIT_CANNOT_RUN: u8 = 3;

/// Failure of a suppression command, already classified for the exit code.
#[derive(Debug, thiserror::Error)]
pub enum CmdError {
    #[error("{0}")]
    Input(#[from] SuppressionInputError),
    #[error("suppression key unavailable: {0}")]
    Key(#[from] SuppressionKeyError),
    #[error("{0}")]
    Db(#[from] DbError),
}

impl From<sqlx::Error> for CmdError {
    fn from(e: sqlx::Error) -> Self {
        Self::Db(e.into())
    }
}

impl CmdError {
    /// Python mapping: input -> 2, key/db -> 3.
    pub fn exit_code(&self) -> u8 {
        match self {
            Self::Input(_) => EXIT_BAD_ARGS,
            _ => EXIT_CANNOT_RUN,
        }
    }
}

/// A live restriction found for an identity.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Match {
    pub reason: &'static str,
    pub expires_at: Option<DateTime<Utc>>,
}

/// BAN outranks AUTO_BAN.
fn stronger(a: &'static str, b: &'static str) -> &'static str {
    if a == "ban" || b == "ban" {
        "ban"
    } else {
        a
    }
}

/// The later of two expiries; NULL (until lifted) outranks every date.
fn later(a: Option<DateTime<Utc>>, b: Option<DateTime<Utc>>) -> Option<DateTime<Utc>> {
    match (a, b) {
        (Some(x), Some(y)) => Some(x.max(y)),
        _ => None,
    }
}

fn reason_of(raw: &str) -> Result<&'static str, DbError> {
    match raw {
        "ban" => Ok("ban"),
        "auto_ban" => Ok("auto_ban"),
        _ => Err(DbError::Sql("unknown suppression reason_enum".into())),
    }
}

/// Python `datetime.isoformat()` of an aware UTC value (`+00:00`, micros only when non-zero).
pub fn py_isoformat(t: DateTime<Utc>) -> String {
    let base = t.format("%Y-%m-%dT%H:%M:%S").to_string();
    let micros = t.timestamp_subsec_micros();
    if micros == 0 {
        format!("{base}+00:00")
    } else {
        format!("{base}.{micros:06}+00:00")
    }
}

/// Deletes one identity's row(s) in one tenant (all key versions) in one transaction.
pub async fn lift(
    pool: &PgPool,
    tenant_id: i64,
    platform: &str,
    platform_user_id: &str,
    kek: &dyn Fn() -> Result<PlatformKek, SuppressionKeyError>,
) -> Result<u64, CmdError> {
    let tenant_id = validate_tenant_id(tenant_id)?;
    let (plat, puid) = normalize_identity(platform, platform_user_id)?;
    let tenant_i32 =
        i32::try_from(tenant_id).map_err(|_| SuppressionInputError("tenant_id out of range"))?;
    let mut tx = pool.begin().await?;
    let present: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM suppression_records WHERE tenant_id = $1 AND platform = $2",
    )
    .bind(tenant_i32)
    .bind(&plat)
    .fetch_one(&mut *tx)
    .await?;
    if present == 0 {
        tx.commit().await?;
        return Ok(0);
    }
    // A missing key with rows present raises: a lift must not report success while leaving the
    // row behind. (Dropping `tx` on this early return rolls back.)
    let codes = lookup_codes(&kek()?, tenant_id, &plat, &puid)?;
    let mut deleted = 0u64;
    for code in &codes {
        let res = sqlx::query(
            "DELETE FROM suppression_records \
             WHERE tenant_id = $1 AND platform = $2 AND key_ver = $3 AND hmac = $4",
        )
        .bind(tenant_i32)
        .bind(&plat)
        .bind(&code.key_ver)
        .bind(&code.hmac)
        .execute(&mut *tx)
        .await?;
        deleted += res.rows_affected();
    }
    tx.commit().await?;
    Ok(deleted)
}

/// The non-expired restriction matching this identity in THIS tenant, else `None`.
///
/// Cheap path first: no live row for the platform -> no key is touched.
pub async fn find(
    pool: &PgPool,
    tenant_id: i64,
    platform: &str,
    platform_user_id: &str,
    kek: &dyn Fn() -> Result<PlatformKek, SuppressionKeyError>,
) -> Result<Option<Match>, CmdError> {
    let tenant_id = validate_tenant_id(tenant_id)?;
    let (plat, puid) = normalize_identity(platform, platform_user_id)?;
    let tenant_i32 =
        i32::try_from(tenant_id).map_err(|_| SuppressionInputError("tenant_id out of range"))?;
    let live: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM suppression_records WHERE tenant_id = $1 AND platform = $2 \
         AND (expires_at IS NULL OR expires_at > now())",
    )
    .bind(tenant_i32)
    .bind(&plat)
    .fetch_one(pool)
    .await?;
    if live == 0 {
        return Ok(None);
    }
    let codes = lookup_codes(&kek()?, tenant_id, &plat, &puid)?;
    let hmacs: Vec<String> = codes.iter().map(|c| c.hmac.clone()).collect();
    let rows = sqlx::query(
        "SELECT key_ver, hmac, reason_enum, expires_at FROM suppression_records \
         WHERE tenant_id = $1 AND platform = $2 \
         AND (expires_at IS NULL OR expires_at > now()) AND hmac = ANY($3)",
    )
    .bind(tenant_i32)
    .bind(&plat)
    .bind(&hmacs)
    .fetch_all(pool)
    .await?;
    let mut acc: Option<Match> = None;
    for r in &rows {
        let key_ver: String = r.try_get("key_ver")?;
        let hmac: String = r.try_get("hmac")?;
        if !codes.iter().any(|c| c.key_ver == key_ver && c.hmac == hmac) {
            continue;
        }
        let reason = reason_of(&r.try_get::<String, _>("reason_enum")?)?;
        let expires: Option<DateTime<Utc>> = r.try_get("expires_at")?;
        acc = Some(match acc {
            None => Match {
                reason,
                expires_at: expires,
            },
            Some(m) => Match {
                reason: stronger(m.reason, reason),
                expires_at: later(m.expires_at, expires),
            },
        });
    }
    Ok(acc)
}

/// Deletes every expired row; no key needed.
pub async fn purge(pool: &PgPool) -> Result<u64, CmdError> {
    let res = sqlx::query(
        "DELETE FROM suppression_records WHERE expires_at IS NOT NULL AND expires_at <= now()",
    )
    .execute(pool)
    .await?;
    Ok(res.rows_affected())
}

/// Live/expired counts per `(tenant, reason, key_ver, state)`.
pub async fn stats(pool: &PgPool) -> Result<Vec<String>, CmdError> {
    let rows = sqlx::query(
        "SELECT tenant_id::bigint AS tenant_id, reason_enum, key_ver, expires_at \
         FROM suppression_records WHERE id > 0",
    )
    .fetch_all(pool)
    .await?;
    let now = Utc::now();
    let mut buckets: BTreeMap<(i64, String, String, &'static str), u64> = BTreeMap::new();
    for r in &rows {
        let expires: Option<DateTime<Utc>> = r.try_get("expires_at")?;
        let state = if expires.is_some_and(|e| e <= now) {
            "expired"
        } else {
            "live"
        };
        *buckets
            .entry((
                r.try_get("tenant_id")?,
                r.try_get("reason_enum")?,
                r.try_get("key_ver")?,
                state,
            ))
            .or_insert(0) += 1;
    }
    let mut lines: Vec<String> = buckets
        .iter()
        .map(|((t, reason, ver, state), n)| {
            format!("tenant_id={t} reason={reason} key_ver={ver} state={state} rows={n}")
        })
        .collect();
    lines.push(format!(
        "TOTAL rows={} groups={}",
        buckets.values().sum::<u64>(),
        buckets.len()
    ));
    Ok(lines)
}

/// Which subcommand to run.
pub enum Command {
    Lift { tenant_id: i128, platform: String },
    Check { tenant_id: i128, platform: String },
    Purge,
    Stats,
}

/// Reads the first stdin line, stripping trailing CR/LF (Python `readline().rstrip("\r\n")`).
pub fn read_platform_user_id(mut input: impl std::io::BufRead) -> std::io::Result<String> {
    let mut line = String::new();
    input.read_line(&mut line)?;
    Ok(line.trim_end_matches(['\r', '\n']).to_string())
}

/// CLI body: classifies failures into the documented exit codes. Never prints ids or codes.
pub async fn run(cmd: Command, platform_user_id: String) -> u8 {
    // Validate before touching the database: bad input is a usage error (2) even if the
    // database is down.
    let tenant_for = |t: i128| -> Result<i64, SuppressionInputError> {
        i64::try_from(t)
            .map_err(|_| SuppressionInputError("tenant_id must be a positive integer"))
            .and_then(validate_tenant_id)
    };
    let pre = match &cmd {
        Command::Lift {
            tenant_id,
            platform,
        }
        | Command::Check {
            tenant_id,
            platform,
        } => tenant_for(*tenant_id)
            .and_then(|t| normalize_identity(platform, &platform_user_id).map(|_| t))
            .map(Some),
        _ => Ok(None),
    };
    let tenant = match pre {
        Ok(t) => t,
        Err(e) => {
            eprintln!("error: {e}");
            return EXIT_BAD_ARGS;
        }
    };
    let pool = match crate::db::connect_from_env().await {
        Ok(p) => p,
        Err(e) => {
            eprintln!("error: suppression command could not run: {e}");
            return EXIT_CANNOT_RUN;
        }
    };
    let kek = PlatformKek::from_env;
    let result: Result<u8, CmdError> = match cmd {
        Command::Lift { platform, .. } => {
            let t = tenant.unwrap_or(0);
            lift(&pool, t, &platform, &platform_user_id, &kek)
                .await
                .map(|deleted| {
                    println!("lift tenant_id={t} platform={platform} rows_deleted={deleted}");
                    tracing::info!(tenant_id = t, rows = deleted, "suppression.lifted");
                    if deleted > 0 {
                        EXIT_OK
                    } else {
                        EXIT_NOTHING_MATCHED
                    }
                })
        }
        Command::Check { platform, .. } => {
            let t = tenant.unwrap_or(0);
            find(&pool, t, &platform, &platform_user_id, &kek)
                .await
                .map(|m| match m {
                    None => {
                        println!("check tenant_id={t} platform={platform} matched=false");
                        EXIT_NOTHING_MATCHED
                    }
                    Some(m) => {
                        let expires = m
                            .expires_at
                            .map_or("until_lifted".to_string(), py_isoformat);
                        println!(
                            "check tenant_id={t} platform={platform} matched=true reason={} expires_at={expires}",
                            m.reason
                        );
                        EXIT_OK
                    }
                })
        }
        Command::Purge => purge(&pool).await.map(|n| {
            println!("purge rows_deleted={n}");
            EXIT_OK
        }),
        Command::Stats => stats(&pool).await.map(|lines| {
            for l in lines {
                println!("{l}");
            }
            EXIT_OK
        }),
    };
    match result {
        Ok(code) => code,
        Err(e) => {
            eprintln!("error: {e}");
            e.exit_code()
        }
    }
}
