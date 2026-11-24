//! Maintenance jobs: stale-session sweep, cohort refresh and the retention purge.
//! They run even while the consumer flag is OFF (retention must always be honoured).
//! The purge is bounded (rows per batch, batches per table) and only starts inside the
//! configured off-peak UTC window, at most once per UTC day.

use tokio::time::Instant;
use tracing::Instrument;

use crate::config::Config;
use crate::db::AnalyticsDb;
use crate::metrics::Metrics;

/// Scheduler state for the maintenance jobs.
#[derive(Default)]
pub struct Jobs {
    last_maintenance: Option<Instant>,
    last_purge_day: Option<i64>,
}

/// Closes stale sessions (bounded loop); returns sessions closed.
pub async fn run_sweep<D: AnalyticsDb>(db: &D, cfg: &Config, m: &Metrics, now_s: i64) -> u64 {
    let mut total = 0;
    for _ in 0..10 {
        match db
            .close_stale_sessions(now_s, cfg.session_max_secs, 500)
            .await
        {
            Ok(n) => {
                total += n;
                if n < 500 {
                    break;
                }
            }
            Err(e) => {
                Metrics::inc(&m.jobs, 1, "job", "sweep_error");
                tracing::warn!(fault = %e, "stale session sweep failed");
                return total;
            }
        }
    }
    Metrics::inc(&m.sessions, total, "outcome", "swept");
    Metrics::inc(&m.jobs, 1, "job", "sweep_ok");
    total
}

/// Recomputes cohorts for `premium` communities; returns rows upserted.
pub async fn run_cohorts<D: AnalyticsDb>(db: &D, m: &Metrics, premium: &[i32], now_s: i64) -> u64 {
    if premium.is_empty() {
        return 0;
    }
    match db.refresh_cohorts(premium, now_s).await {
        Ok(n) => {
            Metrics::inc(&m.jobs, 1, "job", "cohorts_ok");
            n
        }
        Err(e) => {
            Metrics::inc(&m.jobs, 1, "job", "cohorts_error");
            tracing::warn!(fault = %e, "cohort refresh failed");
            0
        }
    }
}

/// Runs one purge pass; returns total rows deleted.
pub async fn run_purge<D: AnalyticsDb>(db: &D, cfg: &Config, m: &Metrics, now_s: i64) -> u64 {
    match db
        .purge(now_s, cfg.purge_batch, cfg.purge_max_batches)
        .await
    {
        Ok(rep) => {
            let mut total = 0;
            for (table, n) in rep.tables {
                Metrics::inc(&m.purge_rows, n, "table", table);
                total += n;
            }
            Metrics::inc(&m.jobs, 1, "job", "purge_ok");
            tracing::info!(rows = total, "retention purge complete");
            total
        }
        Err(e) => {
            Metrics::inc(&m.jobs, 1, "job", "purge_error");
            tracing::warn!(fault = %e, "retention purge failed");
            0
        }
    }
}

impl Jobs {
    /// Runs whatever is due at `now_s`.
    pub async fn tick<D: AnalyticsDb>(
        &mut self,
        db: &D,
        cfg: &Config,
        m: &Metrics,
        premium: &[i32],
        now_s: i64,
    ) {
        let due = self
            .last_maintenance
            .is_none_or(|t| t.elapsed() >= cfg.maintenance_interval);
        if !due {
            return;
        }
        self.last_maintenance = Some(Instant::now());
        async {
            run_sweep(db, cfg, m, now_s).await;
            run_cohorts(db, m, premium, now_s).await;
            let day = now_s.div_euclid(86_400);
            let hour = now_s.rem_euclid(86_400) / 3600;
            if cfg.in_purge_window(hour) && self.last_purge_day != Some(day) {
                self.last_purge_day = Some(day);
                run_purge(db, cfg, m, now_s).await;
            }
        }
        .instrument(tracing::info_span!("analytics.maintenance"))
        .await;
    }
}
