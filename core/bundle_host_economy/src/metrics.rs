//! OTel instruments for the `economy` capability store (rules/
//! critical-rules.md Observability: latency histogram + outcome counter, no
//! user ids or PII in telemetry). Reads the global `SdkMeterProvider`
//! `penguin-logging` installs at process start; never builds its own.

use std::sync::OnceLock;

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};

struct Instruments {
    op_duration_seconds: Histogram<f64>,
    calls_total: Counter<u64>,
    amount: Histogram<f64>,
}

static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();

fn instruments() -> &'static Instruments {
    INSTRUMENTS.get_or_init(|| {
        let meter = global::meter("bundle_host_economy");
        Instruments {
            op_duration_seconds: meter
                .f64_histogram("waddles_bundle_economy_call_duration_seconds")
                .with_description("Bundle `economy` store op latency")
                .with_unit("s")
                .build(),
            calls_total: meter
                .u64_counter("waddles_bundle_economy_calls_total")
                .with_description("Bundle `economy` store calls, by op and outcome")
                .build(),
            amount: meter
                .f64_histogram("waddles_bundle_economy_amount")
                .with_description("Amount moved by APPLIED economy ops (stake / transfer amount)")
                .build(),
        }
    })
}

/// Records one op's latency and outcome (`ok`, `not_a_member`,
/// `insufficient_funds`, `over_cap`, `invalid_args`, `backend`).
pub(crate) fn record_call(op: &'static str, outcome: &'static str, seconds: f64) {
    let attrs = [KeyValue::new("op", op), KeyValue::new("outcome", outcome)];
    instruments().op_duration_seconds.record(seconds, &attrs);
    instruments().calls_total.add(1, &attrs);
}

/// Records the size of one applied money-moving op.
pub(crate) fn record_applied_amount(op: &'static str, amount: i64) {
    instruments()
        .amount
        .record(amount as f64, &[KeyValue::new("op", op)]);
}
