//! OTel instruments for at-rest encryption. Reads the global meter provider
//! the process installed at startup (never builds its own), so a dead or
//! unconfigured exporter can never fail a seal/open. Attributes are
//! low-cardinality labels only: never keys, tenant data, or values.

use std::sync::OnceLock;
use std::time::Duration;

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};

struct Instruments {
    ops_total: Counter<u64>,
    op_duration_seconds: Histogram<f64>,
    dek_fetch_total: Counter<u64>,
    dek_cache_hits_total: Counter<u64>,
    key_unavailable_total: Counter<u64>,
    legacy_plaintext_reads_total: Counter<u64>,
}

static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();

fn instruments() -> &'static Instruments {
    INSTRUMENTS.get_or_init(|| {
        let meter = global::meter("bundle_host_crypto");
        Instruments {
            ops_total: meter
                .u64_counter("bundle_crypto_ops_total")
                .with_description("Tenant at-rest crypto operations by op, surface and result")
                .build(),
            op_duration_seconds: meter
                .f64_histogram("bundle_crypto_op_duration_seconds")
                .with_description("Tenant at-rest crypto operation latency (incl. DEK resolution)")
                .with_unit("s")
                .build(),
            dek_fetch_total: meter
                .u64_counter("bundle_crypto_dek_fetch_total")
                .with_description("Tenant DEK fetches from the broker by result")
                .build(),
            dek_cache_hits_total: meter
                .u64_counter("bundle_crypto_dek_cache_hits_total")
                .with_description("Tenant DEK served from the in-memory TTL cache")
                .build(),
            key_unavailable_total: meter
                .u64_counter("bundle_crypto_key_unavailable_total")
                .with_description("Operations refused because the tenant DEK was unavailable")
                .build(),
            legacy_plaintext_reads_total: meter
                .u64_counter("bundle_crypto_legacy_plaintext_reads_total")
                .with_description("Reads that returned a pre-encryption legacy plaintext value")
                .build(),
        }
    })
}

/// Records one seal/open/blind-index op (`op`, `surface`, `result` labels).
pub fn record_op(op: &'static str, surface: &'static str, result: &'static str, took: Duration) {
    let i = instruments();
    let attrs = [
        KeyValue::new("op", op),
        KeyValue::new("surface", surface),
        KeyValue::new("result", result),
    ];
    i.ops_total.add(1, &attrs);
    i.op_duration_seconds.record(took.as_secs_f64(), &attrs);
}

/// Records one broker DEK fetch (`ok` / `error`).
pub fn record_dek_fetch(result: &'static str) {
    instruments()
        .dek_fetch_total
        .add(1, &[KeyValue::new("result", result)]);
}

/// Records one DEK cache hit.
pub fn record_dek_cache_hit() {
    instruments().dek_cache_hits_total.add(1, &[]);
}

/// Records one refused operation due to an unavailable key.
pub fn record_key_unavailable(surface: &'static str) {
    instruments()
        .key_unavailable_total
        .add(1, &[KeyValue::new("surface", surface)]);
}

/// Records one legacy-plaintext read on `surface`.
pub fn record_legacy_plaintext_read(surface: &'static str) {
    instruments()
        .legacy_plaintext_reads_total
        .add(1, &[KeyValue::new("surface", surface)]);
}
