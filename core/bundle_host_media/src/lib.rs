//! Bundle `media` host capability: Gemini image / music / video generation with a
//! host-side tier gate and per-community BYOK. See the module docs and
//! `docs/superpowers/specs/2026-11-06-harness-media-generation.md`.
//!
//! Layering: [`host::MediaHost`] orchestrates; [`tier`], [`credentials`],
//! [`provider`]/[`gemini`], [`store`] and [`jobs`] are the seams. The stage
//! (`core/svc_process`) owns the capability gate and feature flag; everything
//! here runs after `media.generate` has been granted.

pub mod auth;
pub mod credentials;
pub mod gemini;
pub mod host;
pub mod jobs;
mod metrics;
pub mod provider;
pub mod store;
pub mod tier;
pub mod types;
pub mod validate;

pub use host::{Clock, MediaConfig, MediaHost, SystemClock};
pub use types::{Handle, JobState, MediaError, MediaKind, Scope, Tier};
