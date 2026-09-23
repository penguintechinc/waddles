//! The traits a bundle author implements, and the [`export_stage!`] macro
//! that wires them into the WIT world's two exports
//! (`waddle:bundle/process-stage` and `waddle:bundle/action-stage`).
//!
//! Both exports are always present on the compiled component -- the spec
//! is explicit that "Tier 1 SDKs generate the stub automatically" for
//! whichever stage a bundle does not implement (SS6.5). [`ProcessStage`]
//! and [`ActionStage`] both carry default method bodies that return the
//! canonical stub response, so a bundle author only writes the trait impl
//! for the stage(s) it actually uses.

use crate::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};

/// Implemented by a bundle that reacts to inbound platform events.
///
/// `None` means "no reply"; the event is dropped, exactly as v1's
/// `transform() -> PlatformEvent | None` (WIT doc comment on
/// `process-stage.transform`). The default implementation is the stub a
/// bundle that only implements [`ActionStage`] gets for free.
pub trait ProcessStage {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        let _ = event;
        Err(UnsupportedStage::process())
    }
}

/// Implemented by a bundle that dispatches outbound actions.
///
/// `config` is the resolved 3-tier config (activation > tenant
/// availability > bundle default) as canonical JSON object text -- use
/// [`crate::context::BundleContext::config`] for the typed accessor when
/// the same shape is also available via `get_context()`. The default
/// implementation is the stub a bundle that only implements
/// [`ProcessStage`] gets for free.
pub trait ActionStage {
    fn dispatch(envelope: StageEnvelope, config: &str) -> Result<TransportResult, TransportError> {
        let _ = (envelope, config);
        Err(TransportError::unsupported_stage())
    }
}

/// Runs `T::transform`, converting between the WIT-generated export types
/// and this crate's idiomatic [`PlatformEvent`]/[`UnsupportedStage`].
/// Called from the [`export_stage!`]-generated `Guest` impl; not intended
/// to be called directly by bundle authors.
#[cfg(target_arch = "wasm32")]
pub fn run_transform<T: ProcessStage>(
    event: crate::bindings_glue::waddle::bundle::types::PlatformEvent,
) -> Result<
    Option<crate::bindings_glue::waddle::bundle::types::PlatformEvent>,
    crate::bindings_glue::waddle::bundle::types::UnsupportedStage,
> {
    let event = crate::bindings_glue::platform_event_from_wit(event);
    match T::transform(event) {
        Ok(Some(out)) => Ok(Some(crate::bindings_glue::platform_event_to_wit(out))),
        Ok(None) => Ok(None),
        Err(u) => Err(crate::bindings_glue::unsupported_stage_to_wit(u)),
    }
}

/// Runs `T::dispatch`, converting between the WIT-generated export types
/// and this crate's idiomatic [`StageEnvelope`]/[`TransportResult`]/
/// [`TransportError`]. Called from the [`export_stage!`]-generated `Guest`
/// impl; not intended to be called directly by bundle authors.
#[cfg(target_arch = "wasm32")]
pub fn run_dispatch<T: ActionStage>(
    envelope: crate::bindings_glue::waddle::bundle::types::StageEnvelope,
    config: String,
) -> Result<
    crate::bindings_glue::waddle::bundle::types::TransportResult,
    crate::bindings_glue::waddle::bundle::types::TransportError,
> {
    let envelope = crate::bindings_glue::stage_envelope_from_wit(envelope);
    match T::dispatch(envelope, &config) {
        Ok(result) => Ok(crate::bindings_glue::transport_result_to_wit(result)),
        Err(err) => Err(crate::bindings_glue::transport_error_to_wit(err)),
    }
}

/// Wires `$ty` (a type implementing [`ProcessStage`] and/or
/// [`ActionStage`]) into the component's `waddle:bundle/process-stage` and
/// `waddle:bundle/action-stage` exports, and calls the generated
/// `export!` macro to make the crate a valid `waddle:bundle/stage@1.0.0`
/// component. Call exactly once, from the bundle crate's `lib.rs`:
///
/// ```ignore
/// struct MyBundle;
///
/// impl waddle_sdk::ProcessStage for MyBundle {
///     fn transform(event: waddle_sdk::PlatformEvent)
///         -> Result<Option<waddle_sdk::PlatformEvent>, waddle_sdk::UnsupportedStage>
///     {
///         Ok(Some(event))
///     }
/// }
///
/// // Required even when only the default is wanted: Rust only applies a
/// // trait's default method to a type that explicitly implements the
/// // trait, so the stub `dispatch` needs this empty block to take effect.
/// impl waddle_sdk::ActionStage for MyBundle {}
///
/// waddle_sdk::export_stage!(MyBundle);
/// ```
///
/// A bundle that only cares about one stage still exports both -- the
/// other's default method returns the stub response the WIT world
/// requires (SS6.5), as long as its trait is still (possibly empty)
/// explicitly implemented.
#[macro_export]
macro_rules! export_stage {
    ($ty:ty) => {
        /// Zero-sized marker type the generated `Guest` impls dispatch
        /// through to `$ty`'s `ProcessStage`/`ActionStage` implementation.
        struct __WaddleStageComponent;

        impl $crate::bindings_glue::exports::waddle::bundle::process_stage::Guest for __WaddleStageComponent {
            fn transform(
                event: $crate::bindings_glue::waddle::bundle::types::PlatformEvent,
            ) -> ::core::result::Result<
                ::core::option::Option<$crate::bindings_glue::waddle::bundle::types::PlatformEvent>,
                $crate::bindings_glue::waddle::bundle::types::UnsupportedStage,
            > {
                $crate::stage::run_transform::<$ty>(event)
            }
        }

        impl $crate::bindings_glue::exports::waddle::bundle::action_stage::Guest for __WaddleStageComponent {
            fn dispatch(
                envelope: $crate::bindings_glue::waddle::bundle::types::StageEnvelope,
                config: ::std::string::String,
            ) -> ::core::result::Result<
                $crate::bindings_glue::waddle::bundle::types::TransportResult,
                $crate::bindings_glue::waddle::bundle::types::TransportError,
            > {
                $crate::stage::run_dispatch::<$ty>(envelope, config)
            }
        }

        $crate::bindings_glue::export!(__WaddleStageComponent with_types_in $crate::bindings_glue);
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    struct OnlyProcess;
    impl ProcessStage for OnlyProcess {
        fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
            Ok(Some(event))
        }
    }

    struct NeitherImplemented;
    impl ProcessStage for NeitherImplemented {}
    impl ActionStage for NeitherImplemented {}

    fn sample_event() -> PlatformEvent {
        PlatformEvent {
            platform: "discord".to_string(),
            event_type: "message.create".to_string(),
            actor: None,
            payload_json: "{}".to_string(),
            occurred_at: "2026-09-22T00:00:00.000Z".to_string(),
        }
    }

    #[test]
    fn process_stage_default_is_the_unsupported_stub() {
        let err = NeitherImplemented::transform(sample_event()).unwrap_err();
        assert_eq!(err, UnsupportedStage::process());
    }

    #[test]
    fn action_stage_default_is_the_unsupported_transport_error() {
        let envelope = StageEnvelope {
            tenant: "acme".to_string(),
            community: None,
            app_id: "app-1".to_string(),
            stage: "action".to_string(),
            event: sample_event(),
            ts: "2026-09-22T00:00:00.000Z".to_string(),
            target_app_id: None,
            trace_context: None,
        };
        let err = NeitherImplemented::dispatch(envelope, "{}").unwrap_err();
        assert_eq!(err, TransportError::unsupported_stage());
    }

    #[test]
    fn implemented_process_stage_overrides_the_default() {
        let event = sample_event();
        let result = OnlyProcess::transform(event.clone()).expect("implemented, not an error");
        assert_eq!(result, Some(event));
    }
}
