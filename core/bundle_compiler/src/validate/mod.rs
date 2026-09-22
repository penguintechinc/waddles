//! Component import/export validation: `wasm-tools component wit` parsing
//! plus the per-language WASI import allowlist (V31), and the
//! egress-vs-`waddle:bundle/http` cross-check (V22/V25).
//!
//! **STUBBED THIS WAVE** -- see the M2a plan's Tasks 11-12. This module
//! deliberately has no public items yet rather than a fake
//! always-succeeds implementation: `crate::artifact` (Task 13) does not
//! call into it until the real validator lands, so there is no caller to
//! mislead.
