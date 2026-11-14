//! Precompiled-component cache (`.cwasm`), spec SS7.2/SS7.6.
//!
//! A cold bundle load JIT-compiles the component with Cranelift (seconds for
//! a Rust component, minutes for a CPython/componentize-py one). This module
//! persists the serialized artifact under `EXECUTOR_PRECOMPILE_DIR` so a
//! restarted executor deserializes instead of recompiling.
//!
//! **Security model (the point of this module).** Deserializing a cwasm
//! maps and runs machine code, so a tampered file is code execution inside
//! the executor. Every artifact therefore carries an HMAC-SHA256 tag over
//! `len(key) || key || artifact` keyed by a platform secret
//! (`EXECUTOR_CACHE_HMAC_KEY[_FILE]`), verified in constant time **before**
//! `Component::deserialize`. No key => no cache (fail closed to
//! compile-from-source); there is no unauthenticated mode. The cache sits
//! strictly *after* the loader's digest + signature checks: a cache hit
//! replaces only the compile step, never the source verification.
//!
//! **Key / layout.** `<dir>/cwasm-v1/<sha256-hex>-w<wasmtime>-<collector>-<fp>.cwasm`
//! where `<fp>` is the first 16 hex chars of SHA-256 over wasmtime's
//! `Engine::precompile_compatibility_hash` (target triple, compiler flags,
//! ISA/CPU feature flags, tunables, wasm features) plus OS/arch and the
//! collector. A cwasm built under a different engine config or CPU feature
//! set lands under a different key, and the startup sweep deletes it.
//!
//! **Durability.** Writes go to a unique temp file, `fsync`, atomic
//! `rename`, then `fsync` of the directory; a crash leaves either the old
//! state or the complete new file, never a torn artifact (and a torn one
//! would fail the HMAC anyway).
//!
//! **Bounds.** Total bytes (`EXECUTOR_PRECOMPILE_MAX_BYTES`) and entry count
//! (`EXECUTOR_PRECOMPILE_MAX_ENTRIES`), evicted least-recently-used by
//! mtime (touched on every hit). Keys currently leased by an in-flight load
//! are never evicted. The dir may be shared by replicas on one node; every
//! cross-process interaction is a whole-file atomic op, and a file evicted
//! under a reader just becomes a miss.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::hash::{Hash, Hasher};
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, Instant, SystemTime};

use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use tracing::{debug, error, info, warn};
use wasmtime::component::Component;
use wasmtime::Engine;

use crate::config::CliConfig;
use crate::metrics::ExecutorMetrics;

/// The wasmtime crate version this build links; part of every cache key. A
/// unit test pins it to `Cargo.toml`'s exact `wasmtime` pin so a bump cannot
/// silently reuse artifacts (the engine also embeds and checks its own
/// version on deserialize -- this makes the mismatch a clean miss+sweep
/// instead of a deserialize error).
pub const WASMTIME_ABI: &str = "49.0.5";

/// Name of the versioned subdirectory the cache owns inside
/// `EXECUTOR_PRECOMPILE_DIR` (which may be a volume mount root with
/// foreign ownership/mode, e.g. group-writable via `fsGroup`).
pub const CACHE_SUBDIR: &str = "cwasm-v1";

/// Minimum accepted HMAC key length in bytes.
pub const MIN_HMAC_KEY_LEN: usize = 32;

/// Opt-out kill switch flag key; the executor reads its ENV baseline
/// (`EXECUTOR_DISABLE_COMPILE_CACHE`), see `CliConfig`.
pub const DISABLE_FLAG: &str = "waddles.disable-executor-compile-cache";

const MAGIC: &[u8; 8] = b"WDLCWSM1";
const TAG_LEN: usize = 32;
const HEADER_LEN: usize = MAGIC.len() + TAG_LEN;
const ARTIFACT_EXT: &str = "cwasm";
const TMP_EXT: &str = "tmp";
/// Temp files younger than this may belong to a sibling replica's in-flight
/// write on a shared volume, so the startup sweep leaves them alone.
const STALE_TMP_AGE: Duration = Duration::from_secs(15 * 60);

type HmacSha256 = Hmac<Sha256>;

/// Why a cache could not be used or an operation on it failed. Never carries
/// key material or artifact bytes.
#[derive(Debug, thiserror::Error)]
pub enum CacheError {
    /// Directory missing, unwritable, symlinked, group/world-writable or not
    /// owned by the executor's uid.
    #[error("cache directory unusable: {0}")]
    Directory(String),
    /// Filesystem error on an artifact.
    #[error("cache i/o error: {0}")]
    Io(#[from] std::io::Error),
    /// The artifact is larger than the whole cache budget.
    #[error("artifact of {size} bytes exceeds the cache cap of {cap} bytes")]
    TooLarge { size: u64, cap: u64 },
}

/// Why the cache is off, for logs/tests (never a secret).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DisabledReason {
    /// `waddles.disable-executor-compile-cache` baseline ON.
    KillSwitch,
    /// No / too-short / unreadable HMAC key.
    NoKey,
    /// Directory failed the creation or permission checks.
    BadDirectory,
}

/// Identity of one cached artifact (also its file stem). Built only by
/// [`CompileCache::key_for`], which validates the digest shape so a key can
/// never contain a path separator.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct CacheKey(String);

impl CacheKey {
    /// The key string (`<hex>-w<abi>-<collector>-<fp>`); no secret.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    fn file_name(&self) -> String {
        format!("{}.{ARTIFACT_EXT}", self.0)
    }
}

/// RAII marker: while alive, eviction will not delete this key's file.
pub struct Lease {
    cache: Arc<CompileCache>,
    key: String,
}

impl Drop for Lease {
    fn drop(&mut self) {
        let mut in_use = self
            .cache
            .in_use
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        if let Some(n) = in_use.get_mut(&self.key) {
            *n = n.saturating_sub(1);
            if *n == 0 {
                in_use.remove(&self.key);
            }
        }
    }
}

/// Adapts SHA-256 to `std::hash::Hasher` so wasmtime's `impl Hash`
/// compatibility hash can be folded into a stable digest.
struct Sha256Hasher(Sha256);

impl Hasher for Sha256Hasher {
    fn finish(&self) -> u64 {
        let d = self.0.clone().finalize();
        let mut b = [0u8; 8];
        b.copy_from_slice(&d[..8]);
        u64::from_be_bytes(b)
    }

    fn write(&mut self, bytes: &[u8]) {
        self.0.update(bytes);
    }
}

/// Hex fingerprint (16 chars) of everything that makes a cwasm loadable by
/// `engine`: wasmtime's own compatibility hash (target, compiler flags,
/// CPU-feature ISA flags, tunables, wasm features, module version), plus
/// OS/arch and the GC collector.
pub fn engine_fingerprint(engine: &Engine, collector: &str) -> String {
    let mut h = Sha256Hasher(Sha256::new());
    engine.precompile_compatibility_hash().hash(&mut h);
    h.0.update(std::env::consts::OS.as_bytes());
    h.0.update(std::env::consts::ARCH.as_bytes());
    h.0.update(collector.as_bytes());
    hex(&h.0.finalize()[..8])
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes.iter().fold(String::new(), |mut s, b| {
        let _ = write!(s, "{b:02x}");
        s
    })
}

/// Result of resolving the configured HMAC key.
fn resolve_hmac_key(cfg: &CliConfig) -> Result<Vec<u8>, String> {
    let raw = if let Some(path) = &cfg.executor_cache_hmac_key_file {
        fs::read_to_string(path)
            .map_err(|e| format!("EXECUTOR_CACHE_HMAC_KEY_FILE unreadable ({:?})", e.kind()))?
    } else if let Some(k) = &cfg.executor_cache_hmac_key {
        k.expose().to_string()
    } else {
        return Err("no EXECUTOR_CACHE_HMAC_KEY / EXECUTOR_CACHE_HMAC_KEY_FILE configured".into());
    };
    let key = raw.trim().as_bytes().to_vec();
    if key.len() < MIN_HMAC_KEY_LEN {
        return Err(format!(
            "cache HMAC key shorter than {MIN_HMAC_KEY_LEN} bytes (or empty)"
        ));
    }
    Ok(key)
}

/// The on-disk precompiled-component cache. Cheap to share behind an `Arc`.
pub struct CompileCache {
    root: PathBuf,
    hmac_key: Vec<u8>,
    suffix: String,
    max_bytes: u64,
    max_entries: u64,
    in_use: Mutex<HashMap<String, usize>>,
}

struct Entry {
    path: PathBuf,
    key: String,
    size: u64,
    mtime: SystemTime,
}

impl CompileCache {
    /// Builds the cache for `engine`, or returns `None` (cache DISABLED,
    /// behavior = compile from source) after logging why. Missing/short key
    /// logs ERROR and counts `disabled`; the kill switch logs INFO. Runs the
    /// startup sweep and publishes the size gauges.
    pub fn open(
        cfg: &CliConfig,
        engine: &Engine,
        metrics: &ExecutorMetrics,
    ) -> Option<Arc<Self>> {
        match Self::try_open(cfg, engine, metrics) {
            Ok(c) => Some(c),
            Err(reason) => {
                metrics.record_precompile_cache("disabled");
                debug!(?reason, "compile cache disabled");
                None
            }
        }
    }

    /// [`Self::open`] with the reason exposed (tests, diagnostics).
    pub fn try_open(
        cfg: &CliConfig,
        engine: &Engine,
        metrics: &ExecutorMetrics,
    ) -> Result<Arc<Self>, DisabledReason> {
        if cfg.executor_disable_compile_cache {
            info!(
                flag = DISABLE_FLAG,
                "precompiled-component cache disabled by kill switch; compiling from source"
            );
            return Err(DisabledReason::KillSwitch);
        }
        let hmac_key = match resolve_hmac_key(cfg) {
            Ok(k) => k,
            Err(why) => {
                error!(
                    reason = %why,
                    "precompiled-component cache DISABLED: no usable integrity key \
                     (a cache without an HMAC would let a tampered .cwasm run as machine code); \
                     every bundle load will JIT-compile from source"
                );
                return Err(DisabledReason::NoKey);
            }
        };
        let root = match prepare_dir(&cfg.executor_precompile_dir) {
            Ok(r) => r,
            Err(e) => {
                error!(
                    dir = %cfg.executor_precompile_dir.display(),
                    error = %e,
                    "precompiled-component cache DISABLED: cache directory failed checks; \
                     every bundle load will JIT-compile from source"
                );
                return Err(DisabledReason::BadDirectory);
            }
        };
        let suffix = format!(
            "w{WASMTIME_ABI}-{}-{}",
            cfg.executor_wasm_collector,
            engine_fingerprint(engine, &cfg.executor_wasm_collector)
        );
        let cache = Arc::new(Self {
            root,
            hmac_key,
            suffix,
            max_bytes: cfg.executor_precompile_max_bytes,
            max_entries: cfg.executor_precompile_max_entries,
            in_use: Mutex::new(HashMap::new()),
        });
        cache.startup_sweep(metrics);
        cache.publish_gauges(metrics);
        info!(
            dir = %cache.root.display(),
            suffix = %cache.suffix,
            max_bytes = cache.max_bytes,
            max_entries = cache.max_entries,
            "precompiled-component cache enabled"
        );
        Ok(cache)
    }

    /// The cache key for a source digest (`sha256:<64 lowercase hex>`), or
    /// `None` for any other shape (never builds a path from unvalidated text).
    pub fn key_for(&self, digest: &str) -> Option<CacheKey> {
        let hexpart = digest.strip_prefix("sha256:")?;
        if hexpart.len() != 64
            || !hexpart
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return None;
        }
        Some(CacheKey(format!("{hexpart}-{}", self.suffix)))
    }

    /// Marks `key` in use until the returned lease drops.
    pub fn lease(self: &Arc<Self>, key: &CacheKey) -> Lease {
        let mut in_use = self.in_use.lock().unwrap_or_else(PoisonError::into_inner);
        *in_use.entry(key.0.clone()).or_insert(0) += 1;
        Lease {
            cache: Arc::clone(self),
            key: key.0.clone(),
        }
    }

    fn is_leased(&self, key: &str) -> bool {
        self.in_use
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .contains_key(key)
    }

    fn tag(&self, key: &CacheKey, body: &[u8]) -> HmacSha256 {
        // `new_from_slice` accepts any key length for HMAC; the Err arm is
        // unreachable but handled without panicking by a zero-key fallback
        // that can never match (key length was validated >= 32 at open).
        let mut mac = match HmacSha256::new_from_slice(&self.hmac_key) {
            Ok(m) => m,
            Err(_) => HmacSha256::new_from_slice(&[0u8; MIN_HMAC_KEY_LEN]).unwrap_or_else(|_| {
                unreachable_mac()
            }),
        };
        mac.update(&(key.0.len() as u32).to_be_bytes());
        mac.update(key.0.as_bytes());
        mac.update(body);
        mac
    }

    /// Looks `key` up and, only if its integrity tag verifies, deserializes
    /// it for `engine`. `None` = miss OR rejected (corrupt files are deleted,
    /// counted and logged here). Records hit/miss/corrupt and the
    /// deserialize-seconds histogram. Blocking: call from `spawn_blocking`.
    pub fn lookup(
        &self,
        engine: &Engine,
        key: &CacheKey,
        metrics: &ExecutorMetrics,
    ) -> Option<Component> {
        let path = self.root.join(key.file_name());
        let started = Instant::now();
        let buf = match read_regular_file(&path, self.max_bytes) {
            Ok(Some(b)) => b,
            Ok(None) => {
                metrics.record_precompile_cache("miss");
                debug!(key = key.as_str(), "compile cache miss");
                return None;
            }
            Err(e) => {
                self.reject(&path, key, metrics, &format!("read failed: {:?}", e.kind()));
                return None;
            }
        };
        if buf.len() < HEADER_LEN || &buf[..MAGIC.len()] != MAGIC {
            self.reject(&path, key, metrics, "truncated or bad header");
            return None;
        }
        let (tag, body) = buf[MAGIC.len()..].split_at(TAG_LEN);
        // Constant-time verify BEFORE any deserialize (see module doc).
        if self.tag(key, body).verify_slice(tag).is_err() {
            self.reject(&path, key, metrics, "integrity tag mismatch");
            return None;
        }
        // SAFETY: `body` was authenticated by HMAC-SHA256 under a platform
        // secret that only the executor holds, bound to this exact key
        // (digest + wasmtime ABI + collector + engine fingerprint), so it is
        // bytes this engine family produced via `Component::serialize`;
        // `deserialize` (not `deserialize_file`) copies them, so the file
        // cannot be mutated underneath the mapped code afterwards.
        #[allow(unsafe_code)]
        let component = unsafe { Component::deserialize(engine, body) };
        match component {
            Ok(c) => {
                touch(&path);
                metrics.record_deserialize(started.elapsed(), "ok");
                metrics.record_precompile_cache("hit");
                debug!(
                    key = key.as_str(),
                    ms = started.elapsed().as_millis() as u64,
                    "compile cache hit"
                );
                Some(c)
            }
            Err(e) => {
                metrics.record_deserialize(started.elapsed(), "error");
                self.reject(
                    &path,
                    key,
                    metrics,
                    &format!("authenticated artifact failed to deserialize: {e}"),
                );
                None
            }
        }
    }

    fn reject(&self, path: &Path, key: &CacheKey, metrics: &ExecutorMetrics, why: &str) {
        let removed = fs::remove_file(path).is_ok();
        metrics.record_precompile_cache("corrupt");
        // Key + reason only: never artifact bytes, never key material.
        error!(
            key = key.as_str(),
            reason = why,
            removed,
            "compile cache entry rejected; recompiling from source"
        );
        self.publish_gauges(metrics);
    }

    /// Persists `artifact` (the output of `Component::serialize`) under
    /// `key`: temp file, fsync, atomic rename, fsync dir; then enforces the
    /// size/count bounds. An `Err` is best-effort-failure for the caller to
    /// log and count (`write_failed`); it never means the load failed.
    pub fn store(
        &self,
        key: &CacheKey,
        artifact: &[u8],
        metrics: &ExecutorMetrics,
    ) -> Result<(), CacheError> {
        let total = (HEADER_LEN + artifact.len()) as u64;
        if total > self.max_bytes {
            return Err(CacheError::TooLarge {
                size: total,
                cap: self.max_bytes,
            });
        }
        let tag = self.tag(key, artifact).finalize().into_bytes();
        let nanos = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let tmp = self
            .root
            .join(format!("{}.{}.{nanos}.{TMP_EXT}", key.0, std::process::id()));
        let result = (|| -> std::io::Result<()> {
            let mut f = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&tmp)?;
            f.write_all(MAGIC)?;
            f.write_all(&tag)?;
            f.write_all(artifact)?;
            f.sync_all()?;
            drop(f);
            fs::rename(&tmp, self.root.join(key.file_name()))?;
            File::open(&self.root)?.sync_all()
        })();
        if let Err(e) = result {
            let _ = fs::remove_file(&tmp);
            return Err(CacheError::Io(e));
        }
        debug!(key = key.as_str(), bytes = total, "compile cache entry written");
        self.enforce_bounds(metrics);
        self.publish_gauges(metrics);
        Ok(())
    }

    fn scan(&self) -> Vec<Entry> {
        let Ok(rd) = fs::read_dir(&self.root) else {
            return Vec::new();
        };
        rd.flatten()
            .filter_map(|de| {
                let path = de.path();
                if path.extension().and_then(|e| e.to_str()) != Some(ARTIFACT_EXT) {
                    return None;
                }
                let md = fs::symlink_metadata(&path).ok()?;
                if !md.is_file() {
                    return None;
                }
                let key = path.file_stem()?.to_str()?.to_string();
                Some(Entry {
                    path,
                    key,
                    size: md.len(),
                    mtime: md.modified().unwrap_or(SystemTime::UNIX_EPOCH),
                })
            })
            .collect()
    }

    /// Evicts least-recently-used entries (oldest mtime first) until both
    /// caps hold, skipping any key currently leased by a load.
    fn enforce_bounds(&self, metrics: &ExecutorMetrics) {
        let mut entries = self.scan();
        let mut bytes: u64 = entries.iter().map(|e| e.size).sum();
        let mut count = entries.len() as u64;
        if bytes <= self.max_bytes && count <= self.max_entries {
            return;
        }
        entries.sort_by_key(|e| e.mtime);
        for e in entries {
            if bytes <= self.max_bytes && count <= self.max_entries {
                break;
            }
            if self.is_leased(&e.key) {
                debug!(key = %e.key, "eviction skipped: entry in use");
                continue;
            }
            if fs::remove_file(&e.path).is_ok() {
                bytes = bytes.saturating_sub(e.size);
                count = count.saturating_sub(1);
                metrics.record_precompile_cache("evicted");
                debug!(key = %e.key, size = e.size, "compile cache entry evicted (LRU)");
            }
        }
        if bytes > self.max_bytes || count > self.max_entries {
            warn!(
                bytes,
                count,
                "compile cache still over its bound: every remaining entry is in use"
            );
        }
    }

    /// Deletes artifacts built for a different wasmtime ABI / engine config /
    /// collector, symlinks, and stale partial temp files.
    fn startup_sweep(&self, metrics: &ExecutorMetrics) {
        let Ok(rd) = fs::read_dir(&self.root) else {
            return;
        };
        let (mut stale, mut tmp) = (0u32, 0u32);
        for de in rd.flatten() {
            let path = de.path();
            let Some(name) = path.file_name().and_then(|n| n.to_str()).map(str::to_string) else {
                continue;
            };
            let Ok(md) = fs::symlink_metadata(&path) else {
                continue;
            };
            if md.file_type().is_symlink() {
                let _ = fs::remove_file(&path);
                warn!(entry = %name, "removed symlink from compile cache directory");
                continue;
            }
            match path.extension().and_then(|e| e.to_str()) {
                Some(TMP_EXT) => {
                    let age = md
                        .modified()
                        .ok()
                        .and_then(|m| SystemTime::now().duration_since(m).ok())
                        .unwrap_or(Duration::ZERO);
                    if age >= STALE_TMP_AGE && fs::remove_file(&path).is_ok() {
                        tmp += 1;
                    }
                }
                Some(ARTIFACT_EXT) => {
                    let stem = name.trim_end_matches(&format!(".{ARTIFACT_EXT}")[..]);
                    if !stem.ends_with(&self.suffix) && fs::remove_file(&path).is_ok() {
                        stale += 1;
                        metrics.record_precompile_cache("evicted");
                    }
                }
                _ => {}
            }
        }
        if stale + tmp > 0 {
            info!(
                stale_abi_or_engine_entries = stale,
                partial_temp_files = tmp,
                "compile cache startup sweep removed entries"
            );
        }
        self.enforce_bounds(metrics);
    }

    /// Re-scans the directory and publishes the bytes/entries gauges.
    pub fn publish_gauges(&self, metrics: &ExecutorMetrics) {
        let entries = self.scan();
        metrics.set_precompile_cache_size(entries.iter().map(|e| e.size).sum(), entries.len());
    }

    /// Current `(bytes, entries)` on disk, for tests and diagnostics.
    pub fn usage(&self) -> (u64, usize) {
        let e = self.scan();
        (e.iter().map(|x| x.size).sum(), e.len())
    }

    /// The directory holding artifacts (tests, diagnostics).
    pub fn root(&self) -> &Path {
        &self.root
    }
}

/// `HmacSha256::new_from_slice` cannot fail for HMAC (any key length is
/// valid); this exists only so [`CompileCache::tag`] needs no `expect`.
fn unreachable_mac() -> HmacSha256 {
    <HmacSha256 as Mac>::new(&Default::default())
}

/// Reads `path` if it is a regular (non-symlink) file no larger than `cap`
/// plus header; `Ok(None)` when absent.
fn read_regular_file(path: &Path, cap: u64) -> std::io::Result<Option<Vec<u8>>> {
    let md = match fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e),
    };
    if !md.is_file() || md.len() > cap {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "not a regular file within the size cap",
        ));
    }
    let mut f = File::open(path)?;
    let mut buf = Vec::with_capacity(md.len() as usize);
    f.read_to_end(&mut buf)?;
    Ok(Some(buf))
}

/// Marks `path` as just used (LRU recency); best effort.
fn touch(path: &Path) {
    if let Ok(f) = OpenOptions::new().write(true).open(path) {
        let _ = f.set_modified(SystemTime::now());
    }
}

/// Creates (0700) or validates `<parent>/cwasm-v1` and returns it. Refuses a
/// symlink, a group/world-writable directory, or one not owned by the
/// current uid. The parent may be a volume mount root with foreign
/// ownership/mode, which is why the cache owns a subdirectory.
pub fn prepare_dir(parent: &Path) -> Result<PathBuf, CacheError> {
    fs::create_dir_all(parent)
        .map_err(|e| CacheError::Directory(format!("cannot create {}: {e}", parent.display())))?;
    let root = parent.join(CACHE_SUBDIR);
    match fs::symlink_metadata(&root) {
        Ok(md) if md.file_type().is_symlink() => {
            return Err(CacheError::Directory(format!(
                "{} is a symlink",
                root.display()
            )));
        }
        Ok(md) if !md.is_dir() => {
            return Err(CacheError::Directory(format!(
                "{} exists and is not a directory",
                root.display()
            )));
        }
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            fs::DirBuilder::new()
                .mode(0o700)
                .create(&root)
                .map_err(|e| {
                    CacheError::Directory(format!("cannot create {}: {e}", root.display()))
                })?;
        }
        Err(e) => return Err(CacheError::Io(e)),
    }
    let md = fs::metadata(&root)?;
    let mode = md.permissions().mode();
    if mode & 0o022 != 0 {
        return Err(CacheError::Directory(format!(
            "{} is group/world-writable (mode {:o})",
            root.display(),
            mode & 0o7777
        )));
    }
    // Ownership without libc: a file this process creates is owned by this
    // process's euid, so the directory must carry that same uid. Combined
    // with the no-group/world-write check, only the owner can write here.
    let probe = root.join(format!(".probe-{}", std::process::id()));
    let _ = fs::remove_file(&probe);
    let pf = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&probe)
        .map_err(|e| CacheError::Directory(format!("{} is not writable: {e}", root.display())))?;
    let probe_uid = pf.metadata().map(|m| m.uid());
    drop(pf);
    let _ = fs::remove_file(&probe);
    if probe_uid? != md.uid() {
        return Err(CacheError::Directory(format!(
            "{} is not owned by the executor's uid",
            root.display()
        )));
    }
    Ok(root)
}
