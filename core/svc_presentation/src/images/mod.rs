//! Overlay image assets: P6 (authenticated upload to SeaweedFS) and P9
//! (`Surface::image` render -- resolving a pushed asset reference into a
//! signed, scoped, expiring URL).
//!
//! # Why `OverlayPush.image_url` carries an asset reference, not a raw URL
//!
//! `overlay_schema::OverlayPush` (contract C3, merged and NOT modified by
//! this change) documents `image_url` as "`full_screen`/`media` -- must be
//! `http(s)://`" -- that doc describes those two *pre-existing* surfaces'
//! use of the field, carried over verbatim from the Python alpha's
//! `render.py`. `Surface::image` is new (#458) and has no dedicated wire
//! field of its own in the frozen contract; rather than invent a second
//! `extra`-escape-hatch convention (documented as "never populated by any
//! of the 9 built-in surfaces" -- reserved for bundle-widget data-binding,
//! not this), this subsystem repurposes the same `image_url` field for
//! `Surface::image` pushes to hold the opaque `asset_id` (UUID) of a
//! previously-uploaded asset (P6), scoped to the pushing credential's own
//! community. [`render::render_image_push`] resolves that reference into
//! the real, signed URL the overlay client actually loads -- the pusher
//! (an action-stage adapter) never sees or constructs a bucket URL itself,
//! and RISK #3 (serve images via signed/scoped/expiring URLs, never a
//! flat/public avatar-style path) is enforced at render time, in one
//! place, for every consumer.
//!
//! Display params (`position_x`/`position_y`/`width`/`height`/
//! `duration_ms`) live on the stored asset row (set once, at upload time)
//! rather than being re-sent on every push -- a push is just "show this
//! already-uploaded asset now", not a redescription of how to show it.
//!
//! # Module map
//!
//! - [`store`]: the `ImageStore` trait + its production `object_store`-
//!   backed implementation (`ObjectStoreImageStore`) -- SeaweedFS PUT +
//!   presigned-GET-URL signing.
//! - [`asset_store`]: the `AssetStore` trait + its production SeaORM-backed
//!   implementation (`SeaOrmImageAssetStore`) -- `overlay_images` metadata
//!   rows, always queried scoped to `(community_id, asset_id)`.
//! - [`upload`]: P6's axum handler (`POST /{overlay_code}/image/push`,
//!   PUSH-guarded, gated on `crate::flags::IMAGE_UPLOAD_FLAG`).
//! - [`render`]: P9's pure transform, `render_image_push`.

pub mod asset_store;
pub mod render;
pub mod store;
pub mod upload;

pub use asset_store::{AssetStore, ImageAssetRecord, SeaOrmImageAssetStore};
pub use render::{render_image_push, ImageSurfaceRender, RenderError};
pub use store::{ImageStore, ImageStoreError, ObjectStoreImageStore};
