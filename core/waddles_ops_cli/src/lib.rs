//! Rust operator tooling superseding `hub_api/cli/{sign_approved_bundles,verify_audit_chain,
//! suppression}.py`. Security-sensitive: behaviour and bytes must stay identical to the Python
//! (see `tests/*_parity.rs`, vectors from `scripts/gen_ops_cli_vectors.py`).

pub mod audit_chain;
pub mod audit_cli;
pub mod db;
pub mod pyjson;
pub mod pystr;
pub mod sign_backfill;
pub mod signing;
pub mod storage;
pub mod suppression;
pub mod suppression_cli;
