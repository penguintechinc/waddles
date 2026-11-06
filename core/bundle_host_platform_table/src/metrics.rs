//! OTel instruments for the platform-table store (no user ids/PII in telemetry).
//! Reads the global `SdkMeterProvider`; never builds its own.

use std::sync::OnceLock;

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};

struct Instruments {
    op_duration_seconds: Histogram<f64>,
    calls_total: Counter<u64>,
    denied_total: Counter<u64>,
}

static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();

fn instruments() -> &'static Instruments {
    INSTRUMENTS.get_or_init(|| {
        let meter = global::meter("bundle_host_platform_table");
        Instruments {
            op_duration_seconds: meter
                .f64_histogram("waddles_bundle_platform_table_call_duration_seconds")
                .with_description("Bundle `platform-table` store op latency")
                .with_unit("s")
                .build(),
            calls_total: meter
                .u64_counter("waddles_bundle_platform_table_calls")
                .with_description("Bundle `platform-table` store calls, by op and outcome")
                .build(),
            denied_total: meter
                .u64_counter("waddles_bundle_platform_table_denied")
                .with_description("Bundle `platform-table` denials, by reason")
                .build(),
        }
    })
}

/// Records one op's latency and outcome.
pub(crate) fn record_call(op: &'static str, outcome: &'static str, seconds: f64) {
    let attrs = [KeyValue::new("op", op), KeyValue::new("outcome", outcome)];
    instruments().op_duration_seconds.record(seconds, &attrs);
    instruments().calls_total.add(1, &attrs);
}

/// Counts one denial (`reason` is a stable code, never user input).
pub fn record_denied(reason: &'static str) {
    instruments()
        .denied_total
        .add(1, &[KeyValue::new("reason", reason)]);
}
