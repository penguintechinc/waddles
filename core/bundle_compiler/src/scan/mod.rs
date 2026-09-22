//! Security scanning orchestration for the untrusted `build` container.
//!
//! Every scanner here runs on **inert source** -- text and dependency
//! manifests, never executed -- and every one of them must complete
//! before `build/mod.rs::run_build` invokes a per-language toolchain that
//! *does* execute bundle-supplied code (`componentize-py`'s build-time
//! dry run, `cargo component`'s build-script execution, `jco`'s
//! bundler). That ordering is D34, a security gate, not a nicety: see
//! `run_build`'s doc comment for the enforced sequence.

/// Rejects legacy `flask_core.database`/`pydal` imports (D21b).
pub mod legacy_dal;
/// Orchestrates semgrep (SAST), a language-appropriate dependency audit,
/// and gitleaks (secrets) -- each with a non-zero-denominator gate.
pub mod sast;
/// Hands off to PenguinTech's own scanner (Skauswatch), when configured.
pub mod skauswatch;
