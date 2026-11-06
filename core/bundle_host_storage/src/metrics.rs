//! OTel instruments (global meter provider installed at process start).

use std::sync::OnceLock;
use std::time::Duration;

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};

struct Instruments {
    duration: Histogram<f64>,
    denied: Counter<u64>,
    bytes: Counter<u64>,
    quota: Counter<u64>,
    audit_dropped: Counter<u64>,
}

static I: OnceLock<Instruments> = OnceLock::new();

fn i() -> &'static Instruments {
    I.get_or_init(|| {
        let m = global::meter("bundle_host_storage");
        Instruments {
            duration: m
                .f64_histogram("waddles_bundle_storage_op_duration_seconds")
                .with_unit("s")
                .build(),
            denied: m.u64_counter("waddles_bundle_storage_denied_total").build(),
            bytes: m.u64_counter("waddles_bundle_storage_bytes_total").build(),
            audit_dropped: m
                .u64_counter("waddles_bundle_storage_audit_dropped_total")
                .build(),
            quota: m
                .u64_counter("waddles_bundle_storage_quota_rejections_total")
                .build(),
        }
    })
}

pub fn op_duration(op: &'static str, d: Duration) {
    i().duration
        .record(d.as_secs_f64(), &[KeyValue::new("op", op)]);
}
pub fn denied(reason: &'static str) {
    i().denied.add(1, &[KeyValue::new("reason", reason)]);
}
pub fn bytes(dir: &'static str, n: u64) {
    i().bytes.add(n, &[KeyValue::new("dir", dir)]);
}
pub fn audit_dropped() {
    i().audit_dropped.add(1, &[]);
}
pub fn quota_rejected(kind: &'static str) {
    i().quota.add(1, &[KeyValue::new("kind", kind)]);
}
