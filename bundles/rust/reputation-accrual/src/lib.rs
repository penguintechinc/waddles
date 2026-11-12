//! Reputation ACCRUAL app bundle (`waddles.core.app.reputation-accrual`).
//!
//! The default reputation LOGIC bundle (`provides: reputation-logic`): it maps
//! platform events to reputation deltas and applies them through the
//! reputation SERVICE bundle's gated tools. All behaviour lives in [`logic`]
//! behind the [`logic::Host`] seam so it is unit-tested on the host target;
//! the `component` module below is the only code touching real WIT imports.
#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;

pub mod logic;

#[cfg(target_arch = "wasm32")]
mod component {
    use serde_json::Value;

    use super::bindings::exports::waddle::bundle::action_stage::Guest as ActionGuest;
    use super::bindings::exports::waddle::bundle::process_stage::Guest as ProcessGuest;
    use super::bindings::exports::waddle::bundle::streaming_lifecycle::{
        Guest as StreamGuest, RecordingInfo, SegmentInfo, StreamInfo,
    };
    use super::bindings::waddle::bundle::metrics::Label;
    use super::bindings::waddle::bundle::tool::ToolError as WitToolError;
    use super::bindings::waddle::bundle::types::{
        PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
    };
    use super::bindings::waddle::bundle::{
        clock, context, flags, identity, kv, log, metrics, tool,
    };
    use super::logic::{self, Host, Input, Level, ToolErr};

    pub struct Component;
    struct WasmHost;

    impl Host for WasmHost {
        /// Raw kv read through the host import.
        fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            kv::get(key).map_err(|e| format!("{e:?}"))
        }
        /// Atomic kv increment-with-ttl through the host import.
        fn kv_increment(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, String> {
            kv::increment(key, delta, ttl_s).map_err(|e| format!("{e:?}"))
        }
        /// Flag lookup, defaulting OFF when the flag is unknown.
        fn flag(&self, key: &str) -> bool {
            flags::enabled(key, false)
        }
        /// The verified actor's community user UUID, if linked.
        fn resolve_actor(&self) -> Option<String> {
            identity::resolve_actor().ok()
        }
        /// Monotonic nanoseconds for duration measurement.
        fn monotonic_nanos(&self) -> u64 {
            clock::monotonic_nanos()
        }
        /// Gated bundle-to-bundle tool call (JSON in, JSON out).
        fn invoke(
            &self,
            target: &str,
            tool_name: &str,
            args: &Value,
            idem: Option<&str>,
        ) -> Result<Value, ToolErr> {
            let bytes =
                serde_json::to_vec(args).map_err(|e| ToolErr::InvalidArgs(e.to_string()))?;
            let out = tool::invoke(target, tool_name, &bytes, idem).map_err(|e| match e {
                WitToolError::NotGranted => ToolErr::NotGranted,
                WitToolError::Forbidden => ToolErr::Forbidden,
                WitToolError::Timeout => ToolErr::Timeout,
                WitToolError::DepthExceeded => ToolErr::DepthExceeded,
                WitToolError::PoolExhausted => ToolErr::PoolExhausted,
                WitToolError::TargetUnavailable => ToolErr::TargetUnavailable,
                WitToolError::InvalidArgs(m) => ToolErr::InvalidArgs(m),
            })?;
            serde_json::from_slice(&out).map_err(|e| ToolErr::InvalidArgs(e.to_string()))
        }
        /// Structured, PII-free log line.
        fn log(&self, level: Level, message: &str, fields: Value) {
            let lvl = match level {
                Level::Error => log::Level::Error,
                Level::Warn => log::Level::Warn,
                Level::Info => log::Level::Info,
                Level::Debug => log::Level::Debug,
            };
            log::write(lvl, message, &fields.to_string());
        }
        /// Emits the outcome counter and duration histogram; failures are
        /// swallowed because telemetry must never fail an event.
        fn metric(&self, outcome: &str, reason_class: &str, elapsed_ms: f64) {
            let labels = |pairs: &[(&str, &str)]| {
                pairs
                    .iter()
                    .map(|(k, v)| Label {
                        key: (*k).to_string(),
                        value: (*v).to_string(),
                    })
                    .collect::<Vec<_>>()
            };
            let _ = metrics::counter_add(
                logic::METRIC_EVENTS,
                1,
                &labels(&[("outcome", outcome), ("reason_class", reason_class)]),
            );
            let _ = metrics::histogram_record(
                logic::METRIC_DURATION,
                elapsed_ms,
                &labels(&[("outcome", outcome)]),
            );
        }
    }

    impl ProcessGuest for Component {
        /// Accrues reputation for one event; never produces a reply event.
        fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            let message_id = context::get_context().message_id;
            let input = Input {
                platform: &event.platform,
                event_type: &event.event_type,
                payload_json: &event.payload_json,
                occurred_at: &event.occurred_at,
                message_id: &message_id,
            };
            logic::process(&WasmHost, &input);
            Ok(None)
        }
    }

    impl ActionGuest for Component {
        /// This bundle has no action stage; any dispatch fails loud.
        fn dispatch(
            _envelope: StageEnvelope,
            _config: String,
        ) -> Result<TransportResult, TransportError> {
            Err(TransportError {
                retryable: false,
                code: "UNSUPPORTED_STAGE".to_string(),
                message: "reputation-accrual has no action stage".to_string(),
                retry_after_ms: None,
            })
        }
    }

    fn unsupported() -> UnsupportedStage {
        UnsupportedStage {
            stage: "streaming-lifecycle".to_string(),
        }
    }

    /// No lifecycle hooks: accrual is event-driven only; these fail loud.
    impl StreamGuest for Component {
        fn on_start(_: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
        fn on_stop(_: StreamInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
        fn on_segment(_: SegmentInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
        fn on_recording_ready(_: RecordingInfo) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Err(unsupported())
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);
