//! Overlay-auth wiring (contract C4, `overlay_auth` crate): the concrete
//! VIEW/PUSH trust sources this service provides, plus the axum routers
//! that mount `overlay_auth`'s guard middleware ahead of the real overlay
//! routes P2 (render), P3 (live SSE/websocket), and P4 (push) add.
//!
//! [`hub`] is P3's in-process push fan-out; `crate::http::overlay` (P4)
//! mounts the real routes -- `GET .../live` (SSE), `GET .../live/ws`
//! (websocket), `POST .../push` -- behind [`router::with_view_guard`]/
//! [`router::with_push_guard`], consuming [`hub::PresentationHub`]
//! directly rather than any route in this module.

pub mod detok;
pub mod hub;
pub mod push_trust;
pub mod render;
pub mod router;
pub mod view_store;

pub use hub::PresentationHub;
pub use push_trust::AppPushTrustSource;
pub use view_store::SeaOrmViewCredentialStore;
