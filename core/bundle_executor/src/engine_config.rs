// SPDX-License-Identifier: AGPL-3.0-or-later
//! The ONE wasmtime `Config` builder shared by the runtime executor
//! (`crate::engine::build_engine`) and the ahead-of-time precompiler
//! (`crate::precompile`), plus the engine fingerprint that keys every
//! precompiled `.cwasm` artifact.
//!
//! Why a dedicated module: a `.cwasm` produced under one `Config` and
//! deserialized under another is at best rejected by wasmtime and at worst
//! subtly wrong (fuel/epoch instrumentation, GC collector). Both code paths
//! therefore call [`engine_config`] -- there is no second place a knob can
//! drift -- and both compute [`engine_fingerprint`] from the built `Engine`
//! so an artifact is only ever looked up under the exact configuration that
//! produced it (executor-scale CONTRACT: "single shared builder fn used by
//! BOTH precompiler and executor - no drift").

use std::hash::{Hash, Hasher};

use sha2::{Digest, Sha256};
use wasmtime::{Collector, Config, Engine, InstanceAllocationStrategy, PoolingAllocationConfig};

use crate::engine::WASMTIME_VERSION;

/// Domain separator mixed into [`engine_fingerprint`] so the value can
/// never collide with any other sha256 this crate computes. Bump the
/// suffix if the fingerprint's input layout ever changes.
const FINGERPRINT_DOMAIN: &[u8] = b"waddles-engine-fp-v1";

/// Builds the wasmtime `Config` every bundle engine in this process uses:
/// component model, epoch interruption, fuel metering, the `drc` GC
/// collector (spec SS7.2 -- must match componentize-py's collector) and the
/// pooling instance allocator.
pub fn engine_config() -> Config {
    let mut config = Config::new();
    config.wasm_component_model(true);
    config.epoch_interruption(true);
    // Connector spec SS0 condition 4: fuel metering alongside the epoch
    // deadline. Epoch ticks bound wall-clock time between yield points;
    // fuel bounds guest-executed work directly, so a tight loop that never
    // reaches a yield point still traps deterministically.
    // `crate::invoke::on_invoke` arms the per-call budget via
    // `Store::set_fuel`; a `Store` with accounting on and no fuel set traps
    // immediately, so every call site MUST call `set_fuel`.
    config.consume_fuel(true);
    config.collector(Collector::DeferredReferenceCounting);

    let mut pooling = PoolingAllocationConfig::default();
    // spec SS7.2 defaults: EXECUTOR_INSTANCES_PER_BUNDLE (4) resident
    // stores per bundle, EXECUTOR_MAX_CONCURRENT_CALLS (32) globally. The
    // pooling allocator's own ceiling is set generously above both so a
    // legitimate config override never gets silently capped.
    pooling.total_component_instances(256);
    pooling.total_memories(256);
    pooling.total_tables(256);
    config.allocation_strategy(InstanceAllocationStrategy::Pooling(pooling));
    config
}

/// Feeds `std::hash::Hash` output into SHA-256 instead of SipHash, so the
/// fingerprint is stable across processes, hosts and Rust releases
/// (`DefaultHasher` makes no such promise). `finish()` is never used for
/// the fingerprint itself -- only the accumulated digest is.
struct Sha256Hasher(Sha256);

impl Hasher for Sha256Hasher {
    fn finish(&self) -> u64 {
        let digest = self.0.clone().finalize();
        let mut first = [0u8; 8];
        first.copy_from_slice(&digest[..8]);
        u64::from_le_bytes(first)
    }

    fn write(&mut self, bytes: &[u8]) {
        self.0.update(bytes);
    }
}

/// Lowercase-hex SHA-256 over the wasmtime version, the target triple and
/// `Engine::precompile_compatibility_hash()` (which covers every `Config`
/// setting that affects compiled code). Two engines with equal
/// fingerprints can load each other's precompiled artifacts; a different
/// fingerprint means "recompile", never "try anyway".
pub fn engine_fingerprint(engine: &Engine) -> String {
    let mut hasher = Sha256Hasher(Sha256::new());
    hasher.write(FINGERPRINT_DOMAIN);
    hasher.write(WASMTIME_VERSION.as_bytes());
    hasher.write(std::env::consts::ARCH.as_bytes());
    hasher.write(std::env::consts::OS.as_bytes());
    engine.precompile_compatibility_hash().hash(&mut hasher);
    format!("{:x}", hasher.0.finalize())
}

/// True iff `fp` has the exact shape [`engine_fingerprint`] produces (64
/// lowercase hex chars) -- used to refuse a fingerprint that could escape
/// its object-key prefix before it is ever interpolated into a key.
pub fn is_valid_fingerprint(fp: &str) -> bool {
    fp.len() == 64 && fp.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn fingerprint_is_deterministic_for_the_shared_config() {
        let a = Engine::new(&engine_config()).unwrap();
        let b = Engine::new(&engine_config()).unwrap();
        assert_eq!(engine_fingerprint(&a), engine_fingerprint(&b));
        assert!(is_valid_fingerprint(&engine_fingerprint(&a)));
    }

    #[test]
    fn fingerprint_changes_when_the_config_drifts() {
        let shared = Engine::new(&engine_config()).unwrap();
        let mut drifted_cfg = engine_config();
        drifted_cfg.consume_fuel(false);
        let drifted = Engine::new(&drifted_cfg).unwrap();
        assert_ne!(engine_fingerprint(&shared), engine_fingerprint(&drifted));
    }

    #[test]
    fn fingerprint_validator_rejects_traversal_and_wrong_length() {
        assert!(!is_valid_fingerprint("../etc"));
        assert!(!is_valid_fingerprint(&"A".repeat(64)));
        assert!(!is_valid_fingerprint(&"a".repeat(63)));
        assert!(is_valid_fingerprint(&"0".repeat(64)));
    }
}
