//! Hand-written SeaORM entities for the tables this service owns. No
//! `sea-orm-cli` was run against the cluster (schema/DDL is owned by the
//! SQL migrations under `config/postgres/migrations/`, not this crate) --
//! each `Model` below only declares the columns this service actually
//! reads or writes, which is sufficient because SeaORM's generated queries
//! always select the declared columns explicitly, never `SELECT *`.
//!
//! - `overlay_surfaces`/`presentation_config` (migration 073,
//!   `config/postgres/migrations/073_svc_presentation_overlays.sql`) --
//!   this service's own tables, ported from `core/svc_presentation/
//!   services/schema.py::bind_presentation_tables()` (pydal). Not yet
//!   queried by any P1 code path; declared now so P2/P3 (render routes)
//!   and the webui overlay designer (#458) wire against a single
//!   already-reviewed entity shape instead of each defining their own.
//! - `overlay_view_credentials` (migration 100,
//!   `config/postgres/migrations/100_overlay_view_credentials.sql`) --
//!   backs [`crate::overlay::view_store::SeaOrmViewCredentialStore`], the
//!   concrete `overlay_auth::ViewCredentialStore` this service mounts.
//! - `overlay_images` (migration 101,
//!   `config/postgres/migrations/101_overlay_images.sql`) -- backs
//!   [`crate::images::asset_store::SeaOrmImageAssetStore`] (P6 upload /
//!   P9 render).
//! - `caption_events` (migration 102,
//!   `config/postgres/migrations/102_caption_events_rust_port.sql`) --
//!   backs [`crate::overlay::caption_store::SeaOrmCaptionStore`] (the
//!   caption overlay's reconnect-replay history).
//! - `communities` (hub-owned; read-only, `id`/`tenant_id` only) -- backs
//!   [`crate::overlay::community_ctx::SeaOrmCommunityContextStore`], which
//!   maps a verified credential's `community_id` to its tenant for
//!   display-name resolution.
//! - `communities` again, projected to `id`/`overlay_code` (alembic
//!   `0056_communities_overlay_code`) -- backs
//!   [`crate::overlay::code::SeaOrmOverlayCodeResolver`], which maps the
//!   unguessable overlay code in a URL to the real community id.

pub mod caption_event;
pub mod community;
pub mod community_overlay_code;
pub mod overlay_image;
pub mod overlay_surface;
pub mod overlay_view_credential;
pub mod presentation_config;
