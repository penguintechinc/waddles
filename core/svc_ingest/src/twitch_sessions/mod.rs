//! Per-community Twitch inbound sessions.
//!
//! `spec` turns hub-api's data-plane binding list into the desired set of
//! sessions (complete-or-fail paging, deduplicated); later layers reconcile
//! that set against running sessions.

pub mod spec;

pub use spec::{
    discover, DiscoverError, Discovery, SessionSpec, SpecError, MAX_PAGES, META_BOT_LOGIN,
    META_CHANNEL, PAGE_LIMIT,
};
