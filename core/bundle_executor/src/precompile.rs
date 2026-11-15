// SPDX-License-Identifier: AGPL-3.0-or-later
//! `bundle-executor --precompile`: ahead-of-time compile approved bundle
//! components into signed `.cwasm` artifacts (executor-scale plan task 4).
//!
//! Flow per item, all fail-closed:
//! 1. Fetch `component_key` + `sidecar_key` from the bundle bucket and run
//!    the SAME verification the runtime `load` path runs: sha256 digest
//!    match (`crate::invoke::verify_digest`) and the platform Ed25519
//!    sidecar signature (`crate::signing::verify_artifact_signature`).
//! 2. Compile with an `Engine` built from the SHARED
//!    `crate::engine_config::engine_config()` -- byte-identical to the
//!    executor's -- and key the artifact by `engine_fingerprint`.
//! 3. Sign the artifact with a DEDICATED precompile Ed25519 key (never the
//!    bundle-approval key: compromise of one must not forge the other) and
//!    upload `precompiled/<engine_fp>/<digest_hex>.cwasm`, then the
//!    detached `.cwasm.sig`. The signature is written LAST so it acts as
//!    the commit marker: a reader never sees a signature without its
//!    artifact.
//!
//! Why the signature matters: `Component::deserialize` of `.cwasm` bytes is
//! `unsafe` -- it maps native code. An unsigned or forged artifact is code
//! execution inside the executor. [`verify_precompiled`] is the check the
//! executor's AOT load path MUST pass before deserializing; it binds the
//! signature to (digest, engine fingerprint, sha256 of the artifact bytes).
//!
//! Idempotency: an item whose artifact already exists under the current
//! fingerprint AND verifies against this precompiler's key is skipped, so
//! re-running a queue export or the backfill is safe and cheap.
//!
//! Secrets: the signing key is read from a FILE (`PRECOMPILE_SIGNING_KEY_FILE`,
//! a mounted K8s secret) -- never argv, never logged; [`PrecompileSigner`]'s
//! `Debug` redacts it.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use clap::Parser;
use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use wasmtime::Engine;

use crate::bucket::{BucketConfig, SecretAccessKey};
use crate::engine_config::{engine_config, engine_fingerprint, is_valid_fingerprint};
use crate::error::ExecutorError;
use crate::invoke::verify_digest;
use crate::signing::{verify_artifact_signature, write_length_prefixed, PlatformPublicKeys};

/// Domain separator for precompiled-artifact signatures -- distinct from
/// `crate::signing::DOMAIN_SEPARATOR` so a bundle-approval signature can
/// never be replayed as an artifact signature or vice versa.
pub const CWASM_DOMAIN_SEPARATOR: &[u8] = b"waddles-cwasm-sig-v1";

/// Object-key prefix every precompiled artifact lives under. The
/// precompiler's bucket credentials are scoped to write ONLY this prefix.
pub const PRECOMPILED_PREFIX: &str = "precompiled";

/// The detached signature document stored at `<artifact>.sig`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CwasmSignature {
    /// Bare lowercase hex sha256 of the source `.wasm` component.
    pub digest: String,
    pub engine_fingerprint: String,
    /// Lowercase hex sha256 of the `.cwasm` bytes this signature covers.
    pub cwasm_sha256: String,
    pub key_id: String,
    pub algorithm: String,
    /// Base64 64-byte Ed25519 signature over [`cwasm_signing_payload`].
    pub signature: String,
}

/// The exact bytes signed: domain separator + length-prefixed digest,
/// fingerprint and artifact hash (length prefixes rule out field-boundary
/// ambiguity).
pub fn cwasm_signing_payload(digest_hex: &str, engine_fp: &str, cwasm_sha256: &str) -> Vec<u8> {
    let mut buf = Vec::with_capacity(CWASM_DOMAIN_SEPARATOR.len() + 12 + 64 * 3);
    buf.extend_from_slice(CWASM_DOMAIN_SEPARATOR);
    write_length_prefixed(&mut buf, digest_hex.as_bytes());
    write_length_prefixed(&mut buf, engine_fp.as_bytes());
    write_length_prefixed(&mut buf, cwasm_sha256.as_bytes());
    buf
}

/// Normalizes `sha256:<hex>` or bare `<hex>` into bare lowercase hex,
/// refusing anything that is not exactly 64 hex chars (it is interpolated
/// into an object key, so it must never carry `/` or `..`).
pub fn normalize_digest_hex(digest: &str) -> Result<String, ExecutorError> {
    let hex = digest
        .strip_prefix("sha256:")
        .unwrap_or(digest)
        .to_ascii_lowercase();
    if hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        Ok(hex)
    } else {
        Err(ExecutorError::MalformedDigest(digest.to_string()))
    }
}

/// `(artifact_key, signature_key)` for one digest under one engine
/// fingerprint: `precompiled/<fp>/<digest>.cwasm` + `.cwasm.sig`.
pub fn object_keys(engine_fp: &str, digest: &str) -> Result<(String, String), ExecutorError> {
    if !is_valid_fingerprint(engine_fp) {
        return Err(ExecutorError::Config(format!(
            "engine fingerprint {engine_fp:?} is not 64 lowercase hex chars"
        )));
    }
    let hex = normalize_digest_hex(digest)?;
    let artifact = format!("{PRECOMPILED_PREFIX}/{engine_fp}/{hex}.cwasm");
    let sig = format!("{artifact}.sig");
    Ok((artifact, sig))
}

fn sha256_hex(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}

/// Verifies a precompiled artifact before it may be deserialized. Fails
/// closed on: malformed signature JSON, wrong algorithm, a digest or
/// engine fingerprint different from what the caller expects (stale or
/// swapped artifact), an artifact whose bytes do not hash to the signed
/// value, an unknown key id, or a signature that does not verify.
pub fn verify_precompiled(
    cwasm: &[u8],
    sig_bytes: &[u8],
    keys: &PlatformPublicKeys,
    expected_digest: &str,
    expected_engine_fp: &str,
) -> Result<(), ExecutorError> {
    let sig: CwasmSignature = serde_json::from_slice(sig_bytes).map_err(|e| {
        ExecutorError::SignatureInvalid(format!("cwasm signature is not valid JSON: {e}"))
    })?;
    if sig.algorithm != "ed25519" {
        return Err(ExecutorError::SignatureInvalid(format!(
            "unsupported cwasm signature algorithm {:?}",
            sig.algorithm
        )));
    }
    let expected_hex = normalize_digest_hex(expected_digest)?;
    if sig.digest != expected_hex {
        return Err(ExecutorError::SignatureInvalid(format!(
            "cwasm signature digest {:?} does not match expected {expected_hex:?}",
            sig.digest
        )));
    }
    if sig.engine_fingerprint != expected_engine_fp {
        return Err(ExecutorError::SignatureInvalid(format!(
            "cwasm engine fingerprint {:?} does not match this engine's {expected_engine_fp:?}",
            sig.engine_fingerprint
        )));
    }
    let actual = sha256_hex(cwasm);
    if actual != sig.cwasm_sha256 {
        return Err(ExecutorError::SignatureInvalid(
            "cwasm bytes do not match the signed artifact hash".to_string(),
        ));
    }
    let key = keys.get(&sig.key_id).ok_or_else(|| {
        ExecutorError::SignatureInvalid(format!("unknown precompile key id {:?}", sig.key_id))
    })?;
    let raw = BASE64.decode(sig.signature.as_bytes()).map_err(|e| {
        ExecutorError::SignatureInvalid(format!("cwasm signature is not valid base64: {e}"))
    })?;
    let arr: [u8; 64] = raw.as_slice().try_into().map_err(|_| {
        ExecutorError::SignatureInvalid(format!(
            "cwasm signature must be 64 bytes, got {}",
            raw.len()
        ))
    })?;
    let payload = cwasm_signing_payload(&sig.digest, &sig.engine_fingerprint, &sig.cwasm_sha256);
    key.verify_strict(&payload, &Signature::from_bytes(&arr))
        .map_err(|e| ExecutorError::SignatureInvalid(format!("cwasm signature invalid: {e}")))
}

/// The precompiler's Ed25519 signing key + its id. `Debug` never prints
/// key material.
pub struct PrecompileSigner {
    key_id: String,
    key: SigningKey,
}

impl std::fmt::Debug for PrecompileSigner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "PrecompileSigner {{ key_id: {:?}, key: ***redacted*** }}",
            self.key_id
        )
    }
}

impl PrecompileSigner {
    /// Builds a signer from a raw 32-byte seed.
    pub fn from_seed(key_id: &str, seed: [u8; 32]) -> Result<Self, ExecutorError> {
        let ok = !key_id.is_empty()
            && key_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'));
        if !ok {
            return Err(ExecutorError::Config(format!(
                "PRECOMPILE_SIGNING_KEY_ID {key_id:?} must be non-empty [A-Za-z0-9._-]"
            )));
        }
        Ok(Self {
            key_id: key_id.to_string(),
            key: SigningKey::from_bytes(&seed),
        })
    }

    /// Reads a base64-encoded 32-byte seed from `path` (a mounted secret).
    /// Error messages name the path, never the contents.
    pub fn from_file(key_id: &str, path: &Path) -> Result<Self, ExecutorError> {
        let raw = std::fs::read_to_string(path).map_err(|e| {
            ExecutorError::Config(format!(
                "cannot read precompile signing key file {path:?}: {e}"
            ))
        })?;
        let bytes = BASE64.decode(raw.trim().as_bytes()).map_err(|_| {
            ExecutorError::Config(format!(
                "precompile signing key file {path:?} is not valid base64"
            ))
        })?;
        let seed: [u8; 32] = bytes.as_slice().try_into().map_err(|_| {
            ExecutorError::Config(format!(
                "precompile signing key file {path:?} must decode to exactly 32 bytes"
            ))
        })?;
        Self::from_seed(key_id, seed)
    }

    pub fn key_id(&self) -> &str {
        &self.key_id
    }

    pub fn verifying_key(&self) -> VerifyingKey {
        self.key.verifying_key()
    }

    /// Base64 of the public key -- what operators put in the executor's
    /// `PRECOMPILE_SIGNING_PUBLIC_KEYS` JSON map.
    pub fn public_key_b64(&self) -> String {
        BASE64.encode(self.verifying_key().as_bytes())
    }

    /// The key set containing only this signer's public key.
    pub fn own_key_set(&self) -> PlatformPublicKeys {
        let mut m = HashMap::new();
        m.insert(self.key_id.clone(), self.verifying_key());
        PlatformPublicKeys::from_keys(m)
    }

    /// Produces the serialized [`CwasmSignature`] for `cwasm`.
    pub fn sign(
        &self,
        digest: &str,
        engine_fp: &str,
        cwasm: &[u8],
    ) -> Result<Vec<u8>, ExecutorError> {
        let digest_hex = normalize_digest_hex(digest)?;
        let cwasm_sha256 = sha256_hex(cwasm);
        let payload = cwasm_signing_payload(&digest_hex, engine_fp, &cwasm_sha256);
        let doc = CwasmSignature {
            digest: digest_hex,
            engine_fingerprint: engine_fp.to_string(),
            cwasm_sha256,
            key_id: self.key_id.clone(),
            algorithm: "ed25519".to_string(),
            signature: BASE64.encode(self.key.sign(&payload).to_bytes()),
        };
        serde_json::to_vec(&doc)
            .map_err(|e| ExecutorError::Config(format!("cannot serialize cwasm signature: {e}")))
    }
}

/// Object-storage operations the precompiler needs. The production impl is
/// [`BucketStore`]; tests use an in-memory store.
pub trait ArtifactStore: Send + Sync {
    /// Fetches the source component and its signed sidecar.
    fn fetch_bundle(
        &self,
        component_key: &str,
        sidecar_key: &str,
    ) -> impl std::future::Future<Output = Result<(Vec<u8>, Vec<u8>), ExecutorError>> + Send;
    /// `Ok(None)` when the key does not exist; any other failure is an error.
    fn get_optional(
        &self,
        key: &str,
    ) -> impl std::future::Future<Output = Result<Option<Vec<u8>>, ExecutorError>> + Send;
    fn put(
        &self,
        key: &str,
        body: &[u8],
    ) -> impl std::future::Future<Output = Result<(), ExecutorError>> + Send;
}

/// The SigV4 bucket store (same hand-rolled client as the runtime fetch;
/// no general-purpose HTTP client enters this binary).
pub struct BucketStore {
    config: BucketConfig,
    max_artifact_bytes: u64,
}

impl BucketStore {
    pub fn new(config: BucketConfig, max_artifact_bytes: u64) -> Self {
        Self {
            config,
            max_artifact_bytes,
        }
    }
}

impl ArtifactStore for BucketStore {
    async fn fetch_bundle(
        &self,
        component_key: &str,
        sidecar_key: &str,
    ) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
        let component = crate::bucket::get_object_optional(
            &self.config,
            component_key,
            self.config.max_component_bytes,
        )
        .await?
        .ok_or_else(|| {
            ExecutorError::BucketFetch(format!("component {component_key:?} not found"))
        })?;
        let sidecar = crate::bucket::get_object_optional(&self.config, sidecar_key, 1024 * 1024)
            .await?
            .ok_or_else(|| {
                ExecutorError::BucketFetch(format!("sidecar {sidecar_key:?} not found"))
            })?;
        Ok((component, sidecar))
    }

    async fn get_optional(&self, key: &str) -> Result<Option<Vec<u8>>, ExecutorError> {
        crate::bucket::get_object_optional(&self.config, key, self.max_artifact_bytes).await
    }

    async fn put(&self, key: &str, body: &[u8]) -> Result<(), ExecutorError> {
        crate::bucket::put_object(&self.config, key, body).await
    }
}

/// One bundle version to precompile -- the row shape hub-api's
/// `cli/precompile_queue.py export` writes (from `app_versions`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PrecompileItem {
    pub app_id: String,
    pub version: String,
    pub digest: String,
    pub component_key: String,
    pub sidecar_key: String,
}

/// What happened to one item.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ItemStatus {
    Compiled,
    Skipped,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ItemResult {
    pub app_id: String,
    pub version: String,
    pub digest: String,
    pub status: ItemStatus,
    /// Error text for `Failed` (never contains key material or bundle bytes).
    pub error: Option<String>,
}

/// The report `--report` writes; hub-api's `ack` step consumes it.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PrecompileReport {
    pub engine_fingerprint: String,
    pub examined: usize,
    pub compiled: usize,
    pub skipped: usize,
    pub failed: usize,
    pub results: Vec<ItemResult>,
}

/// Everything one precompile run needs, independent of where it came from.
pub struct Precompiler<'a, S: ArtifactStore> {
    pub engine: &'a Engine,
    pub engine_fp: &'a str,
    pub store: &'a S,
    /// Platform bundle-approval keys (`BUNDLE_SIGNING_PUBLIC_KEYS`); must
    /// be non-empty -- the precompiler never compiles an unapproved bundle.
    pub bundle_keys: &'a PlatformPublicKeys,
    pub signer: &'a PrecompileSigner,
}

impl<S: ArtifactStore> Precompiler<'_, S> {
    /// Precompiles one item; see the module doc for the steps.
    pub async fn precompile_one(&self, item: &PrecompileItem) -> Result<ItemStatus, ExecutorError> {
        if self.bundle_keys.is_empty() {
            return Err(ExecutorError::Config(
                "BUNDLE_SIGNING_PUBLIC_KEYS must be set: the precompiler never compiles an \
                 unverified bundle"
                    .to_string(),
            ));
        }
        let digest_hex = normalize_digest_hex(&item.digest)?;
        let (artifact_key, sig_key) = object_keys(self.engine_fp, &digest_hex)?;

        // Idempotency: an existing, valid artifact for this fingerprint is
        // left alone. A present-but-invalid one (key rotation, partial
        // write, tampering) is rebuilt and overwritten.
        if let Some(existing_sig) = self.store.get_optional(&sig_key).await? {
            if let Some(existing) = self.store.get_optional(&artifact_key).await? {
                match verify_precompiled(
                    &existing,
                    &existing_sig,
                    &self.signer.own_key_set(),
                    &digest_hex,
                    self.engine_fp,
                ) {
                    Ok(()) => return Ok(ItemStatus::Skipped),
                    Err(e) => tracing::warn!(
                        app_id = %item.app_id,
                        digest = %digest_hex,
                        error = %e,
                        "existing precompiled artifact failed verification; rebuilding"
                    ),
                }
            }
        }

        let cwasm =
            fetch_verify_compile(self.engine, self.store, self.bundle_keys, item, &digest_hex)
                .await?;

        let sig = self.signer.sign(&digest_hex, self.engine_fp, &cwasm)?;
        self.store.put(&artifact_key, &cwasm).await?;
        // Signature last: it is the commit marker.
        self.store.put(&sig_key, &sig).await?;
        Ok(ItemStatus::Compiled)
    }

    /// Runs every item sequentially (compile is CPU- and memory-heavy; the
    /// Job's CPU limit is the parallelism knob), never aborting the batch
    /// on one failure -- each failure is recorded and logged at ERROR.
    pub async fn run(&self, items: &[PrecompileItem]) -> PrecompileReport {
        let meter = opentelemetry::global::meter(crate::metrics::SCOPE);
        let items_total = meter
            .u64_counter("executor_precompile_items_total")
            .with_description("Bundle precompile outcomes by status")
            .build();
        let duration = meter
            .f64_histogram("executor_precompile_duration_seconds")
            .with_description("Wall time to precompile (or skip) one bundle")
            .with_unit("s")
            .build();
        let mut results = Vec::with_capacity(items.len());
        for item in items {
            let started = Instant::now();
            let outcome = self.precompile_one(item).await;
            let (status, error) = match outcome {
                Ok(s) => (s, None),
                Err(e) => {
                    tracing::error!(
                        app_id = %item.app_id,
                        version = %item.version,
                        error = %e,
                        "bundle precompile failed"
                    );
                    (ItemStatus::Failed, Some(e.to_string()))
                }
            };
            let label = [opentelemetry::KeyValue::new(
                "status",
                match status {
                    ItemStatus::Compiled => "compiled",
                    ItemStatus::Skipped => "skipped",
                    ItemStatus::Failed => "failed",
                },
            )];
            items_total.add(1, &label);
            duration.record(started.elapsed().as_secs_f64(), &label);
            results.push(ItemResult {
                app_id: item.app_id.clone(),
                version: item.version.clone(),
                digest: item.digest.clone(),
                status,
                error,
            });
        }
        let count = |s: ItemStatus| results.iter().filter(|r| r.status == s).count();
        PrecompileReport {
            engine_fingerprint: self.engine_fp.to_string(),
            examined: results.len(),
            compiled: count(ItemStatus::Compiled),
            skipped: count(ItemStatus::Skipped),
            failed: count(ItemStatus::Failed),
            results,
        }
    }
}

/// Fetches one bundle, runs the SAME digest + platform-signature checks the
/// runtime `load` path runs, then compiles it with Cranelift. Shared by the
/// combined `--precompile` mode and the key-less compile-only stage so both
/// apply identical admission checks before any untrusted bytes are compiled.
async fn fetch_verify_compile<S: ArtifactStore>(
    engine: &Engine,
    store: &S,
    bundle_keys: &PlatformPublicKeys,
    item: &PrecompileItem,
    digest_hex: &str,
) -> Result<Vec<u8>, ExecutorError> {
    let (component, sidecar) = store
        .fetch_bundle(&item.component_key, &item.sidecar_key)
        .await?;
    let prefixed = format!("sha256:{digest_hex}");
    verify_digest(&component, &prefixed)?;
    verify_artifact_signature(
        &sidecar,
        bundle_keys,
        &item.app_id,
        &item.version,
        &prefixed,
    )?;

    let engine = engine.clone();
    let started = Instant::now();
    let cwasm = tokio::task::spawn_blocking(move || engine.precompile_component(&component))
        .await
        .map_err(|e| ExecutorError::Wasmtime(format!("precompile task panicked: {e}")))?
        .map_err(|e| ExecutorError::Wasmtime(format!("precompile failed: {e}")))?;
    tracing::info!(
        app_id = %item.app_id,
        digest = %digest_hex,
        bytes = cwasm.len(),
        ms = started.elapsed().as_millis() as u64,
        "bundle precompiled"
    );
    Ok(cwasm)
}

/// Name of the compile-stage result document inside the shared work dir.
pub const COMPILED_INDEX_FILE: &str = "compiled.json";

/// Hard cap on a `.cwasm` the sign stage will read from the (untrusted)
/// compile stage's output directory.
pub const MAX_STAGED_CWASM_BYTES: u64 = 512 * 1024 * 1024;

/// One compile-stage outcome, staged for the sign stage.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CompiledEntry {
    pub item: PrecompileItem,
    pub status: ItemStatus,
    /// Error text for `Failed`.
    pub error: Option<String>,
    /// Lowercase hex sha256 of `<out-dir>/<digest>.cwasm` (`Compiled` only).
    pub cwasm_sha256: Option<String>,
}

/// The index the compile stage writes next to the staged `.cwasm` files.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CompiledIndex {
    pub engine_fingerprint: String,
    pub entries: Vec<CompiledEntry>,
}

/// File name of a staged artifact: derived ONLY from the validated digest so
/// a hostile index can never steer the sign stage to another path.
fn staged_name(digest_hex: &str) -> String {
    format!("{digest_hex}.cwasm")
}

fn status_label(s: ItemStatus) -> &'static str {
    match s {
        ItemStatus::Compiled => "compiled",
        ItemStatus::Skipped => "skipped",
        ItemStatus::Failed => "failed",
    }
}

/// Compile stage: holds NO signing key. Admits each item exactly as the
/// combined mode does, compiles it, and stages `<digest>.cwasm` plus an
/// index for the sign stage. Item failures are recorded, never fatal.
pub struct CompileStage<'a, S: ArtifactStore> {
    pub engine: &'a Engine,
    pub engine_fp: &'a str,
    pub store: &'a S,
    pub bundle_keys: &'a PlatformPublicKeys,
    /// Public keys of the precompile signer (NOT secret). When present an
    /// existing valid artifact short-circuits the compile (idempotency).
    pub existing_keys: Option<&'a PlatformPublicKeys>,
}

impl<S: ArtifactStore> CompileStage<'_, S> {
    async fn compile_item(
        &self,
        item: &PrecompileItem,
        out_dir: &Path,
    ) -> Result<CompiledEntry, ExecutorError> {
        if self.bundle_keys.is_empty() {
            return Err(ExecutorError::Config(
                "BUNDLE_SIGNING_PUBLIC_KEYS must be set: the precompiler never compiles an \
                 unverified bundle"
                    .to_string(),
            ));
        }
        let digest_hex = normalize_digest_hex(&item.digest)?;
        let (artifact_key, sig_key) = object_keys(self.engine_fp, &digest_hex)?;
        if let Some(keys) = self.existing_keys {
            if let (Some(sig), Some(art)) = (
                self.store.get_optional(&sig_key).await?,
                self.store.get_optional(&artifact_key).await?,
            ) {
                if verify_precompiled(&art, &sig, keys, &digest_hex, self.engine_fp).is_ok() {
                    return Ok(CompiledEntry {
                        item: item.clone(),
                        status: ItemStatus::Skipped,
                        error: None,
                        cwasm_sha256: None,
                    });
                }
            }
        }
        let cwasm =
            fetch_verify_compile(self.engine, self.store, self.bundle_keys, item, &digest_hex)
                .await?;
        std::fs::write(out_dir.join(staged_name(&digest_hex)), &cwasm)?;
        Ok(CompiledEntry {
            item: item.clone(),
            status: ItemStatus::Compiled,
            error: None,
            cwasm_sha256: Some(sha256_hex(&cwasm)),
        })
    }

    /// Compiles every item sequentially and writes the index.
    pub async fn run(
        &self,
        items: &[PrecompileItem],
        out_dir: &Path,
    ) -> Result<CompiledIndex, ExecutorError> {
        std::fs::create_dir_all(out_dir)?;
        let meter = opentelemetry::global::meter(crate::metrics::SCOPE);
        let counter = meter
            .u64_counter("executor_precompile_compile_items_total")
            .with_description("Compile-stage outcomes by status")
            .build();
        let mut entries = Vec::with_capacity(items.len());
        for item in items {
            let entry = match self.compile_item(item, out_dir).await {
                Ok(e) => e,
                Err(e) => {
                    tracing::error!(
                        app_id = %item.app_id,
                        version = %item.version,
                        error = %e,
                        "bundle compile failed"
                    );
                    CompiledEntry {
                        item: item.clone(),
                        status: ItemStatus::Failed,
                        error: Some(e.to_string()),
                        cwasm_sha256: None,
                    }
                }
            };
            counter.add(
                1,
                &[opentelemetry::KeyValue::new(
                    "status",
                    status_label(entry.status),
                )],
            );
            entries.push(entry);
        }
        let index = CompiledIndex {
            engine_fingerprint: self.engine_fp.to_string(),
            entries,
        };
        let body = serde_json::to_vec_pretty(&index)
            .map_err(|e| ExecutorError::Config(format!("cannot serialize compiled index: {e}")))?;
        std::fs::write(out_dir.join(COMPILED_INDEX_FILE), body)?;
        Ok(index)
    }
}

/// Sign stage: holds the signing key but NEVER compiles or loads a module --
/// it only hashes staged bytes, signs and uploads. The trusted export
/// manifest is the denominator; the (untrusted) compile index may only
/// answer for items the export listed.
pub struct SignStage<'a, S: ArtifactStore> {
    pub engine_fp: &'a str,
    pub store: &'a S,
    pub signer: &'a PrecompileSigner,
}

impl<S: ArtifactStore> SignStage<'_, S> {
    async fn sign_item(
        &self,
        item: &PrecompileItem,
        entry: Option<&CompiledEntry>,
        in_dir: &Path,
    ) -> Result<ItemStatus, ExecutorError> {
        let entry = entry.ok_or_else(|| {
            ExecutorError::Config("compile stage produced no result for this item".into())
        })?;
        match entry.status {
            ItemStatus::Skipped => return Ok(ItemStatus::Skipped),
            ItemStatus::Failed => {
                return Err(ExecutorError::Wasmtime(
                    entry
                        .error
                        .clone()
                        .unwrap_or_else(|| "compile stage reported failure".into()),
                ))
            }
            ItemStatus::Compiled => {}
        }
        let digest_hex = normalize_digest_hex(&item.digest)?;
        let want = entry.cwasm_sha256.as_deref().ok_or_else(|| {
            ExecutorError::SignatureInvalid("compiled entry has no cwasm_sha256".into())
        })?;
        let path = in_dir.join(staged_name(&digest_hex));
        let len = std::fs::metadata(&path)?.len();
        if len > MAX_STAGED_CWASM_BYTES {
            return Err(ExecutorError::Config(format!(
                "staged artifact is {len} bytes, over the {MAX_STAGED_CWASM_BYTES} cap"
            )));
        }
        let cwasm = std::fs::read(&path)?;
        if sha256_hex(&cwasm) != want {
            return Err(ExecutorError::SignatureInvalid(
                "staged artifact does not match the hash the compile stage recorded".into(),
            ));
        }
        let (artifact_key, sig_key) = object_keys(self.engine_fp, &digest_hex)?;
        let sig = self.signer.sign(&digest_hex, self.engine_fp, &cwasm)?;
        self.store.put(&artifact_key, &cwasm).await?;
        // Signature last: it is the commit marker.
        self.store.put(&sig_key, &sig).await?;
        Ok(ItemStatus::Compiled)
    }

    /// Signs and uploads every manifest item the compile stage produced.
    pub async fn run(
        &self,
        items: &[PrecompileItem],
        index: &CompiledIndex,
        in_dir: &Path,
    ) -> PrecompileReport {
        let meter = opentelemetry::global::meter(crate::metrics::SCOPE);
        let counter = meter
            .u64_counter("executor_precompile_sign_items_total")
            .with_description("Sign-stage outcomes by status")
            .build();
        let fp_ok = index.engine_fingerprint == self.engine_fp;
        let mut results = Vec::with_capacity(items.len());
        for item in items {
            let entry = index.entries.iter().find(|e| {
                e.item.digest == item.digest
                    && e.item.app_id == item.app_id
                    && e.item.version == item.version
            });
            let outcome = if fp_ok {
                self.sign_item(item, entry, in_dir).await
            } else {
                Err(ExecutorError::SignatureInvalid(format!(
                    "compile index fingerprint {:?} differs from this engine's {:?}",
                    index.engine_fingerprint, self.engine_fp
                )))
            };
            let (status, error) = match outcome {
                Ok(s) => (s, None),
                Err(e) => {
                    tracing::error!(
                        app_id = %item.app_id,
                        version = %item.version,
                        error = %e,
                        "bundle sign/upload failed"
                    );
                    (ItemStatus::Failed, Some(e.to_string()))
                }
            };
            counter.add(
                1,
                &[opentelemetry::KeyValue::new("status", status_label(status))],
            );
            results.push(ItemResult {
                app_id: item.app_id.clone(),
                version: item.version.clone(),
                digest: item.digest.clone(),
                status,
                error,
            });
        }
        let count = |s: ItemStatus| results.iter().filter(|r| r.status == s).count();
        PrecompileReport {
            engine_fingerprint: self.engine_fp.to_string(),
            examined: results.len(),
            compiled: count(ItemStatus::Compiled),
            skipped: count(ItemStatus::Skipped),
            failed: count(ItemStatus::Failed),
            results,
        }
    }
}

/// CLI for `bundle-executor --precompile ...`. Bucket credentials here are
/// the PRECOMPILER's (write-scoped to `precompiled/`), not the runtime
/// executor's read-only ones.
#[derive(Debug, Parser)]
#[command(name = "bundle-executor --precompile")]
pub struct PrecompileArgs {
    /// Marker flag selecting this mode.
    #[arg(long)]
    pub precompile: bool,

    /// Key-less compile stage: admit + compile each manifest item and stage
    /// `<digest>.cwasm` + `compiled.json` into `--out-dir`. Refuses to run
    /// when a signing key is configured.
    #[arg(long = "precompile-compile-only", default_value_t = false)]
    pub compile_only: bool,

    /// Sign stage: hash + sign + upload what `--in-dir` holds. Never compiles
    /// or loads a module.
    #[arg(long = "precompile-sign", default_value_t = false)]
    pub sign_only: bool,

    /// Compile stage output directory (shared emptyDir).
    #[arg(long, env = "PRECOMPILE_OUT_DIR")]
    pub out_dir: Option<PathBuf>,

    /// Sign stage input directory (the compile stage's `--out-dir`).
    #[arg(long, env = "PRECOMPILE_IN_DIR")]
    pub in_dir: Option<PathBuf>,

    /// Public half of the precompile signing key(s) (JSON key map). Optional
    /// in the compile stage: enables skipping already-built artifacts.
    #[arg(long, env = "PRECOMPILE_SIGNING_PUBLIC_KEYS")]
    pub precompile_signing_public_keys: Option<String>,

    /// JSON array of [`PrecompileItem`] (written by hub-api's
    /// `precompile_queue export`).
    #[arg(long, env = "PRECOMPILE_MANIFEST")]
    pub manifest: Option<PathBuf>,

    /// Where to write the [`PrecompileReport`] JSON.
    #[arg(long, env = "PRECOMPILE_REPORT")]
    pub report: Option<PathBuf>,

    /// Exit 0 even if items failed (failures are still in the report and
    /// logged at ERROR). Set by the queue Job, whose ack step fails loudly
    /// instead so successful rows are still acknowledged.
    #[arg(long, default_value_t = false)]
    pub report_failures_only: bool,

    /// Print this binary's engine fingerprint and exit.
    #[arg(long, default_value_t = false)]
    pub print_engine_fingerprint: bool,

    #[arg(long, env = "PRECOMPILE_SIGNING_KEY_FILE")]
    pub signing_key_file: Option<PathBuf>,

    #[arg(long, env = "PRECOMPILE_SIGNING_KEY_ID")]
    pub signing_key_id: Option<String>,

    /// Cap on a fetched existing `.cwasm` (default 512 MiB).
    #[arg(
        long,
        env = "PRECOMPILE_MAX_ARTIFACT_BYTES",
        default_value_t = 536_870_912
    )]
    pub max_artifact_bytes: u64,

    #[arg(long, env = "BUNDLE_SIGNING_PUBLIC_KEYS")]
    pub bundle_signing_public_keys: Option<String>,

    #[arg(long, env = "BUNDLE_BUCKET_ENDPOINT")]
    pub bucket_endpoint: Option<String>,
    #[arg(long, env = "BUNDLE_BUCKET_NAME")]
    pub bucket_name: Option<String>,
    #[arg(long, env = "BUNDLE_BUCKET_REGION", default_value = "us-east-1")]
    pub bucket_region: String,
    #[arg(long, env = "BUNDLE_BUCKET_ACCESS_KEY_ID", hide_env_values = true)]
    pub bucket_access_key_id: Option<String>,
    #[arg(long, env = "BUNDLE_BUCKET_SECRET_ACCESS_KEY", hide_env_values = true)]
    pub bucket_secret_access_key: Option<String>,
    #[arg(long, env = "BUNDLE_BUCKET_CA_FILE")]
    pub bucket_ca_file: Option<PathBuf>,
    #[arg(long, env = "BUNDLE_FETCH_TIMEOUT_S", default_value_t = 120)]
    pub fetch_timeout_s: u64,
    #[arg(
        long,
        env = "BUNDLE_MAX_COMPONENT_BYTES",
        default_value_t = 134_217_728
    )]
    pub max_component_bytes: u64,
}

fn required(name: &str, v: &Option<String>) -> Result<String, ExecutorError> {
    v.clone()
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| ExecutorError::Config(format!("{name} must be set")))
}

impl PrecompileArgs {
    fn bucket_config(&self) -> Result<BucketConfig, ExecutorError> {
        let endpoint = required("BUNDLE_BUCKET_ENDPOINT", &self.bucket_endpoint)?;
        if endpoint.starts_with("https://") && self.bucket_ca_file.is_none() {
            return Err(ExecutorError::Config(
                "BUNDLE_BUCKET_CA_FILE must be set for an https:// BUNDLE_BUCKET_ENDPOINT".into(),
            ));
        }
        Ok(BucketConfig {
            endpoint,
            bucket: required("BUNDLE_BUCKET_NAME", &self.bucket_name)?,
            region: self.bucket_region.clone(),
            access_key_id: required("BUNDLE_BUCKET_ACCESS_KEY_ID", &self.bucket_access_key_id)?,
            secret_access_key: SecretAccessKey::new(required(
                "BUNDLE_BUCKET_SECRET_ACCESS_KEY",
                &self.bucket_secret_access_key,
            )?),
            ca_file: self.bucket_ca_file.clone(),
            fetch_timeout: Duration::from_secs(self.fetch_timeout_s),
            max_component_bytes: self.max_component_bytes,
        })
    }
}

/// Entry point for `bundle-executor --precompile`. Returns the process exit
/// code: 0 success, 1 any item failed (unless `--report-failures-only`).
pub async fn run_cli(args: PrecompileArgs) -> Result<i32, ExecutorError> {
    let engine = Engine::new(&engine_config())?;
    let engine_fp = engine_fingerprint(&engine);
    // Flushed on drop when this fn returns (OTLP only if OTEL_* env is set).
    let _metrics = crate::telemetry::init_metrics("svc-bundles-precompiler");
    if args.print_engine_fingerprint {
        println!("{engine_fp}");
        return Ok(0);
    }
    let manifest_path = args.manifest.clone().ok_or_else(|| {
        ExecutorError::Config("--manifest (PRECOMPILE_MANIFEST) must be set".into())
    })?;
    let raw = std::fs::read(&manifest_path).map_err(ExecutorError::Io)?;
    let items: Vec<PrecompileItem> = serde_json::from_slice(&raw).map_err(|e| {
        ExecutorError::Config(format!(
            "precompile manifest {manifest_path:?} is invalid: {e}"
        ))
    })?;
    if args.compile_only && args.sign_only {
        return Err(ExecutorError::Config(
            "--precompile-compile-only and --precompile-sign are mutually exclusive".into(),
        ));
    }
    if args.compile_only {
        return run_compile_only(&args, &engine, &engine_fp, &items).await;
    }
    if args.sign_only {
        return run_sign_only(&args, &engine_fp, &items).await;
    }

    let key_file = args
        .signing_key_file
        .clone()
        .ok_or_else(|| ExecutorError::Config("PRECOMPILE_SIGNING_KEY_FILE must be set".into()))?;
    let key_id = required("PRECOMPILE_SIGNING_KEY_ID", &args.signing_key_id)?;
    let signer = PrecompileSigner::from_file(&key_id, &key_file)?;
    let bundle_keys = match args.bundle_signing_public_keys.as_deref().map(str::trim) {
        None | Some("") => {
            return Err(ExecutorError::Config(
                "BUNDLE_SIGNING_PUBLIC_KEYS must be set: the precompiler never compiles an \
                 unverified bundle"
                    .into(),
            ))
        }
        Some(raw) => PlatformPublicKeys::from_json(raw)?,
    };
    let store = BucketStore::new(args.bucket_config()?, args.max_artifact_bytes);

    tracing::info!(
        engine_fingerprint = %engine_fp,
        items = items.len(),
        key_id = %signer.key_id(),
        "precompile run starting"
    );
    let precompiler = Precompiler {
        engine: &engine,
        engine_fp: &engine_fp,
        store: &store,
        bundle_keys: &bundle_keys,
        signer: &signer,
    };
    let report = precompiler.run(&items).await;
    tracing::info!(
        engine_fingerprint = %engine_fp,
        examined = report.examined,
        compiled = report.compiled,
        skipped = report.skipped,
        failed = report.failed,
        "precompile run finished"
    );
    if let Some(path) = &args.report {
        let body = serde_json::to_vec_pretty(&report)
            .map_err(|e| ExecutorError::Config(format!("cannot serialize report: {e}")))?;
        std::fs::write(path, body).map_err(ExecutorError::Io)?;
    }
    if report.failed > 0 && !args.report_failures_only {
        return Ok(1);
    }
    Ok(0)
}

/// Parses the platform bundle-approval keys, refusing an empty set.
fn parse_bundle_keys(args: &PrecompileArgs) -> Result<PlatformPublicKeys, ExecutorError> {
    match args.bundle_signing_public_keys.as_deref().map(str::trim) {
        None | Some("") => Err(ExecutorError::Config(
            "BUNDLE_SIGNING_PUBLIC_KEYS must be set: the precompiler never compiles an \
             unverified bundle"
                .into(),
        )),
        Some(raw) => PlatformPublicKeys::from_json(raw),
    }
}

/// `--precompile-compile-only`: Cranelift runs here, in a process that must
/// not hold the signing key (a compiler escape would otherwise reach it).
async fn run_compile_only(
    args: &PrecompileArgs,
    engine: &Engine,
    engine_fp: &str,
    items: &[PrecompileItem],
) -> Result<i32, ExecutorError> {
    if args.signing_key_file.is_some() || args.signing_key_id.is_some() {
        return Err(ExecutorError::Config(
            "the compile stage must not be given a signing key (PRECOMPILE_SIGNING_KEY_*)".into(),
        ));
    }
    let out_dir = args.out_dir.clone().ok_or_else(|| {
        ExecutorError::Config("--out-dir (PRECOMPILE_OUT_DIR) must be set".into())
    })?;
    let bundle_keys = parse_bundle_keys(args)?;
    let existing = match args
        .precompile_signing_public_keys
        .as_deref()
        .map(str::trim)
    {
        None | Some("") => None,
        Some(raw) => Some(PlatformPublicKeys::from_json(raw)?),
    };
    let store = BucketStore::new(args.bucket_config()?, args.max_artifact_bytes);
    let stage = CompileStage {
        engine,
        engine_fp,
        store: &store,
        bundle_keys: &bundle_keys,
        existing_keys: existing.as_ref(),
    };
    let index = stage.run(items, &out_dir).await?;
    let count = |s: ItemStatus| index.entries.iter().filter(|e| e.status == s).count();
    tracing::info!(
        engine_fingerprint = %engine_fp,
        examined = index.entries.len(),
        compiled = count(ItemStatus::Compiled),
        skipped = count(ItemStatus::Skipped),
        failed = count(ItemStatus::Failed),
        "compile stage finished"
    );
    // Item failures travel in the index; the sign stage turns them into the report.
    Ok(0)
}

/// `--precompile-sign`: holds the key, never compiles or loads a module.
async fn run_sign_only(
    args: &PrecompileArgs,
    engine_fp: &str,
    items: &[PrecompileItem],
) -> Result<i32, ExecutorError> {
    let in_dir = args
        .in_dir
        .clone()
        .ok_or_else(|| ExecutorError::Config("--in-dir (PRECOMPILE_IN_DIR) must be set".into()))?;
    let key_file = args
        .signing_key_file
        .clone()
        .ok_or_else(|| ExecutorError::Config("PRECOMPILE_SIGNING_KEY_FILE must be set".into()))?;
    let key_id = required("PRECOMPILE_SIGNING_KEY_ID", &args.signing_key_id)?;
    let signer = PrecompileSigner::from_file(&key_id, &key_file)?;
    let raw = std::fs::read(in_dir.join(COMPILED_INDEX_FILE)).map_err(ExecutorError::Io)?;
    let index: CompiledIndex = serde_json::from_slice(&raw)
        .map_err(|e| ExecutorError::Config(format!("compiled index is invalid: {e}")))?;
    let store = BucketStore::new(args.bucket_config()?, args.max_artifact_bytes);
    let stage = SignStage {
        engine_fp,
        store: &store,
        signer: &signer,
    };
    let report = stage.run(items, &index, &in_dir).await;
    tracing::info!(
        engine_fingerprint = %engine_fp,
        examined = report.examined,
        compiled = report.compiled,
        skipped = report.skipped,
        failed = report.failed,
        "sign stage finished"
    );
    if let Some(path) = &args.report {
        let body = serde_json::to_vec_pretty(&report)
            .map_err(|e| ExecutorError::Config(format!("cannot serialize report: {e}")))?;
        std::fs::write(path, body).map_err(ExecutorError::Io)?;
    }
    if report.failed > 0 && !args.report_failures_only {
        return Ok(1);
    }
    Ok(0)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::sync::Mutex;

    use super::*;

    /// In-memory store counting writes so idempotency is observable.
    #[derive(Default)]
    struct MemStore {
        objects: Mutex<HashMap<String, Vec<u8>>>,
        puts: Mutex<usize>,
    }

    impl ArtifactStore for MemStore {
        async fn fetch_bundle(
            &self,
            c: &str,
            s: &str,
        ) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
            let m = self.objects.lock().unwrap();
            let get = |k: &str| {
                m.get(k)
                    .cloned()
                    .ok_or_else(|| ExecutorError::BucketFetch(format!("{k} missing")))
            };
            Ok((get(c)?, get(s)?))
        }
        async fn get_optional(&self, k: &str) -> Result<Option<Vec<u8>>, ExecutorError> {
            Ok(self.objects.lock().unwrap().get(k).cloned())
        }
        async fn put(&self, k: &str, b: &[u8]) -> Result<(), ExecutorError> {
            *self.puts.lock().unwrap() += 1;
            self.objects
                .lock()
                .unwrap()
                .insert(k.to_string(), b.to_vec());
            Ok(())
        }
    }

    /// Smallest valid component binary: magic + component-layer version.
    fn component_bytes() -> Vec<u8> {
        b"\0asm\x0d\0\x01\0".to_vec()
    }

    struct Fixture {
        engine: Engine,
        fp: String,
        store: MemStore,
        bundle_keys: PlatformPublicKeys,
        signer: PrecompileSigner,
        item: PrecompileItem,
    }

    fn fixture() -> Fixture {
        let engine = Engine::new(&engine_config()).unwrap();
        let fp = engine_fingerprint(&engine);
        let component = component_bytes();
        let digest_hex = sha256_hex(&component);
        let approval = SigningKey::from_bytes(&[9u8; 32]);
        let payload =
            crate::signing::signing_payload("waddles.test", "1.0.0", &digest_hex, 42).unwrap();
        let sidecar = serde_json::json!({
            "app_id": "waddles.test", "version": "1.0.0", "digest": digest_hex,
            "approval_id": 42, "key_id": "platform-1", "algorithm": "ed25519",
            "signature": BASE64.encode(approval.sign(&payload).to_bytes()),
        });
        let store = MemStore::default();
        store
            .objects
            .lock()
            .unwrap()
            .insert("bundles/c.wasm".into(), component);
        store.objects.lock().unwrap().insert(
            "bundles/c.sidecar.json".into(),
            serde_json::to_vec(&sidecar).unwrap(),
        );
        let mut keys = HashMap::new();
        keys.insert("platform-1".to_string(), approval.verifying_key());
        Fixture {
            engine,
            fp,
            store,
            bundle_keys: PlatformPublicKeys::from_keys(keys),
            signer: PrecompileSigner::from_seed("precompile-1", [3u8; 32]).unwrap(),
            item: PrecompileItem {
                app_id: "waddles.test".into(),
                version: "1.0.0".into(),
                digest: digest_hex,
                component_key: "bundles/c.wasm".into(),
                sidecar_key: "bundles/c.sidecar.json".into(),
            },
        }
    }

    fn precompiler(f: &Fixture) -> Precompiler<'_, MemStore> {
        Precompiler {
            engine: &f.engine,
            engine_fp: &f.fp,
            store: &f.store,
            bundle_keys: &f.bundle_keys,
            signer: &f.signer,
        }
    }

    #[tokio::test]
    async fn precompile_round_trip_produces_a_verifiable_deserializable_artifact() {
        let f = fixture();
        let status = precompiler(&f).precompile_one(&f.item).await.unwrap();
        assert_eq!(status, ItemStatus::Compiled);
        let (ak, sk) = object_keys(&f.fp, &f.item.digest).unwrap();
        assert!(ak.starts_with(&format!("precompiled/{}/", f.fp)));
        let objects = f.store.objects.lock().unwrap();
        let cwasm = objects.get(&ak).unwrap();
        let sig = objects.get(&sk).unwrap();
        verify_precompiled(cwasm, sig, &f.signer.own_key_set(), &f.item.digest, &f.fp).unwrap();
        // The verified artifact really deserializes under the shared config.
        // SAFETY: bytes were produced by this engine and verified above.
        let loaded = unsafe { wasmtime::component::Component::deserialize(&f.engine, cwasm) };
        assert!(loaded.is_ok());
    }

    #[tokio::test]
    async fn fingerprint_mismatch_and_tampering_are_rejected() {
        let f = fixture();
        precompiler(&f).precompile_one(&f.item).await.unwrap();
        let (ak, sk) = object_keys(&f.fp, &f.item.digest).unwrap();
        let objects = f.store.objects.lock().unwrap();
        let (cwasm, sig) = (objects.get(&ak).unwrap(), objects.get(&sk).unwrap());
        let keys = f.signer.own_key_set();
        let other_fp = "f".repeat(64);
        assert!(matches!(
            verify_precompiled(cwasm, sig, &keys, &f.item.digest, &other_fp),
            Err(ExecutorError::SignatureInvalid(m)) if m.contains("fingerprint")
        ));
        let mut tampered = cwasm.clone();
        tampered[0] ^= 1;
        assert!(verify_precompiled(&tampered, sig, &keys, &f.item.digest, &f.fp).is_err());
        let wrong = PrecompileSigner::from_seed("precompile-1", [4u8; 32]).unwrap();
        assert!(
            verify_precompiled(cwasm, sig, &wrong.own_key_set(), &f.item.digest, &f.fp).is_err()
        );
        assert!(verify_precompiled(cwasm, sig, &keys, &"0".repeat(64), &f.fp).is_err());
    }

    #[tokio::test]
    async fn second_run_is_idempotent_and_writes_nothing() {
        let f = fixture();
        let pc = precompiler(&f);
        let first = pc.run(std::slice::from_ref(&f.item)).await;
        assert_eq!((first.examined, first.compiled), (1, 1));
        let puts_after_first = *f.store.puts.lock().unwrap();
        assert_eq!(puts_after_first, 2);
        let second = pc.run(std::slice::from_ref(&f.item)).await;
        assert_eq!((second.examined, second.skipped, second.failed), (1, 1, 0));
        assert_eq!(*f.store.puts.lock().unwrap(), puts_after_first);
    }

    #[tokio::test]
    async fn invalid_existing_artifact_is_rebuilt() {
        let f = fixture();
        let (ak, sk) = object_keys(&f.fp, &f.item.digest).unwrap();
        f.store
            .objects
            .lock()
            .unwrap()
            .insert(ak.clone(), b"garbage".to_vec());
        f.store.objects.lock().unwrap().insert(sk, b"{}".to_vec());
        let status = precompiler(&f).precompile_one(&f.item).await.unwrap();
        assert_eq!(status, ItemStatus::Compiled);
        assert_ne!(
            f.store.objects.lock().unwrap().get(&ak).unwrap(),
            b"garbage"
        );
    }

    #[tokio::test]
    async fn unapproved_or_mismatched_bundle_is_never_compiled() {
        let f = fixture();
        // Digest mismatch.
        let mut bad = f.item.clone();
        bad.digest = "a".repeat(64);
        let r = precompiler(&f).run(std::slice::from_ref(&bad)).await;
        assert_eq!(r.failed, 1);
        // Sidecar signed by an unknown key.
        let unknown = PlatformPublicKeys::from_keys(HashMap::new());
        let mut k = HashMap::new();
        k.insert(
            "other".to_string(),
            SigningKey::from_bytes(&[1u8; 32]).verifying_key(),
        );
        let other = PlatformPublicKeys::from_keys(k);
        for keys in [&unknown, &other] {
            let pc = Precompiler {
                bundle_keys: keys,
                ..precompiler(&f)
            };
            assert!(pc.precompile_one(&f.item).await.is_err());
        }
        assert_eq!(*f.store.puts.lock().unwrap(), 0);
    }

    fn temp_dir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("pc-{tag}-{}-{:p}", std::process::id(), &tag));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn compile_stage(f: &Fixture) -> CompileStage<'_, MemStore> {
        CompileStage {
            engine: &f.engine,
            engine_fp: &f.fp,
            store: &f.store,
            bundle_keys: &f.bundle_keys,
            existing_keys: None,
        }
    }

    #[tokio::test]
    async fn split_stages_compile_then_sign_matches_combined_mode() {
        let f = fixture();
        let dir = temp_dir("split");
        let items = std::slice::from_ref(&f.item);
        let index = compile_stage(&f).run(items, &dir).await.unwrap();
        assert_eq!(
            (index.entries.len(), index.entries[0].status),
            (1, ItemStatus::Compiled)
        );
        // Compile stage uploaded nothing and never saw a signer.
        assert_eq!(*f.store.puts.lock().unwrap(), 0);
        let sign = SignStage {
            engine_fp: &f.fp,
            store: &f.store,
            signer: &f.signer,
        };
        let report = sign.run(items, &index, &dir).await;
        assert_eq!((report.examined, report.compiled, report.failed), (1, 1, 0));
        let (ak, sk) = object_keys(&f.fp, &f.item.digest).unwrap();
        let objects = f.store.objects.lock().unwrap();
        verify_precompiled(
            objects.get(&ak).unwrap(),
            objects.get(&sk).unwrap(),
            &f.signer.own_key_set(),
            &f.item.digest,
            &f.fp,
        )
        .unwrap();
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[tokio::test]
    async fn compile_stage_skips_existing_valid_artifact_and_records_failures() {
        let f = fixture();
        let dir = temp_dir("skip");
        precompiler(&f).precompile_one(&f.item).await.unwrap();
        let keys = f.signer.own_key_set();
        let stage = CompileStage {
            existing_keys: Some(&keys),
            ..compile_stage(&f)
        };
        let mut bad = f.item.clone();
        bad.digest = "b".repeat(64);
        let index = stage.run(&[f.item.clone(), bad], &dir).await.unwrap();
        assert_eq!(index.entries[0].status, ItemStatus::Skipped);
        assert_eq!(index.entries[1].status, ItemStatus::Failed);
        assert!(index.entries[1].error.is_some());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[tokio::test]
    async fn sign_stage_rejects_tampered_missing_and_foreign_inputs() {
        let f = fixture();
        let dir = temp_dir("tamper");
        let items = std::slice::from_ref(&f.item);
        let index = compile_stage(&f).run(items, &dir).await.unwrap();
        let sign = SignStage {
            engine_fp: &f.fp,
            store: &f.store,
            signer: &f.signer,
        };
        // Tampered staged bytes: hash no longer matches the index.
        let staged = dir.join(staged_name(&f.item.digest));
        let mut bytes = std::fs::read(&staged).unwrap();
        bytes[0] ^= 1;
        std::fs::write(&staged, &bytes).unwrap();
        let r = sign.run(items, &index, &dir).await;
        assert_eq!((r.examined, r.failed), (1, 1));
        // Manifest item the index never answered for.
        let mut other = f.item.clone();
        other.digest = "c".repeat(64);
        let r = sign.run(&[other], &index, &dir).await;
        assert_eq!(r.failed, 1);
        // Index claiming a different engine fingerprint.
        let mut stale = index.clone();
        stale.engine_fingerprint = "f".repeat(64);
        let r = sign.run(items, &stale, &dir).await;
        assert_eq!(r.failed, 1);
        assert_eq!(*f.store.puts.lock().unwrap(), 0);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[tokio::test]
    async fn sign_stage_carries_compile_failures_into_the_report() {
        let f = fixture();
        let dir = temp_dir("carry");
        let mut bad = f.item.clone();
        bad.digest = "d".repeat(64);
        let items = [bad];
        let index = compile_stage(&f).run(&items, &dir).await.unwrap();
        let sign = SignStage {
            engine_fp: &f.fp,
            store: &f.store,
            signer: &f.signer,
        };
        let r = sign.run(&items, &index, &dir).await;
        assert_eq!((r.examined, r.failed, r.compiled), (1, 1, 0));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[tokio::test]
    async fn compile_only_cli_refuses_a_signing_key() {
        let engine = Engine::new(&engine_config()).unwrap();
        let args = PrecompileArgs::parse_from([
            "x",
            "--precompile-compile-only",
            "--signing-key-file",
            "/nope",
        ]);
        let err = run_compile_only(&args, &engine, "0", &[])
            .await
            .unwrap_err();
        assert!(err.to_string().contains("must not be given a signing key"));
    }

    #[test]
    fn stage_flags_parse_and_default_off() {
        let a = PrecompileArgs::parse_from(["x", "--precompile"]);
        assert!(a.precompile && !a.compile_only && !a.sign_only);
        let a = PrecompileArgs::parse_from(["x", "--precompile-sign", "--in-dir", "/w"]);
        assert!(a.sign_only && !a.precompile && a.in_dir == Some(PathBuf::from("/w")));
    }

    #[test]
    fn object_keys_refuse_path_escapes() {
        let fp = "0".repeat(64);
        assert!(object_keys(&fp, "../../etc/passwd").is_err());
        assert!(object_keys("../x", &"a".repeat(64)).is_err());
        let (a, s) = object_keys(&fp, &format!("sha256:{}", "A".repeat(64))).unwrap();
        assert_eq!(a, format!("precompiled/{fp}/{}.cwasm", "a".repeat(64)));
        assert_eq!(s, format!("{a}.sig"));
    }

    #[test]
    fn signer_debug_and_file_errors_never_leak_key_material() {
        let dir = std::env::temp_dir().join(format!("pc-key-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("key");
        std::fs::write(&path, BASE64.encode([5u8; 32])).unwrap();
        let s = PrecompileSigner::from_file("k1", &path).unwrap();
        let dbg = format!("{s:?}");
        assert!(dbg.contains("redacted") && !dbg.contains(&BASE64.encode([5u8; 32])));
        std::fs::write(&path, "not-base64!!").unwrap();
        let err = PrecompileSigner::from_file("k1", &path)
            .unwrap_err()
            .to_string();
        assert!(!err.contains("not-base64"));
        assert!(PrecompileSigner::from_seed("bad id/", [0u8; 32]).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
