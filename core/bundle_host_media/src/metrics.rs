//! OTel instruments (suffix-free names: the Prometheus bridge appends `_total` /
//! `_seconds` itself). Labels are closed vocabularies only -- never prompts,
//! tenant/community ids, keys or provider text.

use std::sync::OnceLock;

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};

struct Instruments {
    duration: Histogram<f64>,
    calls: Counter<u64>,
}

static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();

fn instruments() -> &'static Instruments {
    INSTRUMENTS.get_or_init(|| {
        let meter = global::meter("bundle_host_media");
        Instruments {
            duration: meter
                .f64_histogram("waddles_bundle_media_call_duration")
                .with_description(
                    "Bundle `media` generation latency (image sync call / job end-to-end)",
                )
                .with_unit("s")
                .build(),
            calls: meter
                .u64_counter("waddles_bundle_media_calls")
                .with_description("Bundle `media` calls by kind/op and outcome")
                .build(),
        }
    })
}

/// Records one finished operation. `outcome` is `ok` or a `MediaError::code()`.
pub(crate) fn record(kind: &'static str, op: &'static str, outcome: &'static str, seconds: f64) {
    let attrs = [
        KeyValue::new("kind", kind),
        KeyValue::new("op", op),
        KeyValue::new("outcome", outcome),
    ];
    instruments().duration.record(seconds, &attrs);
    instruments().calls.add(1, &attrs);
}
