//! Direct-database display-name resolver for the egress detokenizer.
//!
//! The control plane (hub-api) writes identity; the data plane reads it. The
//! outbound drain therefore resolves `{user:<uuid>}` placeholders with ONE
//! batched, read-only Postgres query per message under svc-ingest's own
//! column-scoped role (`waddles_svc_ingest`, `config/postgres/service-roles.yaml`)
//! -- no hub-api round trip in the send path.
//!
//! Semantics mirror hub-api's former `ResolveDisplayNames`
//! (`hub_api/services/identity_resolution_service.py`):
//!
//! * **Tenant isolation** -- a hub user resolves only while they are a member
//!   of a community of the entry's MAC-bound tenant; a pseudonym only within
//!   its own tenant. A UUID from another tenant never resolves.
//! * **Erasure** -- erasure deletes the user's `community_members` rows, so an
//!   erased user is no longer a member anywhere and falls through to
//!   [`egress_detokenizer::NEUTRAL_LABEL`] ("Unknown User").
//! * **No caching** -- names live in the returned map for one send only.
//! * **Fail-safe** -- a connect/query failure is a [`DetokenizeError`]; the
//!   caller sends the tokens unchanged. This module logs a `WARN` and bumps
//!   `svc_ingest_egress_display_name_db_errors_total` (no name, token or UUID
//!   is ever logged).

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::LazyLock;
use std::time::Duration;

use egress_detokenizer::{DetokenizeError, DisplayNameResolver};
use sea_orm::{
    ConnectOptions, ConnectionTrait, Database, DatabaseConnection, DbBackend, Statement,
};

/// Most UUIDs one lookup may carry (same bound hub-api enforced).
pub const MAX_BATCH: usize = 100;
/// Longest display name substituted into outbound text (hub-api parity).
pub const MAX_DISPLAY_NAME_LEN: usize = 64;

/// Hub users: name from `hub_users.display_name`, else the newest
/// per-community display name -- only while a member of `$1`'s communities.
const HUB_NAMES_SQL: &str = "\
SELECT hu.uuid::text,
       COALESCE(
           NULLIF(btrim(hu.display_name), ''),
           (SELECT NULLIF(btrim(cm.display_name), '')
              FROM community_members cm JOIN communities c ON c.id = cm.community_id
             WHERE c.tenant_id = $1
               AND (cm.user_uuid = hu.uuid OR cm.user_id = hu.id::text)
               AND NULLIF(btrim(cm.display_name), '') IS NOT NULL
             ORDER BY cm.id DESC LIMIT 1)
       )
  FROM hub_users hu
 WHERE hu.uuid = ANY($2::uuid[])
   AND EXISTS (
        SELECT 1 FROM community_members cm JOIN communities c ON c.id = cm.community_id
         WHERE c.tenant_id = $1 AND (cm.user_uuid = hu.uuid OR cm.user_id = hu.id::text))";

/// Ephemeral pseudonyms (platform users with no hub account), tenant-scoped.
const PSEUDONYM_NAMES_SQL: &str = "\
SELECT ep.pseudonym::text, ep.handle
  FROM ephemeral_pseudonyms ep
 WHERE ep.tenant_id = $1 AND ep.pseudonym = ANY($2::uuid[])";

static DB_ERRORS: LazyLock<prometheus::IntCounter> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounter::new(
        "svc_ingest_egress_display_name_db_errors_total",
        "Egress display-name lookups that failed against Postgres (tokens sent unchanged)",
    )
    .expect("valid metric definition")
});

/// Registers the DB-error counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(DB_ERRORS.clone()))
}

/// Current DB-error count (test/observability helper).
#[must_use]
pub fn db_error_count() -> u64 {
    DB_ERRORS.get()
}

/// [`DisplayNameResolver`] over a lazily-connected, small Postgres pool.
/// Connects on first use and retries the connect on the next send after a
/// failure, so a database outage at startup never disables resolution for
/// the life of the process.
pub struct PgDisplayNameResolver {
    url: String,
    timeout: Duration,
    conn: tokio::sync::Mutex<Option<DatabaseConnection>>,
}

impl PgDisplayNameResolver {
    /// A resolver that has not connected yet. `url` carries the role's
    /// credential and is never logged.
    #[must_use]
    pub fn new(url: String, timeout: Duration) -> Self {
        Self {
            url,
            timeout,
            conn: tokio::sync::Mutex::new(None),
        }
    }

    async fn connection(&self) -> Result<DatabaseConnection, String> {
        let mut slot = self.conn.lock().await;
        if let Some(c) = slot.as_ref() {
            return Ok(c.clone());
        }
        let mut opts = ConnectOptions::new(self.url.clone());
        opts.max_connections(4)
            .min_connections(0)
            .connect_timeout(self.timeout)
            .acquire_timeout(self.timeout)
            .sqlx_logging(false);
        let conn = Database::connect(opts)
            .await
            .map_err(|e| format!("connect: {}", error_kind(&e)))?;
        *slot = Some(conn.clone());
        Ok(conn)
    }

    async fn lookup(
        &self,
        tenant: i64,
        uuids: Vec<String>,
    ) -> Result<HashMap<String, String>, String> {
        let conn = self.connection().await?;
        let mut found: HashMap<String, String> = HashMap::new();
        let hub = tokio::time::timeout(
            self.timeout,
            conn.query_all_raw(Statement::from_sql_and_values(
                DbBackend::Postgres,
                HUB_NAMES_SQL,
                [tenant.into(), uuids.clone().into()],
            )),
        )
        .await
        .map_err(|_| "hub-user query timed out".to_string())?
        .map_err(|e| format!("hub-user query: {}", error_kind(&e)))?;
        for row in hub {
            let uuid: String = row.try_get_by_index(0).map_err(|e| error_kind(&e))?;
            let name: Option<String> = row.try_get_by_index(1).map_err(|e| error_kind(&e))?;
            if let Some(clean) = name
                .as_deref()
                .map(clean_display_name)
                .filter(|n| !n.is_empty())
            {
                found.insert(uuid, clean);
            }
        }
        let pseudo = tokio::time::timeout(
            self.timeout,
            conn.query_all_raw(Statement::from_sql_and_values(
                DbBackend::Postgres,
                PSEUDONYM_NAMES_SQL,
                [tenant.into(), uuids.into()],
            )),
        )
        .await
        .map_err(|_| "pseudonym query timed out".to_string())?
        .map_err(|e| format!("pseudonym query: {}", error_kind(&e)))?;
        for row in pseudo {
            let uuid: String = row.try_get_by_index(0).map_err(|e| error_kind(&e))?;
            let name: Option<String> = row.try_get_by_index(1).map_err(|e| error_kind(&e))?;
            if let Some(clean) = name
                .as_deref()
                .map(clean_display_name)
                .filter(|n| !n.is_empty())
            {
                found.entry(uuid).or_insert(clean);
            }
        }
        Ok(found)
    }
}

/// The error's variant name only: a sea-orm/sqlx error string can echo the
/// connection URL or bound values, so the message itself is never logged.
fn error_kind(e: &sea_orm::DbErr) -> &'static str {
    match e {
        sea_orm::DbErr::Conn(_) => "connection",
        sea_orm::DbErr::ConnectionAcquire(_) => "connection_acquire",
        sea_orm::DbErr::Query(_) => "query",
        sea_orm::DbErr::Exec(_) => "exec",
        sea_orm::DbErr::Type(_) | sea_orm::DbErr::TryIntoErr { .. } => "type",
        _ => "other",
    }
}

impl DisplayNameResolver for PgDisplayNameResolver {
    fn resolve_many<'a>(
        &'a self,
        tenant_id: &'a str,
        tokens: Vec<String>,
    ) -> Pin<Box<dyn Future<Output = Result<HashMap<String, String>, DetokenizeError>> + Send + 'a>>
    {
        Box::pin(async move {
            let tenant = tenant_id
                .parse::<i64>()
                .ok()
                .filter(|t| *t > 0)
                .ok_or_else(|| DetokenizeError::ResolutionUnavailable("tenant id".into()))?;
            // Canonical UUID -> every token spelling that maps to it, so the
            // returned map is keyed exactly as the placeholders were written.
            let mut by_uuid: HashMap<String, Vec<String>> = HashMap::new();
            for token in tokens {
                if let Ok(u) = uuid::Uuid::parse_str(&token) {
                    by_uuid
                        .entry(u.hyphenated().to_string())
                        .or_default()
                        .push(token);
                }
                if by_uuid.len() >= MAX_BATCH {
                    break;
                }
            }
            if by_uuid.is_empty() {
                return Ok(HashMap::new());
            }
            let keys: Vec<String> = by_uuid.keys().cloned().collect();
            match self.lookup(tenant, keys).await {
                Ok(found) => {
                    let mut out = HashMap::new();
                    for (uuid, name) in found {
                        for token in by_uuid.get(&uuid).into_iter().flatten() {
                            out.insert(token.clone(), name.clone());
                        }
                    }
                    Ok(out)
                }
                Err(kind) => {
                    DB_ERRORS.inc();
                    tracing::warn!(
                        error_kind = %kind,
                        tenant_id = tenant,
                        "display-name database lookup failed; sending tokens unchanged"
                    );
                    Err(DetokenizeError::ResolutionUnavailable(kind))
                }
            }
        })
    }
}

/// Makes a stored name safe to substitute into outbound text; `""` if nothing
/// visible remains. Mirrors hub-api's `_clean_display_name`: drops format
/// (bidi, zero-width, tag, BOM, soft hyphen), private-use and blank-rendering
/// filler characters; turns other controls and line separators into spaces;
/// collapses whitespace and caps the length. Per-sink escaping stays the
/// detokenizer's job.
#[must_use]
pub fn clean_display_name(raw: &str) -> String {
    let mapped: String = raw
        .chars()
        .filter(|c| !is_dropped(*c))
        .map(|c| {
            if c.is_control() || c == '\u{2028}' || c == '\u{2029}' {
                ' '
            } else {
                c
            }
        })
        .collect();
    let collapsed = mapped.split_whitespace().collect::<Vec<_>>().join(" ");
    let capped: String = collapsed.chars().take(MAX_DISPLAY_NAME_LEN).collect();
    let cleaned = capped.trim().to_string();
    if cleaned
        .chars()
        .any(|c| !c.is_whitespace() && !is_combining(c))
    {
        cleaned
    } else {
        String::new()
    }
}

/// Unicode format (Cf), private-use (Co) and invisible filler characters.
fn is_dropped(c: char) -> bool {
    matches!(
        c as u32,
        0x00AD
            | 0x034F
            | 0x061C
            | 0x0600..=0x0605
            | 0x06DD
            | 0x070F
            | 0x08E2
            | 0x115F
            | 0x1160
            | 0x17B4
            | 0x17B5
            | 0x180E
            | 0x200B..=0x200F
            | 0x202A..=0x202E
            | 0x2060..=0x2064
            | 0x2066..=0x206F
            | 0x2800
            | 0x3164
            | 0xFEFF
            | 0xFFA0
            | 0xFFF9..=0xFFFB
            | 0x110BD
            | 0x110CD
            | 0x13430..=0x1343F
            | 0x1BCA0..=0x1BCA3
            | 0x1D173..=0x1D17A
            | 0xE0001
            | 0xE0020..=0xE007F
            | 0xE000..=0xF8FF
            | 0xF0000..=0xFFFFD
            | 0x100000..=0x10FFFD
    )
}

/// Combining marks: a name made only of these renders blank.
fn is_combining(c: char) -> bool {
    matches!(
        c as u32,
        0x0300..=0x036F | 0x1AB0..=0x1AFF | 0x1DC0..=0x1DFF | 0x20D0..=0x20FF | 0xFE20..=0xFE2F
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_drops_bidi_and_zero_width_and_collapses_space() {
        assert_eq!(clean_display_name("  a\u{202E}b\u{200B}c \n d  "), "abc d");
    }

    #[test]
    fn clean_rejects_invisible_only_names() {
        assert_eq!(clean_display_name("\u{3164}\u{200B}"), "");
        assert_eq!(clean_display_name("\u{0301}\u{0302}"), "");
        assert_eq!(clean_display_name("   "), "");
    }

    #[test]
    fn clean_caps_length() {
        assert_eq!(
            clean_display_name(&"x".repeat(200)).chars().count(),
            MAX_DISPLAY_NAME_LEN
        );
    }

    #[tokio::test]
    async fn bad_tenant_is_an_error_not_a_lookup() {
        let r = PgDisplayNameResolver::new(
            "postgresql://nobody@127.0.0.1:1/none".into(),
            Duration::from_millis(200),
        );
        assert!(r.resolve_many("0", vec![]).await.is_err());
        assert!(r.resolve_many("acme", vec![]).await.is_err());
    }

    #[tokio::test]
    async fn non_uuid_tokens_never_touch_the_database() {
        let r = PgDisplayNameResolver::new(
            "postgresql://nobody@127.0.0.1:1/none".into(),
            Duration::from_millis(200),
        );
        let out = r.resolve_many("7", vec!["not-a-uuid".into()]).await;
        assert!(out.is_ok_and(|m| m.is_empty()));
    }

    #[tokio::test]
    async fn unreachable_database_is_an_error_and_counted() {
        let before = db_error_count();
        let r = PgDisplayNameResolver::new(
            "postgresql://nobody@127.0.0.1:1/none".into(),
            Duration::from_millis(300),
        );
        let out = r
            .resolve_many("7", vec!["2b9f3c1e-0000-4000-8000-000000000001".into()])
            .await;
        assert!(out.is_err());
        assert!(db_error_count() > before);
    }
}
