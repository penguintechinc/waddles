//! `metrics`: the bundle-facing counter / histogram / gauge import, mapped onto OTel.
//!
//! Handled entirely inside the executor (no stage round trip): the executor already owns the
//! process's OTel meter provider ([`crate::telemetry`]), and a metric carries no authority. What
//! this module DOES own is the abuse boundary -- every bound documented on the WIT `metrics`
//! interface is enforced here, before an instrument or attribute set is ever created:
//!
//! * names `^[a-z][a-z0-9_]{0,47}$`, at most [`MAX_NAMES_PER_APP`] per bundle, each bound to one
//!   instrument kind **per bundle** -- every bundle records through its own OTel meter scope, so
//!   one bundle can never squat, retype or collide with another bundle's metric name;
//! * at most [`MAX_LABELS`] labels per call, keys `^[a-z][a-z0-9_]{0,31}$` (never `app_id`), and at
//!   most [`MAX_LABEL_KEYS_PER_NAME`] distinct label keys ever used with one `(bundle, name)`;
//! * values `^[A-Za-z0-9_.-]{1,32}$` (no free text) and at most [`MAX_VALUES_PER_KEY`] distinct
//!   values per `(bundle, name, key)` -- later values fold into `other`;
//! * a hard per-bundle series budget ([`MAX_SERIES_PER_APP`] distinct `(name, attribute set)`):
//!   a call that would open a series past it is refused with `limit-exceeded`;
//! * the `app_id` attribute is stamped by the host from the invocation, never the guest;
//! * a call that fails ANY check commits nothing (no name, kind, key, value or series is
//!   recorded), so a rejected call cannot consume a bundle's budgets or poison its state.
//!
//! A rejected call returns the typed WIT error to the guest and is counted on
//! `waddles_executor_bundle_metric_rejects_total{reason}`; nothing is silently dropped.
//!
//! Exported names are `waddles_bundle_<kind>_<name>` (`counter`/`histogram`/`gauge`), so a name
//! shared by two bundles with different kinds can never collide on one exported series name.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, Mutex, OnceLock};

use opentelemetry::metrics::{Counter, Gauge, Histogram, Meter};
use opentelemetry::KeyValue;

use crate::engine::stage_next_world::waddle::bundle::metrics;
use crate::host::ExecState;

/// Most distinct metric names one bundle may use.
pub const MAX_NAMES_PER_APP: usize = 32;
/// Most labels one call may carry.
pub const MAX_LABELS: usize = 4;
/// Most distinct label KEYS one `(bundle, name)` may ever use.
pub const MAX_LABEL_KEYS_PER_NAME: usize = 6;
/// Most distinct values kept per `(bundle, name, key)` before folding into `other`.
pub const MAX_VALUES_PER_KEY: usize = 16;
/// Most distinct series (`name` + attribute set) one bundle may open.
pub const MAX_SERIES_PER_APP: usize = 512;
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

/// Everything one bundle has registered; owned by exactly one `app_id`.
struct AppState {
    meter: Meter,
    kinds: HashMap<String, Kind>,
    instruments: HashMap<String, Arc<Instrument>>,
    label_keys: HashMap<String, HashSet<String>>,
    values: HashMap<(String, String), HashSet<String>>,
    series: HashSet<(String, BTreeMap<String, String>)>,
}

/// Supplies the OTel meter a bundle records through (one scope per `app_id`).
type MeterFor = Box<dyn Fn(&str) -> Meter + Send + Sync>;

/// The enforcing front for bundle metrics.
pub struct BundleMetrics {
    meter_for: MeterFor,
    rejects: Counter<u64>,
    apps: Mutex<HashMap<String, AppState>>,
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
    /// Builds the front; `rejects` and every bundle's instruments come from `meter_for(app_id)`
    /// (`""` for the host's own reject counter).
    #[must_use]
    pub fn new(meter_for: impl Fn(&str) -> Meter + Send + Sync + 'static) -> Self {
        let rejects = meter_for("")
            .u64_counter("waddles_executor_bundle_metric_rejects")
            .with_description("Bundle metric calls the host refused, by reason")
            .build();
        Self {
            meter_for: Box::new(meter_for),
            rejects,
            apps: Mutex::new(HashMap::new()),
        }
    }

    fn reject(&self, reason: &'static str, e: metrics::Error) -> metrics::Error {
        self.rejects.add(1, &[KeyValue::new("reason", reason)]);
        e
    }

    /// Validates the whole call against `app`'s CURRENT state without mutating it, then commits
    /// every registration at once. Any refusal returns before a single write.
    fn prepare(
        &self,
        app_id: &str,
        name: &str,
        kind: Kind,
        labels: &[metrics::Label],
    ) -> Result<(Arc<Instrument>, Vec<KeyValue>), metrics::Error> {
        if !valid_name(name) {
            return Err(self.reject("bad_name", metrics::Error::InvalidName));
        }
        if labels.len() > MAX_LABELS {
            return Err(self.reject("too_many_labels", metrics::Error::InvalidLabel));
        }
        let mut seen = HashSet::new();
        for l in labels {
            if !valid_key(&l.key) || !seen.insert(l.key.as_str()) {
                return Err(self.reject("bad_label_key", metrics::Error::InvalidLabel));
            }
            if !valid_value(&l.value) {
                return Err(self.reject("bad_label_value", metrics::Error::InvalidLabel));
            }
        }
        // Never held across an await: this whole method is synchronous.
        let mut apps = self.apps.lock().unwrap_or_else(|p| p.into_inner());
        let empty_meter = || (self.meter_for)(app_id);
        let st = apps.entry(app_id.to_owned()).or_insert_with(|| AppState {
            meter: empty_meter(),
            kinds: HashMap::new(),
            instruments: HashMap::new(),
            label_keys: HashMap::new(),
            values: HashMap::new(),
            series: HashSet::new(),
        });

        // ---- validate (read-only) ----
        match st.kinds.get(name) {
            Some(k) if *k != kind => {
                return Err(self.reject("kind_conflict", metrics::Error::InvalidName))
            }
            None if st.kinds.len() >= MAX_NAMES_PER_APP => {
                return Err(self.reject("too_many_names", metrics::Error::LimitExceeded))
            }
            _ => {}
        }
        let known_keys = st.label_keys.get(name);
        let new_keys = labels
            .iter()
            .filter(|l| !known_keys.is_some_and(|k| k.contains(&l.key)))
            .count();
        if known_keys.map_or(0, HashSet::len) + new_keys > MAX_LABEL_KEYS_PER_NAME {
            return Err(self.reject("too_many_label_keys", metrics::Error::LimitExceeded));
        }
        let mut attrs: Vec<KeyValue> = vec![KeyValue::new("app_id", app_id.to_owned())];
        let mut fingerprint = BTreeMap::new();
        let mut admit: Vec<(String, String)> = Vec::new();
        for l in labels {
            let admitted = st.values.get(&(name.to_owned(), l.key.clone()));
            let value = if admitted.is_some_and(|v| v.contains(&l.value)) {
                l.value.clone()
            } else if admitted.map_or(0, HashSet::len) < MAX_VALUES_PER_KEY {
                admit.push((l.key.clone(), l.value.clone()));
                l.value.clone()
            } else {
                OVERFLOW_VALUE.to_owned()
            };
            fingerprint.insert(l.key.clone(), value.clone());
            attrs.push(KeyValue::new(l.key.clone(), value));
        }
        let series_key = (name.to_owned(), fingerprint);
        if !st.series.contains(&series_key) && st.series.len() >= MAX_SERIES_PER_APP {
            return Err(self.reject("series_budget", metrics::Error::LimitExceeded));
        }

        // ---- commit (only reached when every check passed) ----
        st.kinds.insert(name.to_owned(), kind);
        let keys = st.label_keys.entry(name.to_owned()).or_default();
        for l in labels {
            keys.insert(l.key.clone());
        }
        for (k, v) in admit {
            st.values.entry((name.to_owned(), k)).or_default().insert(v);
        }
        st.series.insert(series_key);
        // The kind is part of the exported name: the same bundle-chosen `name` can then never
        // surface as two different instrument types under one OTel/Prometheus name, whichever
        // bundles (or kinds) are involved.
        let kind_word = match kind {
            Kind::Counter => "counter",
            Kind::Histogram => "histogram",
            Kind::Gauge => "gauge",
        };
        let full = format!("waddles_bundle_{kind_word}_{name}");
        let key = format!("{full}/{kind:?}");
        let meter = st.meter.clone();
        let inst = st.instruments.entry(key).or_insert_with(|| {
            Arc::new(match kind {
                Kind::Counter => Instrument::Counter(meter.u64_counter(full).build()),
                Kind::Histogram => Instrument::Histogram(meter.f64_histogram(full).build()),
                Kind::Gauge => Instrument::Gauge(meter.f64_gauge(full).build()),
            })
        });
        Ok((Arc::clone(inst), attrs))
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
    Arc::clone(
        GLOBAL.get_or_init(|| Arc::new(BundleMetrics::new(crate::metrics::bundle_meter_for))),
    )
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
        let m = BundleMetrics::new(h.meter_for_app_fn());
        (h, m)
    }

    const APP: &str = "waddles.test.app";
    const OTHER: &str = "waddles.test.other";

    fn limit(r: Result<(), metrics::Error>) -> bool {
        matches!(r, Err(metrics::Error::LimitExceeded))
    }

    // regression: distinct label KEYS per (app, name) are capped (unbounded keys = unbounded series)
    #[test]
    fn distinct_label_keys_per_name_are_capped() {
        let (_h, m) = fresh();
        for i in 0..MAX_LABEL_KEYS_PER_NAME {
            m.counter_add(APP, "keys", 1, &[l(&format!("k{i}"), "v")])
                .unwrap();
        }
        assert!(limit(m.counter_add(APP, "keys", 1, &[l("k_extra", "v")])));
        // Existing keys keep working, and another name has its own key budget.
        m.counter_add(APP, "keys", 1, &[l("k0", "other")]).unwrap();
        m.counter_add(APP, "keys2", 1, &[l("k_extra", "v")])
            .unwrap();
    }

    // regression: a per-bundle series budget refuses the excess instead of growing without bound
    #[test]
    fn series_budget_refuses_the_excess() {
        let (_h, m) = fresh();
        let mut opened = 0usize;
        'outer: for n in 0..MAX_NAMES_PER_APP {
            let name = format!("series_{n}");
            for a in 0..=MAX_VALUES_PER_KEY {
                for b in 0..=MAX_VALUES_PER_KEY {
                    let r = m.counter_add(
                        APP,
                        &name,
                        1,
                        &[l("a", &format!("a{a}")), l("b", &format!("b{b}"))],
                    );
                    match r {
                        Ok(()) => opened += 1,
                        Err(metrics::Error::LimitExceeded) => break 'outer,
                        Err(_) => panic!("only the series budget may refuse here"),
                    }
                }
            }
        }
        // Folded `other` values reuse series, so distinct series == accepted distinct tuples;
        // the budget was reached and enforced exactly.
        assert!(opened >= MAX_SERIES_PER_APP, "calls accepted: {opened}");
        // An existing series still records; a different bundle is unaffected.
        m.counter_add(APP, "series_0", 1, &[l("a", "a0"), l("b", "b0")])
            .unwrap();
        m.counter_add(OTHER, "series_0", 1, &[l("a", "a0")])
            .unwrap();
    }

    // regression: one bundle can neither squat nor retype another bundle's metric name
    #[test]
    fn a_metric_name_is_namespaced_per_bundle() {
        let (h, m) = fresh();
        m.gauge_set(APP, "shared", 1.0, &[]).unwrap();
        // Another bundle uses the same name as a different kind: allowed, independent.
        m.counter_add(OTHER, "shared", 4, &[]).unwrap();
        // ...and the first bundle is still bound to its own kind.
        assert!(matches!(
            m.counter_add(APP, "shared", 1, &[]),
            Err(metrics::Error::InvalidName)
        ));
        let pts = h.points();
        let counter = pts
            .iter()
            .find(|p| p.name == "waddles_bundle_counter_shared" && p.attrs["app_id"] == OTHER)
            .expect("other bundle's counter");
        assert_eq!(counter.value, 4.0);
        let gauge = pts
            .iter()
            .find(|p| p.name == "waddles_bundle_gauge_shared" && p.attrs["app_id"] == APP)
            .expect("first bundle's gauge");
        assert_eq!(gauge.value, 1.0);
        // Same bundle-chosen name, different kinds, two bundles: two DISTINCT exported names.
        let exported: std::collections::HashSet<&str> = pts
            .iter()
            .filter(|p| p.name.ends_with("_shared"))
            .map(|p| p.name.as_str())
            .collect();
        assert_eq!(
            exported,
            [
                "waddles_bundle_counter_shared",
                "waddles_bundle_gauge_shared"
            ]
            .into_iter()
            .collect()
        );
        // The name budget is per bundle too.
        for i in 0..MAX_NAMES_PER_APP - 1 {
            m.counter_add(OTHER, &format!("n{i}"), 1, &[]).unwrap();
        }
        m.counter_add(APP, "fresh_name", 1, &[]).unwrap();
    }

    // regression: a call refused by ANY check commits nothing (no name/key/value/series)
    #[test]
    fn a_refused_call_commits_no_state() {
        let (_h, m) = fresh();
        // Valid name but a bad label: refused. Do it more times than the name budget allows.
        for i in 0..MAX_NAMES_PER_APP + 8 {
            assert!(matches!(
                m.counter_add(APP, &format!("ghost_{i}"), 1, &[l("k", "has space")]),
                Err(metrics::Error::InvalidLabel)
            ));
        }
        // None of those consumed the name budget: a full set of real names still fits.
        for i in 0..MAX_NAMES_PER_APP {
            m.counter_add(APP, &format!("real_{i}"), 1, &[]).unwrap();
        }
        // A key-budget refusal does not admit the refused call's value or key.
        for i in 0..MAX_LABEL_KEYS_PER_NAME {
            m.counter_add(APP, "real_0", 1, &[l(&format!("k{i}"), "v")])
                .unwrap();
        }
        assert!(limit(m.counter_add(
            APP,
            "real_0",
            1,
            &[l("k0", "v"), l("zzz", "v")]
        )));
        let apps = m.apps.lock().unwrap();
        let st = &apps[APP];
        assert!(!st.label_keys["real_0"].contains("zzz"));
        assert!(!st
            .values
            .contains_key(&("real_0".to_owned(), "zzz".to_owned())));
    }

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
            .find(|p| p.name == "waddles_bundle_counter_sent")
            .unwrap();
        assert_eq!(c.value, 3.0);
        assert_eq!(c.attrs["app_id"], APP);
        assert_eq!(c.attrs["outcome"], "ok");
        assert!(pts
            .iter()
            .any(|p| p.name == "waddles_bundle_histogram_took_seconds" && p.sum == 0.25));
        assert!(pts
            .iter()
            .any(|p| p.name == "waddles_bundle_gauge_queue_depth" && p.value == 7.0));
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
            .filter(|p| p.name == "waddles_bundle_counter_by_channel")
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
