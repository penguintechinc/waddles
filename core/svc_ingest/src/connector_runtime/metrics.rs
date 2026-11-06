//! Bounded-label Prometheus series for the connector runtime. Labels are
//! `platform` and fixed outcome enums only -- NEVER community/connection ids
//! (unbounded cardinality, and ids tie series to individual tenants).

use prometheus::{IntCounterVec, IntGaugeVec, Opts, Registry};

/// Why a reconcile pass ended the way it did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReconcileOutcome {
    /// The feed answered and the diff was applied.
    Ok,
    /// The feed returned an error; the current set was kept.
    FeedError,
    /// The feed did not answer within the bound; the current set was kept.
    FeedTimeout,
}

impl ReconcileOutcome {
    /// Stable label value.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::FeedError => "feed_error",
            Self::FeedTimeout => "feed_timeout",
        }
    }
}

/// Why a connection was (re)started.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RestartReason {
    /// The derived config (or secret version) changed.
    Fingerprint,
    /// The connection task panicked.
    Panic,
    /// The sink for the connection could not be built.
    SinkBuild,
    /// The connection task ended without being asked to.
    Ended,
}

impl RestartReason {
    /// Stable label value.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Fingerprint => "fingerprint",
            Self::Panic => "panic",
            Self::SinkBuild => "sink_build",
            Self::Ended => "ended",
        }
    }
}

/// Why a connection was parked (not restarted until its fingerprint changes).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ParkReason {
    /// The transport reported a terminal failure (needs re-auth).
    Fatal,
    /// The driver refused the configuration.
    DriverError,
    /// The transport could not be built from the plan.
    TransportBuild,
    /// A webhook plan arrived but no webhook binder is wired.
    WebhookUnavailable,
}

impl ParkReason {
    /// Stable label value.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Fatal => "fatal",
            Self::DriverError => "driver_error",
            Self::TransportBuild => "transport_build",
            Self::WebhookUnavailable => "webhook_unavailable",
        }
    }
}

/// What the per-connection lease did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LeaseOutcome {
    /// Taken.
    Acquired,
    /// Held by another replica.
    Denied,
    /// The lease store failed (fail-closed: not acquired).
    Error,
    /// Lost (or unrenewable) while running; the connection was stopped.
    Lost,
}

impl LeaseOutcome {
    /// Stable label value.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Acquired => "acquired",
            Self::Denied => "denied",
            Self::Error => "error",
            Self::Lost => "lost",
        }
    }
}

/// Why a binding or desired connection was dropped from the feed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkipReason {
    /// The profile rejected the binding.
    Malformed,
    /// Same-community duplicate resource; lowest connection id kept.
    Duplicate,
    /// Resource claimed by several communities; all claimants dropped.
    Conflict,
}

impl SkipReason {
    /// Stable label value.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Malformed => "malformed",
            Self::Duplicate => "duplicate",
            Self::Conflict => "conflict",
        }
    }
}

/// The runtime's instruments.
#[derive(Clone)]
pub struct RuntimeMetrics {
    /// `connector_runtime_desired{platform}`.
    pub desired: IntGaugeVec,
    /// `connector_runtime_live{platform}`.
    pub live: IntGaugeVec,
    /// `connector_runtime_reconcile_total{platform,outcome}`.
    pub reconcile_total: IntCounterVec,
    /// `connector_runtime_restarts_total{platform,reason}`.
    pub restarts_total: IntCounterVec,
    /// `connector_runtime_capped_total{platform}`.
    pub capped_total: IntCounterVec,
    /// `connector_runtime_parked_total{platform,reason}`.
    pub parked_total: IntCounterVec,
    /// `connector_runtime_lease_total{platform,outcome}`.
    pub lease_total: IntCounterVec,
    /// `connector_runtime_feed_skipped_total{platform,reason}`.
    pub feed_skipped_total: IntCounterVec,
}

impl RuntimeMetrics {
    /// Registers every series against `registry`; a duplicate registration is
    /// a typed error.
    pub fn register(registry: &Registry) -> Result<Self, prometheus::Error> {
        let gauge = |name: &str, help: &str| IntGaugeVec::new(Opts::new(name, help), &["platform"]);
        let counter = |name: &str, help: &str, labels: &[&str]| {
            IntCounterVec::new(Opts::new(name, help), labels)
        };
        let m = Self {
            desired: gauge(
                "connector_runtime_desired",
                "Connections in the last successful desired set",
            )?,
            live: gauge(
                "connector_runtime_live",
                "Supervised connections currently established",
            )?,
            reconcile_total: counter(
                "connector_runtime_reconcile_total",
                "Reconcile passes by outcome",
                &["platform", "outcome"],
            )?,
            restarts_total: counter(
                "connector_runtime_restarts_total",
                "Connection (re)starts by reason",
                &["platform", "reason"],
            )?,
            capped_total: counter(
                "connector_runtime_capped_total",
                "Desired connections skipped by the per-replica cap",
                &["platform"],
            )?,
            parked_total: counter(
                "connector_runtime_parked_total",
                "Connections parked until their fingerprint changes",
                &["platform", "reason"],
            )?,
            lease_total: counter(
                "connector_runtime_lease_total",
                "Per-connection lease operations by outcome",
                &["platform", "outcome"],
            )?,
            feed_skipped_total: counter(
                "connector_runtime_feed_skipped_total",
                "Bindings dropped by the feed",
                &["platform", "reason"],
            )?,
        };
        registry.register(Box::new(m.desired.clone()))?;
        registry.register(Box::new(m.live.clone()))?;
        registry.register(Box::new(m.reconcile_total.clone()))?;
        registry.register(Box::new(m.restarts_total.clone()))?;
        registry.register(Box::new(m.capped_total.clone()))?;
        registry.register(Box::new(m.parked_total.clone()))?;
        registry.register(Box::new(m.lease_total.clone()))?;
        registry.register(Box::new(m.feed_skipped_total.clone()))?;
        Ok(m)
    }

    /// Materializes the gauges at 0 for `platform` so the series exist before
    /// the first reconcile (rollout step 2: "series present at 0").
    pub fn init_platform(&self, platform: &str) {
        self.desired.with_label_values(&[platform]).set(0);
        self.live.with_label_values(&[platform]).set(0);
    }

    pub(crate) fn reconcile(&self, platform: &str, o: ReconcileOutcome) {
        self.reconcile_total
            .with_label_values(&[platform, o.as_str()])
            .inc();
    }

    pub(crate) fn restart(&self, platform: &str, r: RestartReason) {
        self.restarts_total
            .with_label_values(&[platform, r.as_str()])
            .inc();
    }

    pub(crate) fn parked(&self, platform: &str, r: ParkReason) {
        self.parked_total
            .with_label_values(&[platform, r.as_str()])
            .inc();
    }

    pub(crate) fn lease(&self, platform: &str, o: LeaseOutcome) {
        self.lease_total
            .with_label_values(&[platform, o.as_str()])
            .inc();
    }

    pub(crate) fn skipped(&self, platform: &str, r: SkipReason, n: u64) {
        if n > 0 {
            self.feed_skipped_total
                .with_label_values(&[platform, r.as_str()])
                .inc_by(n);
        }
    }

    pub(crate) fn capped(&self, platform: &str, n: u64) {
        if n > 0 {
            self.capped_total.with_label_values(&[platform]).inc_by(n);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registers_once_and_duplicate_is_a_typed_error() {
        let r = Registry::new();
        let m = RuntimeMetrics::register(&r).expect("first");
        m.init_platform("twitch");
        assert!(RuntimeMetrics::register(&r).is_err());
    }

    #[test]
    fn label_values_are_the_fixed_enums() {
        for (got, want) in [
            (ReconcileOutcome::Ok.as_str(), "ok"),
            (ReconcileOutcome::FeedError.as_str(), "feed_error"),
            (ReconcileOutcome::FeedTimeout.as_str(), "feed_timeout"),
            (RestartReason::Fingerprint.as_str(), "fingerprint"),
            (RestartReason::Panic.as_str(), "panic"),
            (RestartReason::SinkBuild.as_str(), "sink_build"),
            (RestartReason::Ended.as_str(), "ended"),
            (ParkReason::Fatal.as_str(), "fatal"),
            (ParkReason::DriverError.as_str(), "driver_error"),
            (ParkReason::TransportBuild.as_str(), "transport_build"),
            (
                ParkReason::WebhookUnavailable.as_str(),
                "webhook_unavailable",
            ),
            (LeaseOutcome::Acquired.as_str(), "acquired"),
            (LeaseOutcome::Denied.as_str(), "denied"),
            (LeaseOutcome::Error.as_str(), "error"),
            (LeaseOutcome::Lost.as_str(), "lost"),
            (SkipReason::Malformed.as_str(), "malformed"),
            (SkipReason::Duplicate.as_str(), "duplicate"),
            (SkipReason::Conflict.as_str(), "conflict"),
        ] {
            assert_eq!(got, want);
        }
    }

    #[test]
    fn helpers_increment_only_bounded_labels() {
        let r = Registry::new();
        let m = RuntimeMetrics::register(&r).expect("reg");
        m.reconcile("twitch", ReconcileOutcome::Ok);
        m.restart("twitch", RestartReason::Panic);
        m.parked("twitch", ParkReason::Fatal);
        m.lease("twitch", LeaseOutcome::Denied);
        m.skipped("twitch", SkipReason::Conflict, 2);
        m.skipped("twitch", SkipReason::Duplicate, 0);
        m.capped("twitch", 3);
        m.capped("twitch", 0);
        let families = r.gather();
        assert!(families.len() >= 6);
        for f in &families {
            for metric in f.get_metric() {
                for l in metric.get_label() {
                    assert!(
                        ["platform", "outcome", "reason"].contains(&l.name()),
                        "unbounded label {}",
                        l.name()
                    );
                }
            }
        }
        assert_eq!(
            m.feed_skipped_total
                .with_label_values(&["twitch", "conflict"])
                .get(),
            2
        );
        assert_eq!(m.capped_total.with_label_values(&["twitch"]).get(), 3);
    }
}
