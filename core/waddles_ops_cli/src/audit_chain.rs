//! Tamper-evident audit hash chain: canonical bytes, hashing and the incremental verifier.
//!
//! Direct port of `hub_api/services/audit_chain.py`; `tests/audit_parity.rs` replays vectors
//! produced by that module. `canonical_bytes` is
//! `b"waddles.audit.chain.v1\n" || ascii_json(sorted-key record minus record_hash)`.

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::pyjson::{dumps_canonical, PyJsonError};

/// Algorithm/serialisation identifier stored on every record.
pub const HASH_VERSION: &str = "sha256-v1";
/// `prev_hash` of the first record in every chain.
pub const GENESIS_HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";
/// Domain-separation prefix mixed into every digest.
pub const DOMAIN_PREFIX: &[u8] = b"waddles.audit.chain.v1\n";

/// One audit record exactly as hashed (`occurred_at` is the canonical UTC string).
#[derive(Debug, Clone, PartialEq)]
pub struct ChainRecord {
    pub chain_id: String,
    pub seq: i64,
    pub event_id: String,
    pub occurred_at: String,
    pub actor_uuid: Option<String>,
    pub actor_kind: String,
    pub category: String,
    pub action: String,
    pub outcome: String,
    pub target_type: Option<String>,
    pub target_id: Option<String>,
    pub details: Map<String, Value>,
    pub prev_hash: String,
    pub record_hash: String,
    pub hash_version: String,
}

/// Why a chain failed verification (first break found, in `seq` order).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BreakReason {
    ChainIdMismatch,
    HashVersionUnsupported,
    SeqGap,
    PrevHashMismatch,
    HashMismatch,
    MalformedHash,
    HeadMismatch,
}

impl BreakReason {
    /// The stable string used in CLI output (identical to the Python enum values).
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ChainIdMismatch => "chain_id_mismatch",
            Self::HashVersionUnsupported => "hash_version_unsupported",
            Self::SeqGap => "seq_gap",
            Self::PrevHashMismatch => "prev_hash_mismatch",
            Self::HashMismatch => "hash_mismatch",
            Self::MalformedHash => "malformed_hash",
            Self::HeadMismatch => "head_mismatch",
        }
    }
}

/// Three-valued outcome; `Empty` is deliberately not `Intact`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChainStatus {
    Intact,
    Broken,
    Empty,
}

impl ChainStatus {
    /// The stable string used in CLI output.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Intact => "intact",
            Self::Broken => "broken",
            Self::Empty => "empty",
        }
    }
}

/// First point at which a chain stopped verifying.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChainBreak {
    pub seq: Option<i64>,
    pub reason: BreakReason,
    pub detail: String,
}

/// Result of verifying a run of records.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChainVerification {
    pub status: ChainStatus,
    pub examined: u64,
    pub head_seq: Option<i64>,
    pub head_hash: Option<String>,
    pub break_: Option<ChainBreak>,
}

/// True for a lowercase 64-char hex SHA-256 digest.
pub fn is_hex_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn opt_str(v: &Option<String>) -> Value {
    v.as_ref().map_or(Value::Null, |s| Value::String(s.clone()))
}

/// Serialises every audited field deterministically (sorted keys, no whitespace, ASCII).
///
/// `record_hash` is excluded (it is the digest of this output).
pub fn canonical_bytes(r: &ChainRecord) -> Result<Vec<u8>, PyJsonError> {
    let mut m = Map::new();
    m.insert("action".into(), Value::String(r.action.clone()));
    m.insert("actor_kind".into(), Value::String(r.actor_kind.clone()));
    m.insert("actor_uuid".into(), opt_str(&r.actor_uuid));
    m.insert("category".into(), Value::String(r.category.clone()));
    m.insert("chain_id".into(), Value::String(r.chain_id.clone()));
    m.insert("details".into(), Value::Object(r.details.clone()));
    m.insert("event_id".into(), Value::String(r.event_id.clone()));
    m.insert("hash_version".into(), Value::String(r.hash_version.clone()));
    m.insert("occurred_at".into(), Value::String(r.occurred_at.clone()));
    m.insert("outcome".into(), Value::String(r.outcome.clone()));
    m.insert("prev_hash".into(), Value::String(r.prev_hash.clone()));
    m.insert("seq".into(), Value::from(r.seq));
    m.insert("target_id".into(), opt_str(&r.target_id));
    m.insert("target_type".into(), opt_str(&r.target_type));
    let body = dumps_canonical(&Value::Object(m))?;
    let mut out = DOMAIN_PREFIX.to_vec();
    out.extend_from_slice(body.as_bytes());
    Ok(out)
}

/// Lowercase-hex SHA-256 of the record's canonical bytes.
pub fn compute_hash(r: &ChainRecord) -> Result<String, PyJsonError> {
    let digest = Sha256::digest(canonical_bytes(r)?);
    Ok(digest.iter().map(|b| format!("{b:02x}")).collect())
}

/// Incremental verifier: feed records in `seq` order; stops at the first break.
pub struct ChainVerifier {
    chain_id: String,
    pub examined: u64,
    pub head_seq: Option<i64>,
    pub head_hash: Option<String>,
    pub broken: Option<ChainBreak>,
    expected_seq: i64,
    expected_prev: String,
}

/// Invalid verifier start anchor (mirrors the Python `ValueError`s).
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct VerifierInitError(&'static str);

impl ChainVerifier {
    /// Starts at genesis (`start_seq = 1`) or mid-chain from a trusted anchor.
    pub fn new(
        chain_id: &str,
        start_seq: i64,
        start_prev_hash: &str,
    ) -> Result<Self, VerifierInitError> {
        if start_seq < 1 {
            return Err(VerifierInitError("start_seq must be >= 1"));
        }
        if !is_hex_digest(start_prev_hash) {
            return Err(VerifierInitError(
                "start_prev_hash must be a 64-char lowercase hex SHA-256 digest",
            ));
        }
        if start_seq == 1 && start_prev_hash != GENESIS_HASH {
            return Err(VerifierInitError(
                "a chain starting at seq 1 must start from GENESIS_HASH",
            ));
        }
        Ok(Self {
            chain_id: chain_id.to_string(),
            examined: 0,
            head_seq: None,
            head_hash: None,
            broken: None,
            expected_seq: start_seq,
            expected_prev: start_prev_hash.to_string(),
        })
    }

    /// Genesis verifier for `chain_id`.
    pub fn genesis(chain_id: &str) -> Self {
        Self {
            chain_id: chain_id.to_string(),
            examined: 0,
            head_seq: None,
            head_hash: None,
            broken: None,
            expected_seq: 1,
            expected_prev: GENESIS_HASH.to_string(),
        }
    }

    fn fail(&mut self, r: &ChainRecord, reason: BreakReason, detail: String) {
        self.broken = Some(ChainBreak {
            seq: Some(r.seq),
            reason,
            detail,
        });
    }

    /// Checks one record; returns true if the chain still holds.
    pub fn feed(&mut self, r: &ChainRecord) -> bool {
        if self.broken.is_some() {
            return false;
        }
        self.examined += 1;
        if r.chain_id != self.chain_id {
            self.fail(
                r,
                BreakReason::ChainIdMismatch,
                "record belongs to another chain".into(),
            );
        } else if r.hash_version != HASH_VERSION {
            self.fail(
                r,
                BreakReason::HashVersionUnsupported,
                format!("unsupported hash_version {}", py_repr(&r.hash_version)),
            );
        } else if r.seq != self.expected_seq {
            let d = format!("expected seq {}, found {}", self.expected_seq, r.seq);
            self.fail(r, BreakReason::SeqGap, d);
        } else if !(is_hex_digest(&r.prev_hash) && is_hex_digest(&r.record_hash)) {
            self.fail(
                r,
                BreakReason::MalformedHash,
                "prev_hash/record_hash not SHA-256 hex".into(),
            );
        } else if r.prev_hash != self.expected_prev {
            self.fail(
                r,
                BreakReason::PrevHashMismatch,
                "prev_hash does not match the preceding record's hash".into(),
            );
        } else {
            // A record that cannot even be serialized canonically cannot match its stored
            // hash: report it as altered (fail-closed), never as intact.
            let matches = compute_hash(r).is_ok_and(|h| h == r.record_hash);
            if !matches {
                self.fail(
                    r,
                    BreakReason::HashMismatch,
                    "recomputed hash differs from stored record_hash (record was altered)".into(),
                );
            }
        }
        if self.broken.is_some() {
            return false;
        }
        self.expected_seq = r.seq + 1;
        self.expected_prev = r.record_hash.clone();
        self.head_seq = Some(r.seq);
        self.head_hash = Some(r.record_hash.clone());
        true
    }

    /// Closes the run, optionally checking the head against an externally pinned anchor.
    ///
    /// `expected_head_seq` is `i128` so an absurdly large pin (Python ints are unbounded)
    /// still compares unequal instead of failing to parse.
    pub fn finish(
        mut self,
        expected_head_seq: Option<i128>,
        expected_head_hash: Option<&str>,
    ) -> ChainVerification {
        if self.broken.is_none() && self.examined > 0 {
            if let Some(pin) = expected_head_seq {
                if self.head_seq.map(i128::from) != Some(pin) {
                    self.broken = Some(ChainBreak {
                        seq: self.head_seq,
                        reason: BreakReason::HeadMismatch,
                        detail: format!(
                            "head seq {} != pinned seq {} (records removed from the tail, or appended since the pin)",
                            self.head_seq.map_or("None".to_string(), |s| s.to_string()),
                            pin
                        ),
                    });
                }
            }
            if self.broken.is_none() {
                if let Some(pin) = expected_head_hash {
                    // Python `elif`: reached only when the seq pin is absent or matched.
                    if self.head_hash.as_deref() != Some(pin) {
                        self.broken = Some(ChainBreak {
                            seq: self.head_seq,
                            reason: BreakReason::HeadMismatch,
                            detail: "head hash differs from the pinned head hash".into(),
                        });
                    }
                }
            }
        }
        if let Some(b) = self.broken {
            return ChainVerification {
                status: ChainStatus::Broken,
                examined: self.examined,
                head_seq: self.head_seq,
                head_hash: self.head_hash,
                break_: Some(b),
            };
        }
        if self.examined == 0 {
            // Python truthiness: a pinned seq of 0 or empty hash is "not pinned".
            let pinned = expected_head_seq.is_some_and(|s| s != 0)
                || expected_head_hash.is_some_and(|h| !h.is_empty());
            if pinned {
                return ChainVerification {
                    status: ChainStatus::Broken,
                    examined: 0,
                    head_seq: None,
                    head_hash: None,
                    break_: Some(ChainBreak {
                        seq: None,
                        reason: BreakReason::HeadMismatch,
                        detail: "chain is empty but a head was pinned".into(),
                    }),
                };
            }
            return ChainVerification {
                status: ChainStatus::Empty,
                examined: 0,
                head_seq: None,
                head_hash: None,
                break_: None,
            };
        }
        ChainVerification {
            status: ChainStatus::Intact,
            examined: self.examined,
            head_seq: self.head_seq,
            head_hash: self.head_hash,
            break_: None,
        }
    }
}

/// Minimal Python `repr()` of a `str` (single quotes unless the text has `'` and no `"`).
fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Verifies an in-memory run of records in one call (stops at the first break).
pub fn verify_records(
    records: &[ChainRecord],
    chain_id: &str,
    expected_head_seq: Option<i128>,
    expected_head_hash: Option<&str>,
) -> ChainVerification {
    let mut v = ChainVerifier::genesis(chain_id);
    for r in records {
        if !v.feed(r) {
            break;
        }
    }
    v.finish(expected_head_seq, expected_head_hash)
}
