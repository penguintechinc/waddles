//! Publisher-only re-validation and dual digest computation (component +
//! precompiled `.cwasm`) -- the sole producer of `app_versions.artifact_digest`
//! anywhere in the system (M2a plan Artifact & Digest Contract).
//!
//! **STUBBED THIS WAVE** -- see the M2a plan's Task 13. Runs only inside
//! the trusted `publisher` container, never `build`; deliberately no
//! public items yet rather than a fake digest implementation, since a
//! wrong digest here is a security property violation, not a cosmetic
//! gap.
