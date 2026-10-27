//! The unified overlay wire contract (issues #456/#457, contract C3):
//! the surface id set, the push payload shape a bundle/action pushes, and
//! the SSE/websocket envelope a live client receives.
//!
//! This is the **single source of truth** svc-presentation-rust (#457),
//! svc-streaming-rust (#456), the bundle `overlay` WIT interface
//! (`wit/waddle-bundle/stage.wit`), and the webui's overlay designer
//! (#458) all target -- no second copy of this shape should exist
//! anywhere else in Rust. Field names and semantics mirror
//! `core/svc_presentation/services/render.py`'s existing Python overlay
//! scaffold byte-for-byte (`title`/`body`/`image_url`/`text`/`type:
//! "clear"`) so the eventual Rust port is a faithful behavioral match, not
//! a redesign.
//!
//! This crate defines shape only -- no transport, no HTTP, no SSE/
//! websocket framing code. It derives `serde::{Serialize, Deserialize}`
//! and nothing else; callers own how these types cross the wire (Axum
//! JSON body, SSE `data: <json>\n\n` frame, websocket text frame -- the
//! JSON is identical for all three).

mod envelope;
mod push;
mod surface;

pub use envelope::{ConnectedFrame, OverlayEnvelope};
pub use push::{
    AlertPayload, CaptionPayload, ChatMessagePayload, GoalPayload, OverlayPush, PushKind,
};
pub use surface::Surface;
