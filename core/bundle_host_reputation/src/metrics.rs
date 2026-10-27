//! OTel instruments for the `reputation` capability store (rules/
//! critical-rules.md Observability: latency histogram + outcome counter, no
//! user ids or PII in telemetry). Reads the global `SdkMeterProvider`
//! `penguin-logging` installs at process start; never builds its own.

use std::sync::OnceLock;

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};

struct Instruments {
    op_duration_seconds: Histogram<f64>,
    calls_total: Counter<u64>,
    delta_abs: Histogram<f64>,
}

static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();

fn instruments() -> &'static Instruments {
    INSTRUMENTS.get_or_init(|| {
        let meter = global::meter("bundle_host_reputation");
        Instruments {
            op_duration_seconds: meter
                .f64_histogram("waddles_bundle_reputation_call_duration_seconds")
                .with_description("Bundle `reputation` store op latency")
                .with_unit("s")
                .build(),
            calls_total: meter
                .u64_counter("waddles_bundle_reputation_calls_total")
                .with_description("Bundle `reputation` store calls, by op and outcome")
                .build(),
            delta_abs: meter
                .f64_histogram("waddles_bundle_reputation_delta_abs")
                .with_description("Absolute delta of APPLIED reputation adjustments")
                .build(),
        }
    })
}

/// Records one op's latency and outcome (`ok`, `not_a_member`,
/// `daily_cap_exceeded`, `invalid_args`, `backend`).
pub(crate) fn record_call(op: &'static str, outcome: &'static str, seconds: f64) {
    let attrs = [KeyValue::new("op", op), KeyValue::new("outcome", outcome)];
    instruments().op_duration_seconds.record(seconds, &attrs);
    instruments().calls_total.add(1, &attrs);
}

/// Records the absolute size of one applied adjustment.
pub(crate) fn record_applied_delta(abs: u32) {
    instruments().delta_abs.record(f64::from(abs), &[]);
}
