//! OTel instruments for the `kv` capability (task requirement: "kv op
//! latency histogram, error counter by kind, quota-rejection counter").
//! Reads the global `SdkMeterProvider` `penguin-logging::telemetry::init`
//! installs at process startup in both `core/svc_process` and
//! `core/svc_action` (`opentelemetry::global::set_meter_provider`) -- this
//! module never builds its own provider, so it emits nothing before
//! startup and (per that module's doc) never panics if OTLP export itself
//! is unconfigured; the meter/instruments still exist and are simply
//! read by whatever exporter (or none) the process installed.

use std::sync::OnceLock;

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};

use crate::authorize::KV_PERMISSION_ID;

struct Instruments {
    op_duration_seconds: Histogram<f64>,
    op_errors_total: Counter<u64>,
    quota_rejections_total: Counter<u64>,
    authorize_denied_total: Counter<u64>,
    maxmemory_policy_violation_total: Counter<u64>,
}

static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();

fn instruments() -> &'static Instruments {
    INSTRUMENTS.get_or_init(|| {
        let meter = global::meter("bundle_host_kv");
        Instruments {
            op_duration_seconds: meter
                .f64_histogram("waddles_bundle_kv_op_duration_seconds")
                .with_description("Bundle `kv` host-capability op latency")
                .with_unit("s")
                .build(),
            op_errors_total: meter
                .u64_counter("waddles_bundle_kv_op_errors_total")
                .with_description("Bundle `kv` host-capability errors, by op and error kind")
                .build(),
            quota_rejections_total: meter
                .u64_counter("waddles_bundle_kv_quota_rejections_total")
                .with_description(
                    "Bundle `kv` host-capability quota/rate-limit rejections, by op and quota kind",
                )
                .build(),
            authorize_denied_total: meter
                .u64_counter("waddles_bundle_kv_authorize_denied_total")
                .with_description(
                    "Bundle `kv` host-capability authorize() denials, by app_id and permission -- \
                     undeclared storage.kv (crate::authorize)",
                )
                .build(),
            maxmemory_policy_violation_total: meter
                .u64_counter("waddles_bundle_kv_maxmemory_policy_violation_total")
                .with_description(
                    "Valkey maxmemory-policy is an allkeys-* eviction policy at startup -- \
                     count_key can be evicted, silently resetting the storage.kv quota",
                )
                .build(),
        }
    })
}

/// Records one op's end-to-end latency (validation + authorization +
/// backend round trip), regardless of outcome -- a rejected or failed op
/// still consumed CPU/Valkey time worth seeing in the histogram.
pub fn record_op_duration(op: &'static str, outcome: &'static str, seconds: f64) {
    instruments().op_duration_seconds.record(
        seconds,
        &[
            KeyValue::new("op", op),
            KeyValue::new("outcome", outcome),
            KeyValue::new("permission", KV_PERMISSION_ID),
        ],
    );
}

/// Increments the error counter for one op, labeled with a stable error
/// `kind` (`"invalid_key"`, `"too_large"`, `"not_granted"`, `"backend"`,
/// ...) so a dashboard can break down failures without parsing log lines.
pub fn record_error(op: &'static str, kind: &'static str) {
    instruments().op_errors_total.add(
        1,
        &[
            KeyValue::new("op", op),
            KeyValue::new("kind", kind),
            KeyValue::new("permission", KV_PERMISSION_ID),
        ],
    );
}

/// Increments the quota-rejection counter for one op, labeled with which
/// quota was hit (`"key_count"`, `"rate_limit"`).
pub fn record_quota_rejection(op: &'static str, quota: &'static str) {
    instruments().quota_rejections_total.add(
        1,
        &[
            KeyValue::new("op", op),
            KeyValue::new("quota", quota),
            KeyValue::new("permission", KV_PERMISSION_ID),
        ],
    );
}

/// Increments the `authorize()` denial counter for `app_id`/`permission`
/// (`crate::authorize::authorize_kv`) -- distinct from
/// [`record_error`]'s generic `kind="not_granted"` bucket so a dashboard
/// can break authorization denials down by app without parsing log lines.
pub fn record_authorize_denied(app_id: &str, permission: &'static str) {
    instruments().authorize_denied_total.add(
        1,
        &[
            KeyValue::new("app_id", app_id.to_string()),
            KeyValue::new("permission", permission),
        ],
    );
}

/// Increments the maxmemory-policy violation counter (`crate::policy`'s
/// startup check) -- a one-shot gauge-like signal, incremented once per
/// check, not per op.
pub fn record_maxmemory_policy_violation(policy: &str) {
    instruments()
        .maxmemory_policy_violation_total
        .add(1, &[KeyValue::new("policy", policy.to_string())]);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Not a behavioral assertion (there is no test meter-provider reader
    /// wired up here to inspect) -- just proves every instrument builds
    /// and every recording call is panic-free against whatever default/
    /// no-op global meter provider is installed in a `cargo test` process
    /// that never called `penguin_logging::telemetry::init`.
    #[test]
    fn recording_every_instrument_does_not_panic_without_a_configured_provider() {
        record_op_duration("get", "ok", 0.001);
        record_error("set", "invalid_key");
        record_quota_rejection("increment", "key_count");
        record_authorize_denied("waddles.bot.a", KV_PERMISSION_ID);
        record_maxmemory_policy_violation("allkeys-lru");
    }
}
