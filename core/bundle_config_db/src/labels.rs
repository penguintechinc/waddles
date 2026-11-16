//! Read-replica source for the command-latency label registries (`command`, `bundle`,
//! `bundle_lang`): registered command names and registered app bundles with their language.
//!
//! **Replica only.** Unlike the config read path this NEVER falls back to the primary DSN: with
//! `DATABASE_REPLICA_URL` unset the source is absent, an ERROR is logged once and the labels stay
//! `other` / `unknown` (a loud, bounded degradation -- never a silent primary read).
//!
//! Statements run under each stage's own column-scoped role (alembic `0112_latency_label_reads`):
//!
//! * svc-process: `app_commands (command)` -- the registered command names, most widely registered
//!   first, capped at the registry cap (+1 to detect truncation);
//! * svc-action: `app_versions (id, app_id, language)` -- the language of each bundle's newest version.
//!
//! No tenant, community or user column is read and nothing user-typed is stored.

use std::time::Duration;

use command_latency::labels::{BoxFut, LabelSource, Snapshot, Wanted};
use command_latency::registry::{MAX_BUNDLES, MAX_COMMANDS};
use sea_orm::{ConnectionTrait, DbBackend, Statement};

use crate::{error_kind, redact, DbConfigSource, DB_TIMEOUT, REPLICA_URL_ENV};

/// Distinct registered command names, most widely registered first. `count(*)` needs no column
/// privilege beyond `command`. `*` (the wildcard handler) is not a label.
pub const COMMANDS_SQL: &str = "\
SELECT command FROM app_commands WHERE command <> '*' \
GROUP BY command ORDER BY count(*) DESC, command LIMIT $1";

/// One row per bundle: the language of its newest version (`id` is monotonic per insert).
pub const BUNDLES_SQL: &str = "\
SELECT DISTINCT ON (app_id) app_id, language FROM app_versions \
ORDER BY app_id, id DESC LIMIT $1";

/// Starts the label refresher for this stage: seed + periodic + lazy-on-miss from the replica into
/// the process-wide registry. `None` (ERROR already logged) when no replica DSN is configured.
/// Must be called from within a Tokio runtime.
pub fn start(wanted: Wanted) -> Option<tokio::task::JoinHandle<()>> {
    let source = PgLabelSource::from_env()?;
    Some(command_latency::labels::spawn_refresher(
        std::sync::Arc::new(source),
        command_latency::registry::global(),
        wanted,
        command_latency::labels::Timing::default(),
    ))
}

/// [`LabelSource`] over the read replica.
pub struct PgLabelSource {
    db: DbConfigSource,
    url: String,
}

impl PgLabelSource {
    /// A source over `replica_url` (credential-bearing, never logged). Connects lazily.
    #[must_use]
    pub fn new(replica_url: String) -> Self {
        Self {
            db: DbConfigSource::new(replica_url.clone(), DB_TIMEOUT),
            url: replica_url,
        }
    }

    /// Builds the source from `DATABASE_REPLICA_URL` only; `None` (with a one-time ERROR) when it is
    /// unset or blank -- there is deliberately no primary fallback.
    #[must_use]
    pub fn from_env() -> Option<Self> {
        Self::from_value(std::env::var(REPLICA_URL_ENV).ok())
    }

    /// [`Self::from_env`] over an explicit value (tests).
    #[must_use]
    pub fn from_value(replica_url: Option<String>) -> Option<Self> {
        match replica_url.filter(|u| !u.trim().is_empty()) {
            Some(url) => Some(Self::new(url)),
            None => {
                tracing::error!(
                    replica_env = REPLICA_URL_ENV,
                    "command-latency labels: replica DSN unset; the `command` / `bundle` labels \
                     stay `other` (this reader never falls back to the primary database)"
                );
                None
            }
        }
    }

    /// Runs one statement with a single bound limit and returns its rows.
    async fn rows(
        &self,
        sql: &'static str,
        limit: usize,
    ) -> Result<Vec<sea_orm::QueryResult>, String> {
        let conn = self.db.connection().await?;
        let limit = i64::try_from(limit.saturating_add(1)).unwrap_or(i64::MAX);
        tokio::time::timeout(
            Duration::from_secs(5),
            conn.query_all_raw(Statement::from_sql_and_values(
                DbBackend::Postgres,
                sql,
                [limit.into()],
            )),
        )
        .await
        .map_err(|_| "label query timed out".to_string())?
        .map_err(|e| {
            format!(
                "label query: {}: {}",
                error_kind(&e),
                redact(&e.to_string(), &self.url)
            )
        })
    }
}

impl LabelSource for PgLabelSource {
    fn load(&self, wanted: Wanted) -> BoxFut<'_, Result<Snapshot, String>> {
        Box::pin(async move {
            let mut snap = Snapshot::default();
            if wanted.commands {
                let mut names = Vec::new();
                for row in self.rows(COMMANDS_SQL, MAX_COMMANDS).await? {
                    names.push(
                        row.try_get_by_index::<String>(0)
                            .map_err(|e| format!("decode command: {}", error_kind(&e)))?,
                    );
                }
                if names.len() > MAX_COMMANDS {
                    tracing::warn!(
                        cap = MAX_COMMANDS,
                        "command-latency labels: more registered commands than the label cap; the \
                         least widely registered fold into `other`"
                    );
                }
                snap.commands = Some(names);
            }
            if wanted.bundles {
                let mut bundles = Vec::new();
                for row in self.rows(BUNDLES_SQL, MAX_BUNDLES).await? {
                    let app: String = row
                        .try_get_by_index(0)
                        .map_err(|e| format!("decode app_id: {}", error_kind(&e)))?;
                    let lang: String = row
                        .try_get_by_index(1)
                        .map_err(|e| format!("decode language: {}", error_kind(&e)))?;
                    bundles.push((app, lang));
                }
                if bundles.len() > MAX_BUNDLES {
                    tracing::warn!(
                        cap = MAX_BUNDLES,
                        "command-latency labels: more bundles than the label cap; the rest fold \
                         into `other`"
                    );
                }
                snap.bundles = Some(bundles);
            }
            Ok(snap)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blank_or_missing_replica_dsn_yields_no_source_never_the_primary() {
        assert!(PgLabelSource::from_value(None).is_none());
        assert!(PgLabelSource::from_value(Some("  ".into())).is_none());
        assert!(PgLabelSource::from_value(Some("postgresql://r/db".into())).is_some());
    }

    #[test]
    fn statements_read_only_the_granted_columns() {
        // Every column the SQL names must be in the 0112 grant (guarded by the alembic test too).
        assert!(COMMANDS_SQL.contains("command"));
        assert!(!COMMANDS_SQL.contains("tenant_id") && !COMMANDS_SQL.contains("community_id"));
        assert!(BUNDLES_SQL.contains("app_id") && BUNDLES_SQL.contains("language"));
        assert!(!BUNDLES_SQL.contains("artifact_digest"));
    }
}
