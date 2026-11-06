//! Platform overlay profiles (spec 1.2): one file per platform, each an
//! [`OverlayProfile`](super::plan::OverlayProfile) with host-constant
//! derivation. One registration line per platform keeps merges trivial.

pub mod discord;
pub mod twitch;
pub mod youtube;
