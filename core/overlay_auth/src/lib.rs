//! Unified overlay-auth contract (streaming/overlay Rust rewrite, chunk
//! C4): one scheme replacing the two that exist in Python today.
//!
//! # Before
//!
//! - **`community_overlay_tokens`** (hub-api, `hub_api/blueprints/v1/
//!   overlay.py` + `services/overlay_service.py`; legacy admin surface
//!   `core/browser_source_core_module/services/overlay_service.py`): a
//!   per-community 64-hex-char opaque key, plaintext at rest, 5-minute
//!   rotation grace period. Issued/rotated by hub-api; **never actually
//!   checked by svc-presentation's GET routes** --
//!   `core/svc_presentation/services/surfaces.py::is_valid_community()` is
//!   a syntax-only regex check, so `/overlay/<community>/<surface>` is
//!   unauthenticated today. Anyone who can guess/enumerate a
//!   `community_id` can view that community's overlay.
//! - **`PRESENTATION_PUSH_TOKEN`** (`core/svc_presentation/blueprints/
//!   overlay.py::push`): a single static bearer secret, open by default
//!   when unset, shared across every community and every calling bundle --
//!   exactly the "long-lived static API key" anti-pattern `security.md`
//!   Service-to-Service Auth forbids.
//!
//! # After (this crate)
//!
//! | | [`view`] (GET, OBS browser source) | [`push`] (POST, action-stage adapters) |
//! |---|---|---|
//! | Shape | opaque `?key=` query param | `Authorization: Bearer <machine JWT>` |
//! | Scoped to | one community (checked at lookup, not just hash match) | one community (`scope` claim, `push::push_scope()`) |
//! | Rotatable | yes, 5-min grace (`view::ROTATION_GRACE_PERIOD`) | yes -- short-lived JWT, re-minted per call |
//! | Storage | SHA-256 hash only (`token::hash_token`), never plaintext | none -- `service_auth`'s existing EdDSA verifier |
//! | New crypto in this crate | `rand`+`sha2` (opaque token gen/hash) | **none** -- delegates to `service_auth::verify` |
//!
//! # Migration
//!
//! `config/postgres/migrations/100_overlay_view_credentials.sql` backfills
//! every existing `community_overlay_tokens` row into the new
//! `overlay_view_credentials` table by hashing its plaintext key forward --
//! existing OBS browser-source URLs keep working unchanged, no customer
//! action required. See that migration file's header comment for the one
//! remaining follow-up it flags (hub-api's admin rotate endpoint needs a
//! write-through to the hashed table in a later chunk so the two tables
//! don't drift after this one-time backfill).
//!
//! # Consumers
//!
//! P4/P5 (future svc-presentation-rust's view routes) and the action-stage
//! push path both depend on this crate directly rather than re-implementing
//! either credential -- see [`view::require_view_credential`] and
//! [`push::require_push_credential`] for the Axum middleware shape each
//! wires in.

pub mod error;
pub mod push;
pub mod token;
pub mod view;

pub use error::OverlayAuthError;
pub use push::{
    push_scope, require_push_credential, PushCredential, PushTrustSource, OVERLAY_PUSH_SCOPE_PREFIX,
};
pub use token::{generate_view_token, hash_token};
pub use view::{
    require_view_credential, validate_view_token, ViewCredential, ViewCredentialRecord,
    ViewCredentialStore, ROTATION_GRACE_PERIOD,
};
