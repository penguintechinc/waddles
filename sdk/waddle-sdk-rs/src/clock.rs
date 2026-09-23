//! Idiomatic wrapper over the WIT `clock` interface (always granted;
//! `wit/waddle-bundle/stage.wit` `interface clock`).
//!
//! A bundle running inside a WASI component has no direct syscall access
//! to wall-clock or monotonic time -- every reading comes from the host.

/// Milliseconds since the Unix epoch, as the stage sees it.
///
/// Only compiles for `wasm32` targets -- see `crate::bindings_glue`'s
/// module doc comment for the resulting host coverage carve-out.
#[cfg(target_arch = "wasm32")]
pub fn now_millis() -> u64 {
    crate::bindings_glue::clock_now_millis()
}

/// RFC 3339 UTC, millisecond precision.
#[cfg(target_arch = "wasm32")]
pub fn now_rfc3339() -> String {
    crate::bindings_glue::clock_now_rfc3339()
}

/// Monotonic nanoseconds, for in-bundle duration measurement only -- never
/// meaningful across separate invocations or compared to wall-clock time.
#[cfg(target_arch = "wasm32")]
pub fn monotonic_nanos() -> u64 {
    crate::bindings_glue::clock_monotonic_nanos()
}

/// Computes an elapsed duration in milliseconds from two
/// [`monotonic_nanos`] readings. Host-testable in isolation from the
/// actual host call: saturates to `0` rather than underflowing/panicking
/// if `end` precedes `start` (e.g. a wrapped counter on a host that does
/// not guarantee monotonicity across an implementation bug).
pub fn elapsed_ms(start_nanos: u64, end_nanos: u64) -> u64 {
    end_nanos.saturating_sub(start_nanos) / 1_000_000
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn elapsed_ms_converts_nanos_to_millis() {
        assert_eq!(elapsed_ms(0, 5_000_000), 5);
    }

    #[test]
    fn elapsed_ms_saturates_on_reversed_readings() {
        assert_eq!(elapsed_ms(10, 5), 0);
    }

    #[test]
    fn elapsed_ms_zero_for_identical_readings() {
        assert_eq!(elapsed_ms(42, 42), 0);
    }
}
