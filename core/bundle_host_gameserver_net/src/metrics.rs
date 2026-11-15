//! OTel instruments (outcome counter + latency histogram). Labels are fixed enums only --
//! never connection ids, hosts or commands. Reads the global meter provider.

use std::sync::OnceLock;

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};

struct Instruments {
    duration: Histogram<f64>,
    calls: Counter<u64>,
    bytes: Counter<u64>,
}

static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();

fn instruments() -> &'static Instruments {
    INSTRUMENTS.get_or_init(|| {
        let meter = global::meter("bundle_host_gameserver_net");
        Instruments {
            duration: meter
                .f64_histogram("waddles_bundle_gameserver_net_duration_seconds")
                .with_description("Bundle `gameserver-net` op latency")
                .with_unit("s")
                .build(),
            calls: meter
                .u64_counter("waddles_bundle_gameserver_net_calls_total")
                .with_description("Bundle `gameserver-net` calls, by op and outcome")
                .build(),
            bytes: meter
                .u64_counter("waddles_bundle_gameserver_net_bytes_total")
                .with_description("Bytes moved through gameserver-net, by direction")
                .build(),
        }
    })
}

/// Records one op's latency and outcome.
pub(crate) fn record_call(op: &'static str, outcome: &'static str, seconds: f64) {
    let attrs = [KeyValue::new("op", op), KeyValue::new("outcome", outcome)];
    instruments().duration.record(seconds, &attrs);
    instruments().calls.add(1, &attrs);
}

/// Records bytes moved (`dir` = `in`/`out`).
pub(crate) fn record_bytes(dir: &'static str, n: usize) {
    instruments()
        .bytes
        .add(n as u64, &[KeyValue::new("dir", dir)]);
}
