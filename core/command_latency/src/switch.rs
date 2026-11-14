//! The core-mechanism opt-out kill switch `waddles.disable-command-latency-telemetry`.
//!
//! Convention (`critical-rules.md` Feature Flags): an unseen/OFF flag means the mechanism is ON;
//! ON opts back out. The switch is a process-wide atomic so the hot path pays one relaxed load. It is
//! seeded from the Docker ENV baseline `FLAG_WADDLES_DISABLE_COMMAND_LATENCY_TELEMETRY` and then kept
//! live by [`spawn_refresher`], which each service wires to its own flag resolver (PostHog -> ENV ->
//! default) so flipping the flag takes effect without a restart.

use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

/// The PostHog flag key. ON = telemetry DISABLED (legacy behaviour: no stamps, no metrics, no spans).
pub const FLAG_KEY: &str = "waddles.disable-command-latency-telemetry";
/// The Docker ENV baseline for [`FLAG_KEY`] (`FLAG_` + key uppercased, `.`/`-` -> `_`).
pub const ENV_BASELINE: &str = "FLAG_WADDLES_DISABLE_COMMAND_LATENCY_TELEMETRY";
/// How often a refresher re-resolves the flag.
pub const REFRESH_INTERVAL: Duration = Duration::from_secs(30);

static DISABLED: AtomicBool = AtomicBool::new(false);

/// `true` when the kill switch is ON (telemetry must do nothing).
pub fn is_disabled() -> bool {
    DISABLED.load(Ordering::Relaxed)
}

/// Sets the switch (the refresher and tests; production code never calls this elsewhere).
pub fn set_disabled(disabled: bool) {
    DISABLED.store(disabled, Ordering::Relaxed);
}

/// Parses the ENV baseline: `1`/`true`/`on`/`yes` (any case) = disabled; unset or anything else =
/// enabled (the opt-out default).
pub fn env_baseline_disabled() -> bool {
    std::env::var(ENV_BASELINE).is_ok_and(|raw| {
        matches!(
            raw.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "on" | "yes"
        )
    })
}

/// Seeds the switch from the ENV baseline; call once at startup, before the first request.
pub fn init_from_env() {
    set_disabled(env_baseline_disabled());
}

/// Spawns a task that re-resolves the flag every [`REFRESH_INTERVAL`] via `resolve` (returning
/// `true` when the kill switch flag is ON) and applies it. The task never panics the service and a
/// flag-source failure is `resolve`'s business (it must fall back to the last-known value).
pub fn spawn_refresher<F, Fut>(resolve: F) -> tokio::task::JoinHandle<()>
where
    F: Fn() -> Fut + Send + 'static,
    Fut: Future<Output = bool> + Send + 'static,
{
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(REFRESH_INTERVAL);
        loop {
            tick.tick().await;
            let want = resolve().await;
            if want != is_disabled() {
                tracing::info!(
                    flag = FLAG_KEY,
                    disabled = want,
                    "command-latency telemetry kill switch changed"
                );
                set_disabled(want);
            }
        }
    })
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::sync::Mutex;

    /// The switch and the environment are process-global; serialize the tests that touch them.
    pub(crate) static SWITCH_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn default_is_enabled_and_env_baseline_opts_out() {
        let _g = SWITCH_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        std::env::remove_var(ENV_BASELINE);
        init_from_env();
        assert!(!is_disabled(), "unseen flag leaves the mechanism ON");
        for on in ["1", "true", "ON", " Yes "] {
            std::env::set_var(ENV_BASELINE, on);
            init_from_env();
            assert!(is_disabled(), "{on:?}");
        }
        std::env::set_var(ENV_BASELINE, "0");
        init_from_env();
        assert!(!is_disabled());
        std::env::remove_var(ENV_BASELINE);
    }

    #[tokio::test(start_paused = true)]
    async fn refresher_applies_a_live_flag_flip() {
        let _g = SWITCH_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        set_disabled(false);
        let want = std::sync::Arc::new(AtomicBool::new(true));
        let w = want.clone();
        let h = spawn_refresher(move || {
            let w = w.clone();
            async move { w.load(Ordering::SeqCst) }
        });
        tokio::time::sleep(REFRESH_INTERVAL + Duration::from_secs(1)).await;
        assert!(is_disabled());
        want.store(false, Ordering::SeqCst);
        tokio::time::sleep(REFRESH_INTERVAL).await;
        assert!(!is_disabled());
        h.abort();
    }
}
