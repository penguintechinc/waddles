//! Overlay-auth wiring (contract C4, `overlay_auth` crate): the concrete
//! VIEW/PUSH trust sources this service provides, plus the axum routers
//! that mount `overlay_auth`'s guard middleware ahead of the real overlay
//! routes P2 (render), P3 (live SSE/websocket), and P4 (push) add.
//!
//! P1 mounts the guards with zero routes behind them -- see [`router`]'s
//! module doc for why that is deliberate, not a stub.

pub mod push_trust;
pub mod router;
pub mod view_store;

pub use push_trust::AppPushTrustSource;
pub use view_store::SeaOrmViewCredentialStore;
