//! OTel instruments for the `db` capability (rules/critical-rules.md
//! Observability: "a span per call, histograms for latency and rows,
//! counters for denials. No row data or PII in telemetry"). Mirrors
//! `bundle_host_kv::metrics` -- reads the global `SdkMeterProvider`
//! `penguin-logging` installs at process startup, never builds its own.

use std::sync::OnceLock;

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};

use crate::authorize::DB_PERMISSION_ID;

struct Instruments {
    op_duration_seconds: Histogram<f64>,
    rows_returned: Histogram<f64>,
    op_errors_total: Counter<u64>,
    quota_rejections_total: Counter<u64>,
    authorize_denied_total: Counter<u64>,
}

static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();

fn instruments() -> &'static Instruments {
    INSTRUMENTS.get_or_init(|| {
        let meter = global::meter("bundle_host_db");
        Instruments {
            op_duration_seconds: meter
                .f64_histogram("waddles_bundle_tables_call_duration_seconds")
                .with_description("Bundle `db` host-capability op latency")
                .with_unit("s")
                .build(),
            rows_returned: meter
                .f64_histogram("waddles_bundle_tables_rows")
                .with_description("Rows returned/affected by one `db` host-capability op")
                .build(),
            op_errors_total: meter
                .u64_counter("waddles_bundle_tables_calls_total")
                .with_description("Bundle `db` host-capability calls, by op and result")
                .build(),
            quota_rejections_total: meter
                .u64_counter("waddles_bundle_quota_denied_total")
                .with_description("Bundle `db` host-capability quota rejections, by quota kind")
                .build(),
            authorize_denied_total: meter
                .u64_counter("waddles_bundle_tables_authorize_denied_total")
                .with_description(
                    "Bundle `db` host-capability authorize() denials, by app_id -- \
                     undeclared storage.tables (crate::authorize)",
                )
                .build(),
        }
    })
}

/// Records one op's end-to-end latency, regardless of outcome.
pub fn record_op_duration(op: &'static str, outcome: &'static str, seconds: f64) {
    instruments().op_duration_seconds.record(
        seconds,
        &[
            KeyValue::new("op", op),
            KeyValue::new("outcome", outcome),
            KeyValue::new("permission", DB_PERMISSION_ID),
        ],
    );
}

/// Records rows returned/affected by one op -- never the row *content*.
pub fn record_rows(op: &'static str, rows: u64) {
    instruments()
        .rows_returned
        .record(rows as f64, &[KeyValue::new("op", op)]);
}

/// Increments the call-result counter for one op, labeled `result` (`"ok"`
/// or a stable error kind: `"invalid_column"`, `"not_found"`, `"conflict"`,
/// `"quota_exceeded"`, `"timeout"`, `"backend"`, `"not_granted"`).
pub fn record_call(op: &'static str, result: &'static str) {
    instruments().op_errors_total.add(
        1,
        &[
            KeyValue::new("op", op),
            KeyValue::new("result", result),
            KeyValue::new("permission", DB_PERMISSION_ID),
        ],
    );
}

/// Increments the quota-rejection counter, labeled with which quota was hit
/// (`"row_count"`, `"text_bytes"`, `"jsonb_bytes"`, `"rate_limit"`,
/// `"query_limit"`).
pub fn record_quota_rejection(app_id: &str, quota_kind: &'static str) {
    instruments().quota_rejections_total.add(
        1,
        &[
            KeyValue::new("app_id", app_id.to_string()),
            KeyValue::new("quota_kind", quota_kind),
        ],
    );
}

/// Increments the `authorize()` denial counter for `app_id`.
pub fn record_authorize_denied(app_id: &str, permission: &'static str) {
    instruments().authorize_denied_total.add(
        1,
        &[
            KeyValue::new("app_id", app_id.to_string()),
            KeyValue::new("permission", permission),
        ],
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Not a behavioral assertion -- proves every instrument builds and
    /// every recording call is panic-free without a configured provider.
    #[test]
    fn recording_every_instrument_does_not_panic_without_a_configured_provider() {
        record_op_duration("insert", "ok", 0.001);
        record_rows("query", 3);
        record_call("get", "not_found");
        record_quota_rejection("waddles.bot.a", "row_count");
        record_authorize_denied("waddles.bot.a", DB_PERMISSION_ID);
    }
}
