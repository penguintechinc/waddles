//! `waddle-sdk-rs` -- the Tier-1 Rust SDK for Waddles app bundles.
//!
//! Idiomatic bindings over the single normative WIT world
//! `waddle:bundle/stage@1.0.0` (`wit/waddle-bundle/stage.wit`, imported by
//! relative path and never forked). Deliberately **thinner** than
//! `waddle-sdk` (Python): there is no `penguin-dal` facade and no
//! obligation to reproduce a prior API -- see
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS6.5 and
//! SS4.12's closing line ("`waddle-sdk-rs` and `waddle-sdk-js` are
//! thinner: they expose idiomatic bindings over the same WIT world with no
//! compatibility obligation to a prior API").
//!
//! # Authoring a bundle
//!
//! Implement [`ProcessStage`] and/or [`ActionStage`] for a marker type,
//! then call [`export_stage!`] once:
//!
//! ```ignore
//! use waddle_sdk::{ActionStage, PlatformEvent, ProcessStage, TransportError, TransportResult, UnsupportedStage};
//!
//! struct EchoBundle;
//!
//! impl ProcessStage for EchoBundle {
//!     fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
//!         Ok(Some(event))
//!     }
//! }
//!
//! waddle_sdk::export_stage!(EchoBundle);
//! ```
//!
//! Both WIT exports (`process-stage`, `action-stage`) are always present
//! on the compiled component; a stage a bundle does not implement returns
//! the canonical stub response automatically (SS6.5).
//!
//! # Capability modules
//!
//! Each WIT import interface has a matching module exposing idiomatic
//! Rust types and, on `wasm32` targets, the host-call functions
//! themselves: [`context`], [`http`], [`kv`], [`db`], [`relay`],
//! [`flags`], [`log`], [`clock`]. Open-ended JSON fields
//! (`payload-json`/`config-json`/`message-json`/`fields-json`, spec
//! Assumption A2) are exposed as typed accessors (e.g.
//! [`types::PlatformEvent::payload`]) rather than raw strings.

#[cfg(target_arch = "wasm32")]
pub mod bindings_glue;

pub mod clock;
pub mod context;
pub mod db;
pub mod error;
pub mod flags;
pub mod http;
pub mod kv;
pub mod log;
pub mod relay;
pub mod stage;
pub mod types;

pub use context::BundleContext;
pub use error::{DbError, HttpError, KvError, RelayError, SdkError};
pub use stage::{ActionStage, ProcessStage};
pub use types::{PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage};
