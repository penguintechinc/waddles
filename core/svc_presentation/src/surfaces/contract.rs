//! The embedded bundle-surface contract (`surfaces.v1.json`): which surface
//! ids a bundle may publish to, each one's JSON Schema, payload size cap,
//! rate limit and replay policy.
//!
//! The JSON is the single source of truth, shared with bundle authors at
//! `bundles/contracts/surfaces.v1.json` (a unit test pins the two copies
//! byte-identical; the copy under `src/` exists only because the service's
//! Docker build context is `core/`). Schemas are compiled once at startup
//! with remote `$ref` resolution compiled out (`jsonschema` is built without
//! `resolve-http`/`resolve-file`), so a hostile schema can never make the
//! service fetch anything.

use std::collections::HashMap;

use serde::Deserialize;
use serde_json::Value;

/// The contract JSON, embedded at compile time.
pub const CONTRACT_JSON: &str = include_str!("surfaces.v1.json");

/// Why the contract could not be loaded. Startup fails loudly on any of these.
#[derive(Debug, thiserror::Error)]
pub enum ContractError {
    /// The document is not valid JSON of the expected shape.
    #[error("surface contract is malformed: {0}")]
    Malformed(String),
    /// One surface's schema failed to compile.
    #[error("surface {surface}'s schema does not compile: {reason}")]
    BadSchema {
        /// The offending surface id.
        surface: String,
        /// The compiler's message.
        reason: String,
    },
    /// A limit in the contract is unusable (zero, or above the hard cap).
    #[error("surface {surface}: {reason}")]
    BadLimit {
        /// The offending surface id.
        surface: String,
        /// What is wrong.
        reason: &'static str,
    },
}

/// One schema violation, reduced to a location and a keyword so the response
/// never echoes the offending (possibly hostile) value back.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Violation {
    /// JSON-pointer location inside the payload (`""` = the root).
    pub path: String,
    /// The schema keyword that failed (`required`, `maxLength`, ...).
    pub rule: String,
}

#[derive(Debug, Deserialize)]
struct RawContract {
    limits: RawLimits,
    #[serde(default)]
    definitions: Value,
    surfaces: HashMap<String, RawSurface>,
}

#[derive(Debug, Deserialize)]
struct RawLimits {
    hard_max_payload_bytes: usize,
    replay_max_entries: usize,
    replay_ttl_seconds: u64,
}

#[derive(Debug, Deserialize)]
struct RawSurface {
    replay: bool,
    max_payload_bytes: usize,
    rate_limit_per_minute: u32,
    schema: Value,
}

/// Contract-wide limits.
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    /// No surface may declare a payload cap above this.
    pub hard_max_payload_bytes: usize,
    /// Upper bound on cached replay entries across all communities.
    pub replay_max_entries: usize,
    /// A cached replay entry older than this is not replayed.
    pub replay_ttl_seconds: u64,
}

/// One surface: policy plus its compiled schema.
pub struct SurfaceSpec {
    id: String,
    replay: bool,
    max_payload_bytes: usize,
    rate_limit_per_minute: u32,
    validator: jsonschema::Validator,
}

impl SurfaceSpec {
    /// The surface id (`music.now_playing`).
    pub fn id(&self) -> &str {
        &self.id
    }

    /// Whether the last payload is cached and replayed to a new subscriber.
    pub fn replay(&self) -> bool {
        self.replay
    }

    /// Maximum accepted payload size in bytes.
    pub fn max_payload_bytes(&self) -> usize {
        self.max_payload_bytes
    }

    /// Publishes allowed per (community, surface) per minute.
    pub fn rate_limit_per_minute(&self) -> u32 {
        self.rate_limit_per_minute
    }

    /// Validates `payload`, returning every violation as (path, rule) only.
    pub fn validate(&self, payload: &Value) -> Result<(), Vec<Violation>> {
        let violations: Vec<Violation> = self
            .validator
            .iter_errors(payload)
            .take(16)
            .map(|err| Violation {
                path: err.instance_path().to_string(),
                rule: err.kind().keyword().to_string(),
            })
            .collect();
        if violations.is_empty() {
            Ok(())
        } else {
            Err(violations)
        }
    }
}

/// The loaded, compiled contract.
pub struct SurfaceContract {
    limits: Limits,
    surfaces: HashMap<String, SurfaceSpec>,
}

impl SurfaceContract {
    /// Router-level request body ceiling (axum needs it at build time); a
    /// unit test pins `hard_max_payload_bytes <= HARD_BODY_LIMIT`. Per-surface
    /// caps are enforced exactly in `SurfaceService::process`.
    pub const HARD_BODY_LIMIT: usize = 16 * 1024 + 1024;

    /// Loads the embedded contract; fails loudly on any malformed part.
    pub fn load() -> Result<Self, ContractError> {
        Self::from_json(CONTRACT_JSON)
    }

    /// Loads a contract from `json` (tests use this for negative cases).
    pub fn from_json(json: &str) -> Result<Self, ContractError> {
        let raw: RawContract =
            serde_json::from_str(json).map_err(|e| ContractError::Malformed(e.to_string()))?;
        if raw.surfaces.is_empty() {
            return Err(ContractError::Malformed("no surfaces declared".into()));
        }
        let limits = Limits {
            hard_max_payload_bytes: raw.limits.hard_max_payload_bytes,
            replay_max_entries: raw.limits.replay_max_entries,
            replay_ttl_seconds: raw.limits.replay_ttl_seconds,
        };
        if limits.hard_max_payload_bytes == 0
            || limits.replay_max_entries == 0
            || limits.replay_ttl_seconds == 0
        {
            return Err(ContractError::Malformed("limits must be non-zero".into()));
        }
        let mut surfaces = HashMap::with_capacity(raw.surfaces.len());
        for (id, surface) in raw.surfaces {
            if surface.max_payload_bytes == 0
                || surface.max_payload_bytes > limits.hard_max_payload_bytes
            {
                return Err(ContractError::BadLimit {
                    surface: id,
                    reason: "max_payload_bytes must be in 1..=hard_max_payload_bytes",
                });
            }
            if surface.rate_limit_per_minute == 0 {
                return Err(ContractError::BadLimit {
                    surface: id,
                    reason: "rate_limit_per_minute must be non-zero",
                });
            }
            // Every schema sees the shared `definitions`, so `$ref:
            // "#/definitions/..."` resolves without any external lookup.
            let mut schema = surface.schema;
            if let Some(obj) = schema.as_object_mut() {
                obj.insert("definitions".to_string(), raw.definitions.clone());
            }
            let validator =
                jsonschema::validator_for(&schema).map_err(|e| ContractError::BadSchema {
                    surface: id.clone(),
                    reason: e.to_string(),
                })?;
            surfaces.insert(
                id.clone(),
                SurfaceSpec {
                    id,
                    replay: surface.replay,
                    max_payload_bytes: surface.max_payload_bytes,
                    rate_limit_per_minute: surface.rate_limit_per_minute,
                    validator,
                },
            );
        }
        Ok(Self { limits, surfaces })
    }

    /// Contract-wide limits.
    pub fn limits(&self) -> Limits {
        self.limits
    }

    /// The surface named `id`, if the contract declares it.
    pub fn get(&self, id: &str) -> Option<&SurfaceSpec> {
        self.surfaces.get(id)
    }

    /// Every declared surface id, sorted (stable for tests and docs).
    pub fn ids(&self) -> Vec<&str> {
        let mut ids: Vec<&str> = self.surfaces.keys().map(String::as_str).collect();
        ids.sort_unstable();
        ids
    }
}
