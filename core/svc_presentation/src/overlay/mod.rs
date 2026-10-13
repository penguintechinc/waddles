//! Overlay-auth wiring (contract C4, `overlay_auth` crate): the concrete
//! VIEW/PUSH trust sources this service provides, plus the guards that
//! resolve the unguessable overlay code in the URL to a community
//! ([`code`]) and then apply `overlay_auth`'s VIEW/PUSH checks ahead of the
//! real overlay routes P2 (render), P3 (live SSE/websocket), and P4 (push) add.
//!
//! [`hub`] is P3's in-process push fan-out; `crate::http::overlay` (P4)
//! mounts the real routes -- `GET .../live` (SSE), `GET .../live/ws`
//! (websocket), `POST .../push` -- behind [`router::with_view_guard`]/
//! [`router::with_push_guard`], consuming [`hub::PresentationHub`]
//! directly rather than any route in this module.
//!
//! The push route renders before it publishes: [`community_ctx`] supplies the
//! credential-derived tenant + theme, [`detok::OverlayDetokenizer`] resolves
//! and HTML-escapes the push through the per-surface [`render`]ers, and only
//! the sanitized [`render::RenderedFrame`] is fanned out.

pub mod caption_store;
pub mod code;
pub mod community_ctx;
pub mod detok;
pub mod hub;
pub mod push_trust;
pub mod render;
pub mod router;
pub mod view_store;

pub use caption_store::{CaptionStore, SeaOrmCaptionStore};
pub use code::{OverlayCodeResolver, SeaOrmOverlayCodeResolver, StaticOverlayCodes};
pub use community_ctx::{CommunityContextStore, SeaOrmCommunityContextStore};
pub use hub::PresentationHub;
pub use push_trust::AppPushTrustSource;
pub use view_store::SeaOrmViewCredentialStore;
