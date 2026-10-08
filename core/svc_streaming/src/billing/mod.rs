//! Transcode-token billing admission -- ported from the Python alpha's
//! `services/token_ledger_client.py`. See [`token_ledger`] for the client
//! and [`crate::api::lifecycle`] for where it's wired into the control-plane
//! `/start` path.

pub mod token_ledger;
