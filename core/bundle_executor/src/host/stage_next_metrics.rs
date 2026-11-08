//! `metrics`: the bundle-facing counter / histogram / gauge import, mapped onto OTel.
//!
//! Handled entirely inside the executor (no stage round trip): the executor already owns the
//! process's OTel meter provider ([`crate::telemetry`]), and a metric carries no authority. What
//! this module DOES own is the abuse boundary -- every bound documented on the WIT `metrics`
//! interface is enforced here, before an instrument or attribute set is ever created:
//!
//! * names `^[a-z][a-z0-9_]{0,47}$`, at most [`MAX_NAMES_PER_APP`] per bundle, each bound to one
//!   instrument kind for the life of the process;
//! * at most [`MAX_LABELS`] labels, keys `^[a-z][a-z0-9_]{0,31}$`, never `app_id`;
//! * values `^[A-Za-z0-9_.-]{1,32}$` (no free text) and at most [`MAX_VALUES_PER_KEY`] distinct
//!   values per `(bundle, name, key)` -- later values fold into `other`, so cardinality is bounded
//!   by construction;
//! * the `app_id` attribute is stamped by the host from the invocation, never the guest.
//!
//! A rejected call returns the typed WIT error to the guest and is counted on
//! `waddles_executor_bundle_metric_rejects_total{reason}`; nothing is silently dropped.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, OnceLock};

use opentelemetry::metrics::{Counter, Gauge, Histogram, Meter};
use opentelemetry::KeyValue;

use crate::engine::stage_next_world::waddle::bundle::metrics;
use crate::host::ExecState;

/// Most distinct metric names one bundle may use.
pub const MAX_NAMES_PER_APP: usize = 32;
/// Most labels one call may carry.
pub const MAX_LABELS: usize = 4;
/// Most distinct values kept per `(bundle, name, key)` before folding into `other`.
pub const MAX_VALUES_PER_KEY: usize = 16;
/// Largest single counter increment.
pub const MAX_COUNTER_DELTA: u64 = 1_000_000;
const OVERFLOW_VALUE: &str = "other";

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Kind {
    Counter,
    Histogram,
    Gauge,
}

enum Instrument {
    Counter(Counter<u64>),
    Histogram(Histogram<f64>),
    Gauge(Gauge<f64>),
}

#[derive(Default)]
struct State {
    /// metric name -> kind, process-wide (an OTel name can hold one instrument type).
    kinds: HashMap<String, Kind>,
    /// app -> names used.
    names: HashMap<String, HashSet<String>>,
    /// (app, name, key) -> admitted values.
    values: HashMap<(String, String, String), HashSet<String>>,
    instruments: HashMap<String, Arc<Instrument>>,
}

/// The enforcing front for bundle metrics.
pub struct BundleMetrics {
    meter: Meter,
    rejects: Counter<u64>,
    state: Mutex<State>,
}

fn valid_name(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 48
        && b[0].is_ascii_lowercase()
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_')
}

fn valid_key(s: &str) -> bool {
    valid_name(s) && s.len() <= 32 && s != "app_id"
}

fn valid_value(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 32
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'-'))
}

impl BundleMetrics {
    /// Builds the front over `meter`.
    #[must_use]
    pub fn new(meter: &Meter) -> Self {
        Self {
            meter: meter.clone(),
            rejects: meter
                .u64_counter("waddles_executor_bundle_metric_rejects")
                .with_description("Bundle metric calls the host refused, by reason")
                .build(),
            state: Mutex::new(State::default()),
        }
    }

    fn reject(&self, reason: &'static str, e: metrics::Error) -> metrics::Error {
        self.rejects.add(1, &[KeyValue::new("reason", reason)]);
        e
    }

    /// Validates `labels` and returns the attribute set (admitting values within the cap).
    fn attributes(
        &self,
        st: &mut State,
        app_id: &str,
        name: &str,
        labels: &[metrics::Label],
    ) -> Result<Vec<KeyValue>, metrics::Error> {
        if labels.len() > MAX_LABELS {
            return Err(self.reject("too_many_labels", metrics::Error::InvalidLabel));
        }
        let mut out = vec![KeyValue::new("app_id", app_id.to_owned())];
        let mut seen = HashSet::new();
        for l in labels {
            if !valid_key(&l.key) || !seen.insert(l.key.clone()) {
                return Err(self.reject("bad_label_key", metrics::Error::InvalidLabel));
            }
            if !valid_value(&l.value) {
                return Err(self.reject("bad_label_value", metrics::Error::InvalidLabel));
            }
            let admitted = st
                .values
                .entry((app_id.to_owned(), name.to_owned(), l.key.clone()))
                .or_default();
            let value = if admitted.contains(&l.value) {
                l.value.clone()
            } else if admitted.len() < MAX_VALUES_PER_KEY {
                admitted.insert(l.value.clone());
                l.value.clone()
            } else {
                OVERFLOW_VALUE.to_owned()
            };
            out.push(KeyValue::new(l.key.clone(), value));
        }
        Ok(out)
    }

    fn instrument(
        &self,
        st: &mut State,
        app_id: &str,
        name: &str,
        kind: Kind,
    ) -> Result<Arc<Instrument>, metrics::Error> {
        if !valid_name(name) {
            return Err(self.reject("bad_name", metrics::Error::InvalidName));
        }
        match st.kinds.get(name) {
            Some(k) if *k != kind => {
                return Err(self.reject("kind_conflict", metrics::Error::InvalidName))
            }
            _ => {}
        }
        let used = st.names.entry(app_id.to_owned()).or_default();
        if !used.contains(name) {
            if used.len() >= MAX_NAMES_PER_APP {
                return Err(self.reject("too_many_names", metrics::Error::LimitExceeded));
            }
            used.insert(name.to_owned());
        }
        st.kinds.insert(name.to_owned(), kind);
        let full = format!("waddles_bundle_{name}");
        let inst = st.instruments.entry(full.clone()).or_insert_with(|| {
            Arc::new(match kind {
                Kind::Counter => Instrument::Counter(self.meter.u64_counter(full).build()),
                Kind::Histogram => Instrument::Histogram(self.meter.f64_histogram(full).build()),
                Kind::Gauge => Instrument::Gauge(self.meter.f64_gauge(full).build()),
            })
        });
        Ok(Arc::clone(inst))
    }

    fn prepare(
        &self,
        app_id: &str,
        name: &str,
        kind: Kind,
        labels: &[metrics::Label],
    ) -> Result<(Arc<Instrument>, Vec<KeyValue>), metrics::Error> {
        // Never held across an await: the whole method is synchronous.
        let mut st = self.state.lock().unwrap_or_else(|p| p.into_inner());
        let inst = self.instrument(&mut st, app_id, name, kind)?;
        let attrs = self.attributes(&mut st, app_id, name, labels)?;
        Ok((inst, attrs))
    }

    /// `counter-add`.
    ///
    /// # Errors
    /// The typed WIT error for the first violated bound.
    pub fn counter_add(
        &self,
        app_id: &str,
        name: &str,
        delta: u64,
        labels: &[metrics::Label],
    ) -> Result<(), metrics::Error> {
        if delta > MAX_COUNTER_DELTA {
            return Err(self.reject("bad_value", metrics::Error::InvalidValue));
        }
        let (inst, attrs) = self.prepare(app_id, name, Kind::Counter, labels)?;
        if let Instrument::Counter(c) = &*inst {
            c.add(delta, &attrs);
        }
        Ok(())
    }

    /// `histogram-record`.
    ///
    /// # Errors
    /// The typed WIT error for the first violated bound.
    pub fn histogram_record(
        &self,
        app_id: &str,
        name: &str,
        value: f64,
        labels: &[metrics::Label],
    ) -> Result<(), metrics::Error> {
        if !value.is_finite() || value < 0.0 {
            return Err(self.reject("bad_value", metrics::Error::InvalidValue));
        }
        let (inst, attrs) = self.prepare(app_id, name, Kind::Histogram, labels)?;
        if let Instrument::Histogram(h) = &*inst {
            h.record(value, &attrs);
        }
        Ok(())
    }

    /// `gauge-set`.
    ///
    /// # Errors
    /// The typed WIT error for the first violated bound.
    pub fn gauge_set(
        &self,
        app_id: &str,
        name: &str,
        value: f64,
        labels: &[metrics::Label],
    ) -> Result<(), metrics::Error> {
        if !value.is_finite() {
            return Err(self.reject("bad_value", metrics::Error::InvalidValue));
        }
        let (inst, attrs) = self.prepare(app_id, name, Kind::Gauge, labels)?;
        if let Instrument::Gauge(g) = &*inst {
            g.record(value, &attrs);
        }
        Ok(())
    }
}

/// The process-wide front, bound to the global meter provider [`crate::telemetry`] installs.
pub fn bundle_metrics() -> Arc<BundleMetrics> {
    static GLOBAL: OnceLock<Arc<BundleMetrics>> = OnceLock::new();
    Arc::clone(GLOBAL.get_or_init(|| Arc::new(BundleMetrics::new(&crate::metrics::bundle_meter()))))
}

impl metrics::Host for ExecState {
    async fn counter_add(
        &mut self,
        name: String,
        delta: u64,
        labels: Vec<metrics::Label>,
    ) -> Result<(), metrics::Error> {
        bundle_metrics().counter_add(&self.app_id, &name, delta, &labels)
    }

    async fn histogram_record(
        &mut self,
        name: String,
        value: f64,
        labels: Vec<metrics::Label>,
    ) -> Result<(), metrics::Error> {
        bundle_metrics().histogram_record(&self.app_id, &name, value, &labels)
    }

    async fn gauge_set(
        &mut self,
        name: String,
        value: f64,
        labels: Vec<metrics::Label>,
    ) -> Result<(), metrics::Error> {
        bundle_metrics().gauge_set(&self.app_id, &name, value, &labels)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::metrics::test_support::Harness;

    fn l(k: &str, v: &str) -> metrics::Label {
        metrics::Label {
            key: k.into(),
            value: v.into(),
        }
    }

    fn fresh() -> (Harness, BundleMetrics) {
        let h = Harness::new();
        let m = BundleMetrics::new(&h.meter());
        (h, m)
    }

    const APP: &str = "waddles.test.app";

    #[test]
    fn counter_histogram_and_gauge_reach_the_sink_with_host_stamped_app_id() {
        let (h, m) = fresh();
        m.counter_add(APP, "sent", 3, &[l("outcome", "ok")])
            .unwrap();
        m.histogram_record(APP, "took_seconds", 0.25, &[]).unwrap();
        m.gauge_set(APP, "queue_depth", 7.0, &[]).unwrap();
        // The harness reads back what an OTLP collector would receive.
        let pts = h.points();
        assert!(pts.len() >= 3, "data points received: {}", pts.len());
        let c = pts
            .iter()
            .find(|p| p.name == "waddles_bundle_sent")
            .unwrap();
        assert_eq!(c.value, 3.0);
        assert_eq!(c.attrs["app_id"], APP);
        assert_eq!(c.attrs["outcome"], "ok");
        assert!(pts
            .iter()
            .any(|p| p.name == "waddles_bundle_took_seconds" && p.sum == 0.25));
        assert!(pts
            .iter()
            .any(|p| p.name == "waddles_bundle_queue_depth" && p.value == 7.0));
    }

    #[test]
    fn names_values_and_labels_are_validated() {
        let (_h, m) = fresh();
        for bad in ["", "A", "1x", "a.b", "a-b", &"x".repeat(49)] {
            assert!(matches!(
                m.counter_add(APP, bad, 1, &[]),
                Err(metrics::Error::InvalidName)
            ));
        }
        assert!(matches!(
            m.counter_add(APP, "ok_name", MAX_COUNTER_DELTA + 1, &[]),
            Err(metrics::Error::InvalidValue)
        ));
        assert!(matches!(
            m.histogram_record(APP, "h", -1.0, &[]),
            Err(metrics::Error::InvalidValue)
        ));
        assert!(matches!(
            m.histogram_record(APP, "h", f64::NAN, &[]),
            Err(metrics::Error::InvalidValue)
        ));
        assert!(matches!(
            m.gauge_set(APP, "g", f64::INFINITY, &[]),
            Err(metrics::Error::InvalidValue)
        ));
        // Label key/value rules, reserved key, duplicate key, count cap.
        for labels in [
            vec![l("app_id", "spoof")],
            vec![l("Bad", "x")],
            vec![l("k", "has space")],
            vec![l("k", "")],
            vec![l("k", &"v".repeat(33))],
            vec![l("k", "a"), l("k", "b")],
            (0..5).map(|i| l(&format!("k{i}"), "v")).collect(),
        ] {
            assert!(matches!(
                m.counter_add(APP, "c", 1, &labels),
                Err(metrics::Error::InvalidLabel)
            ));
        }
    }

    #[test]
    fn a_name_is_bound_to_one_kind_and_a_bundle_to_a_name_budget() {
        let (_h, m) = fresh();
        m.counter_add(APP, "dual", 1, &[]).unwrap();
        assert!(matches!(
            m.gauge_set(APP, "dual", 1.0, &[]),
            Err(metrics::Error::InvalidName)
        ));
        for i in 0..MAX_NAMES_PER_APP - 1 {
            m.counter_add(APP, &format!("n{i}"), 1, &[]).unwrap();
        }
        assert!(matches!(
            m.counter_add(APP, "one_too_many", 1, &[]),
            Err(metrics::Error::LimitExceeded)
        ));
        // Another bundle has its own budget; an already-used name keeps working.
        m.counter_add("waddles.test.other", "one_too_many", 1, &[])
            .unwrap();
        m.counter_add(APP, "dual", 1, &[]).unwrap();
    }

    #[test]
    fn label_cardinality_is_capped_and_folds_into_other() {
        let (h, m) = fresh();
        for i in 0..MAX_VALUES_PER_KEY + 10 {
            m.counter_add(APP, "by_channel", 1, &[l("channel", &format!("c{i}"))])
                .unwrap();
        }
        let pts: Vec<_> = h
            .points()
            .into_iter()
            .filter(|p| p.name == "waddles_bundle_by_channel")
            .collect();
        assert_eq!(
            pts.len(),
            MAX_VALUES_PER_KEY + 1,
            "16 admitted values + the `other` bucket"
        );
        let other = pts.iter().find(|p| p.attrs["channel"] == "other").unwrap();
        assert_eq!(other.value, 10.0);
    }

    #[tokio::test]
    async fn the_wit_host_impl_stamps_the_invocations_app_id() {
        use crate::engine::stage_next_world::waddle::bundle::metrics::Host;
        let mut state = ExecState::new(None, "waddles.test.host-impl".to_string(), 1);
        state
            .counter_add("host_impl_total".into(), 2, vec![l("outcome", "ok")])
            .await
            .unwrap();
        state
            .histogram_record("host_impl_seconds".into(), 0.5, vec![])
            .await
            .unwrap();
        state
            .gauge_set("host_impl_depth".into(), 1.0, vec![])
            .await
            .unwrap();
        assert!(matches!(
            state.counter_add("Bad Name".into(), 1, vec![]).await,
            Err(metrics::Error::InvalidName)
        ));
    }
}
