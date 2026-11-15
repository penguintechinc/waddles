//! Music request queue policy over the `db-v2` capability.
//!
//! One queue per community (host-scoped). The actor is read ONLY from the host-injected
//! `ToolContext`; the requester is stored but never returned. Every multi-row mutation is a
//! single `transact` (the host serializes writers per community), retried on conflicts.
use serde_json::{Value, json};
use std::sync::Arc;
use waddle_sdk::db::Value as Db;
use waddle_sdk::storage_v2::{Cond, DbV2Error, DbV2Transport, Query, Row, TxOp, TxResult};
use waddle_sdk::tool::{ToolContext, ToolError, ToolRegistry};

/// Scope for read-only queue tools.
pub const SCOPE_READ: &str = "music:requests.read";
/// Scope for viewer write tools.
pub const SCOPE_WRITE: &str = "music:requests.write";
/// Scope for moderator queue management (also gates `kind: dj` adds).
pub const SCOPE_MANAGE: &str = "music:requests.manage";
/// Scope for toggling the optional AI DJ layer.
pub const SCOPE_DJ_MANAGE: &str = "music:dj.manage";
/// Scope for the play-statistics read tool.
pub const SCOPE_STATS_READ: &str = "music:stats.read";
/// Scope for the DMCA-safe mode setting.
pub const SCOPE_SETTINGS_WRITE: &str = "music:settings.write";

const KIND_VIEWER: i64 = 1;
const KIND_DJ: i64 = 2;
const KIND_IMPORTED: i64 = 3;
const STATUS_QUEUED: i64 = 1;
const STATUS_PLAYING: i64 = 2;
const DEFAULT_MAX_PER_USER: i64 = 3;
const DEFAULT_MAX_QUEUE: i64 = 100;
const ATTEMPTS: u32 = 4;
const HISTORY_KEEP: u64 = 500;
const MAX_DURATION_MS: i64 = 86_400_000;
const EPOCH: &str = "1970-01-01T00:00:00.000Z";
/// Provider code for any source not on the safe list.
pub const PROVIDER_OTHER: &str = "other";

/// One DMCA-safe source rule: a provider code, a host (exact or subdomain) and an optional
/// lower-case path prefix the URL must also start with.
pub struct SafeSource {
    /// Provider code stored on the row (`rfm` / `ncs`).
    pub provider: &'static str,
    /// Host the URL must have (exact match or a subdomain of it).
    pub host: &'static str,
    /// Lower-case path prefix that must match, or "" for any path.
    pub path_prefix: &'static str,
}

/// CONFIG TABLE: sources treated as DMCA-safe. The provider is derived HERE from the
/// `source_url` host -- never from caller input. Maintained by the platform team; entries
/// need a licensing review before they are added. `rfm` = royalty-free music catalogs that
/// permit stream use; `ncs` = NoCopyrightSounds, identified ONLY by host `ncs.io`,
/// `ncs.lnk.to` or the SoundCloud path `/nocopyrightsounds` (the NCS YouTube channel cannot
/// be derived from a URL, so YouTube links are never treated as safe).
pub const SAFE_SOURCES: &[SafeSource] = &[
    SafeSource {
        provider: "ncs",
        host: "ncs.io",
        path_prefix: "",
    },
    SafeSource {
        provider: "ncs",
        host: "ncs.lnk.to",
        path_prefix: "",
    },
    SafeSource {
        provider: "ncs",
        host: "soundcloud.com",
        path_prefix: "/nocopyrightsounds",
    },
    SafeSource {
        provider: "rfm",
        host: "pixabay.com",
        path_prefix: "/music",
    },
    SafeSource {
        provider: "rfm",
        host: "pretzel.rocks",
        path_prefix: "",
    },
    SafeSource {
        provider: "rfm",
        host: "streambeats.com",
        path_prefix: "",
    },
    SafeSource {
        provider: "rfm",
        host: "uppbeat.io",
        path_prefix: "",
    },
    SafeSource {
        provider: "rfm",
        host: "incompetech.com",
        path_prefix: "",
    },
    SafeSource {
        provider: "rfm",
        host: "bensound.com",
        path_prefix: "",
    },
];

/// Wall clock seam (host-provided, ms since the Unix epoch).
pub trait Clock: Send + Sync {
    /// Current time in milliseconds.
    fn now_millis(&self) -> u64;
}

/// Platform context seam: verified numeric scope and the optional web base URL.
pub trait Platform: Send + Sync {
    /// Host-verified numeric community id (0 = no scope).
    fn community_id(&self) -> i32;
    /// Platform web base URL when configured.
    fn web_base_url(&self) -> Option<String>;
}

/// `db-v2` transport that can live in the shared tool registry.
pub trait Store: DbV2Transport + Send + Sync {}
impl<T: DbV2Transport + Send + Sync> Store for T {}

/// Stable, non-sensitive invalid-args error.
fn invalid(m: &str) -> ToolError {
    ToolError::InvalidArgs(m.into())
}

/// The DMCA-safe refusal, carrying a clear message after the stable code.
fn not_dmca_safe() -> ToolError {
    ToolError::InvalidArgs(
        "not_dmca_safe: DMCA-safe mode is on; only royalty-free (RFM) or NoCopyrightSounds \
         (NCS) links are accepted"
            .into(),
    )
}

/// Maps a storage failure to a stable tool error without backend detail.
fn db_err(e: DbV2Error) -> ToolError {
    match e {
        DbV2Error::Denied(_) => ToolError::Forbidden,
        DbV2Error::QuotaExceeded(_) => invalid("storage_quota"),
        DbV2Error::Conflict(_) | DbV2Error::Contended(_) => invalid("conflict"),
        _ => ToolError::TargetUnavailable,
    }
}

/// Rejects unknown fields so a caller can never smuggle actor/provider data.
fn fields(args: &Value, allowed: &[&str]) -> Result<(), ToolError> {
    let o = args
        .as_object()
        .ok_or_else(|| invalid("args must be an object"))?;
    if o.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(invalid("unknown field"));
    }
    Ok(())
}

/// Every write needs the host-forwarded retry key before any mutation.
fn write(ctx: &ToolContext) -> Result<(), ToolError> {
    if ctx
        .idempotency_key
        .as_ref()
        .is_none_or(|k| k.is_empty() || k.len() > 128)
    {
        return Err(invalid("idempotency key required"));
    }
    Ok(())
}

/// Requires a verified actor holding `scope` (defence in depth behind the host's gate).
fn need_scope(ctx: &ToolContext, scope: &str) -> Result<(), ToolError> {
    if ctx.has_scope(scope) {
        Ok(())
    } else {
        Err(ToolError::Forbidden)
    }
}

/// Days since 1970-01-01 for a proleptic Gregorian date.
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y.rem_euclid(400);
    let mp = if m > 2 { m - 3 } else { m + 9 };
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Proleptic Gregorian date for days since 1970-01-01.
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

/// Formats epoch milliseconds as RFC 3339 UTC with millisecond precision.
pub fn rfc3339(ms: u64) -> String {
    let ms = ms as i64;
    let days = ms.div_euclid(86_400_000);
    let rem = ms.rem_euclid(86_400_000);
    let (y, mo, d) = civil_from_days(days);
    format!(
        "{y:04}-{mo:02}-{d:02}T{:02}:{:02}:{:02}.{:03}Z",
        rem / 3_600_000,
        rem / 60_000 % 60,
        rem / 1000 % 60,
        rem % 1000
    )
}

/// Parses an RFC 3339 timestamp (any fractional precision, `Z` or numeric offset) to epoch ms.
pub fn parse_rfc3339(s: &str) -> Option<u64> {
    let b = s.as_bytes();
    if b.len() < 19 || !s.is_ascii() || (b[10] != b'T' && b[10] != b' ') {
        return None;
    }
    let num = |a: usize, z: usize| s.get(a..z)?.parse::<i64>().ok();
    let (y, mo, d) = (num(0, 4)?, num(5, 7)?, num(8, 10)?);
    let (h, mi, sec) = (num(11, 13)?, num(14, 16)?, num(17, 19)?);
    if b[4] != b'-' || b[7] != b'-' || b[13] != b':' || b[16] != b':' {
        return None;
    }
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) || h > 23 || mi > 59 || sec > 60 {
        return None;
    }
    let mut i = 19;
    let mut frac_ms = 0i64;
    if b.get(i) == Some(&b'.') {
        let start = i + 1;
        i = start;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        let digits = &s[start..i];
        if digits.is_empty() {
            return None;
        }
        let three: String = digits.chars().chain("000".chars()).take(3).collect();
        frac_ms = three.parse().ok()?;
    }
    let offset_min = match b.get(i) {
        Some(b'Z') | Some(b'z') if i + 1 == b.len() => 0,
        Some(&sign @ (b'+' | b'-')) if i + 6 == b.len() && b[i + 3] == b':' => {
            let oh = s.get(i + 1..i + 3)?.parse::<i64>().ok()?;
            let om = s.get(i + 4..i + 6)?.parse::<i64>().ok()?;
            let v = oh * 60 + om;
            if sign == b'+' { v } else { -v }
        }
        None => 0,
        _ => return None,
    };
    let secs = days_from_civil(y, mo, d) * 86_400 + h * 3600 + mi * 60 + sec - offset_min * 60;
    u64::try_from(secs * 1000 + frac_ms).ok()
}

/// SHA-256 digest (FIPS 180-4), used only to derive stable PII-free song keys.
fn sha256(data: &[u8]) -> [u8; 32] {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let mut msg = data.to_vec();
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&((data.len() as u64) * 8).to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0u32; 64];
        for (i, c) in chunk.chunks(4).enumerate() {
            w[i] = u32::from_be_bytes([c[0], c[1], c[2], c[3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let mut v = h;
        for i in 0..64 {
            let s1 = v[4].rotate_right(6) ^ v[4].rotate_right(11) ^ v[4].rotate_right(25);
            let ch = (v[4] & v[5]) ^ (!v[4] & v[6]);
            let t1 = v[7]
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = v[0].rotate_right(2) ^ v[0].rotate_right(13) ^ v[0].rotate_right(22);
            let maj = (v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]);
            let t2 = s0.wrapping_add(maj);
            v = [
                t1.wrapping_add(t2),
                v[0],
                v[1],
                v[2],
                v[3].wrapping_add(t1),
                v[4],
                v[5],
                v[6],
            ];
        }
        for (a, b) in h.iter_mut().zip(v) {
            *a = a.wrapping_add(b);
        }
    }
    let mut out = [0u8; 32];
    for (i, x) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&x.to_be_bytes());
    }
    out
}

/// First 32 hex chars of the SHA-256 of `lower(title)|lower(artist)`: the `song_plays` key.
pub fn song_key(title: &str, artist: &str) -> String {
    let d = sha256(format!("{}|{}", title.to_lowercase(), artist.to_lowercase()).as_bytes());
    d.iter().take(16).map(|b| format!("{b:02x}")).collect()
}

/// Validates a display field (title/artist): trimmed, no control chars, bounded length.
fn text_field(args: &Value, key: &str, required: bool) -> Result<String, ToolError> {
    let s = match args.get(key) {
        None | Some(Value::Null) if !required => return Ok(String::new()),
        Some(Value::String(s)) => s.trim().to_string(),
        _ => return Err(invalid(&format!("{key} must be a string"))),
    };
    let n = s.chars().count();
    if (required && n == 0) || n > 200 || s.chars().any(char::is_control) {
        return Err(invalid(&format!("invalid {key}")));
    }
    Ok(s)
}

/// Splits a validated https URL into lower-case `(host, path)`.
fn split_url(url: &str) -> Option<(String, String)> {
    let rest = url.get(8..)?;
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (authority, tail) = rest.split_at(end);
    let host = match authority.rsplit_once(':') {
        Some((h, port)) if !port.is_empty() && port.bytes().all(|b| b.is_ascii_digit()) => h,
        Some(_) => return None,
        None => authority,
    };
    let path = tail.split(['?', '#']).next().unwrap_or("");
    Some((host.to_ascii_lowercase(), path.to_ascii_lowercase()))
}

/// Validates `source_url`: empty, or https-only, no userinfo, printable ASCII, <= 500 bytes.
pub fn validate_url(url: &str) -> Result<(), ToolError> {
    if url.is_empty() {
        return Ok(());
    }
    let bad = || invalid("invalid_url");
    if url.len() > 500
        || !url
            .bytes()
            .all(|b| (0x21..=0x7e).contains(&b) && b != b'\\')
        || !url
            .get(..8)
            .is_some_and(|p| p.eq_ignore_ascii_case("https://"))
    {
        return Err(bad());
    }
    let (host, _) = split_url(url).ok_or_else(bad)?;
    let rest = &url[8..];
    let authority = &rest[..rest.find(['/', '?', '#']).unwrap_or(rest.len())];
    let ok_host = !host.is_empty()
        && host.contains('.')
        && !host.starts_with(['.', '-'])
        && !host.ends_with(['.', '-'])
        && host
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-');
    if authority.contains('@') || !ok_host {
        return Err(bad());
    }
    Ok(())
}

/// Derives the provider code from a (validated) URL against [`SAFE_SOURCES`]; never from input.
pub fn provider_for(url: &str) -> &'static str {
    if url.is_empty() || validate_url(url).is_err() {
        return PROVIDER_OTHER;
    }
    let Some((host, path)) = split_url(url) else {
        return PROVIDER_OTHER;
    };
    SAFE_SOURCES
        .iter()
        .find(|s| {
            (host == s.host || host.ends_with(&format!(".{}", s.host)))
                && path.starts_with(s.path_prefix)
        })
        .map_or(PROVIDER_OTHER, |s| s.provider)
}

/// A validated, not-yet-stored track.
struct NewItem {
    title: String,
    artist: String,
    source_url: String,
    duration_ms: i64,
    provider: &'static str,
}

/// Parses title/artist/source_url/duration_ms from an object (no other keys are read).
fn parse_new(args: &Value) -> Result<NewItem, ToolError> {
    let title = text_field(args, "title", true)?;
    let artist = text_field(args, "artist", false)?;
    let source_url = match args.get("source_url") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.trim().to_string(),
        _ => return Err(invalid("invalid_url")),
    };
    validate_url(&source_url)?;
    let duration_ms = match args.get("duration_ms") {
        None | Some(Value::Null) => 0,
        Some(v) => v
            .as_i64()
            .filter(|n| (0..=MAX_DURATION_MS).contains(n))
            .ok_or_else(|| invalid("invalid duration_ms"))?,
    };
    let provider = provider_for(&source_url);
    Ok(NewItem {
        title,
        artist,
        source_url,
        duration_ms,
        provider,
    })
}

/// Name for a kind code.
fn kind_name(k: i64) -> &'static str {
    match k {
        KIND_DJ => "dj",
        KIND_IMPORTED => "imported",
        _ => "viewer",
    }
}

/// A stored queue row. `requester` is kept for ownership checks and NEVER serialized.
#[derive(Clone)]
struct Req {
    row_id: String,
    version: u64,
    seq: i64,
    title: String,
    artist: String,
    source_url: String,
    provider: String,
    duration_ms: i64,
    requester: Option<String>,
    kind: i64,
    queued_at: String,
    started_at: Option<String>,
}

impl Req {
    /// Public item JSON: never includes the requester.
    fn item(&self) -> Value {
        json!({
            "id": self.seq, "title": self.title, "artist": self.artist,
            "source_url": self.source_url, "duration_ms": self.duration_ms,
            "kind": kind_name(self.kind), "queued_at": self.queued_at,
            "dmca_safe_item": self.provider != PROVIDER_OTHER,
        })
    }
}

/// Reads an int column.
fn col_i(r: &Row, c: &str) -> Result<i64, ToolError> {
    match r.get(c) {
        Some(Db::Int(i)) => Ok(*i),
        _ => Err(ToolError::TargetUnavailable),
    }
}

/// Reads a text column (absent/null = empty).
fn col_t(r: &Row, c: &str) -> String {
    match r.get(c) {
        Some(Db::Text(t)) => t.clone(),
        _ => String::new(),
    }
}

/// Reads a nullable text column.
fn col_opt(r: &Row, c: &str) -> Option<String> {
    match r.get(c) {
        Some(Db::Text(t)) => Some(t.clone()),
        _ => None,
    }
}

/// Builds a [`Req`] from a `requests` row.
fn parse_req(r: &Row) -> Result<Req, ToolError> {
    Ok(Req {
        row_id: r.row_id.clone(),
        version: r.version,
        seq: col_i(r, "seq")?,
        title: col_t(r, "title"),
        artist: col_t(r, "artist"),
        source_url: col_t(r, "source_url"),
        provider: {
            let p = col_t(r, "provider");
            if p.is_empty() {
                PROVIDER_OTHER.into()
            } else {
                p
            }
        },
        duration_ms: col_i(r, "duration_ms")?,
        requester: col_opt(r, "requester"),
        kind: col_i(r, "kind")?,
        queued_at: col_t(r, "queued_at"),
        started_at: col_opt(r, "started_at"),
    })
}

/// A settings row.
struct Setting {
    key: String,
    ival: i64,
    row_id: String,
    version: u64,
}

/// All settings of the community (a handful of rows) plus typed accessors.
struct Settings(Vec<Setting>);

/// Effective configuration with documented defaults.
struct Config {
    max_per_user: i64,
    max_queue: i64,
    ai_dj_on: bool,
    dmca_safe: bool,
}

impl Settings {
    /// The row for `key`, if stored.
    fn find(&self, key: &str) -> Option<&Setting> {
        self.0.iter().find(|s| s.key == key)
    }
    /// Value for `key` or `default`.
    fn get(&self, key: &str, default: i64) -> i64 {
        self.find(key).map_or(default, |s| s.ival)
    }
    /// Typed config view.
    fn config(&self) -> Config {
        Config {
            max_per_user: self.get("max_per_user", DEFAULT_MAX_PER_USER),
            max_queue: self.get("max_queue", DEFAULT_MAX_QUEUE),
            ai_dj_on: self.get("ai_dj_on", 0) == 1,
            dmca_safe: self.get("dmca_safe", 0) == 1,
        }
    }
    /// Op that sets `key` to `val` (update when stored, insert otherwise).
    fn put(&self, key: &str, val: i64) -> TxOp {
        match self.find(key) {
            Some(s) => TxOp::Update {
                table: "settings".into(),
                row_id: s.row_id.clone(),
                expected_version: s.version,
                columns: vec![("ival".into(), Db::Int(val))],
            },
            None => TxOp::Insert {
                table: "settings".into(),
                columns: vec![("skey".into(), key.into()), ("ival".into(), Db::Int(val))],
            },
        }
    }
    /// Op that fails the batch if the DMCA-safe setting changed since it was read.
    fn dmca_guard(&self) -> TxOp {
        let mut filter = vec![Cond::eq("skey", "dmca_safe")];
        let (min, max) = match self.find("dmca_safe") {
            Some(s) => {
                filter.push(Cond::eq("ival", s.ival));
                (1, 1)
            }
            None => (0, 0),
        };
        TxOp::GuardCount {
            table: "settings".into(),
            filter,
            min,
            max,
        }
    }
    /// Reserves `n` consecutive item ids: the update/insert op and the first id.
    fn seq_ops(&self, n: i64) -> Result<(TxOp, i64), ToolError> {
        let cur = self.get("seq", 0);
        if cur + n > i64::from(i32::MAX) {
            return Err(invalid("id_space_exhausted"));
        }
        Ok((self.put("seq", cur + n), cur + 1))
    }
}

/// Outcome of a transaction attempt.
enum Tx {
    /// Committed results.
    Done(Vec<TxResult>),
    /// Lost a version race or a guard failed: re-read and retry.
    Retry,
}

/// The service: storage, clock and platform seams.
pub struct Service {
    db: Arc<dyn Store>,
    clock: Arc<dyn Clock>,
    platform: Arc<dyn Platform>,
}

impl Service {
    /// Wires the seams.
    pub fn new(db: Arc<dyn Store>, clock: Arc<dyn Clock>, platform: Arc<dyn Platform>) -> Self {
        Self {
            db,
            clock,
            platform,
        }
    }

    /// Current time as RFC 3339.
    fn now(&self) -> String {
        rfc3339(self.clock.now_millis())
    }

    /// Runs one atomic batch; conflicts become `Tx::Retry`, other failures stable errors.
    fn tx(&self, ops: &[TxOp]) -> Result<Tx, ToolError> {
        match self.db.transact(ops) {
            Ok(r) => Ok(Tx::Done(r)),
            Err(DbV2Error::Conflict(_)) | Err(DbV2Error::NotFound) => Ok(Tx::Retry),
            Err(e) => Err(db_err(e)),
        }
    }

    /// Runs `q` and follows cursors (bounded by `max_pages`), collecting every row.
    fn all(&self, mut q: Query, max_pages: u32) -> Result<Vec<Row>, ToolError> {
        let mut rows = Vec::new();
        for _ in 0..max_pages {
            let page = self.db.query(&q).map_err(db_err)?;
            rows.extend(page.rows);
            match page.next_cursor {
                Some(c) => q.cursor = Some(c),
                None => return Ok(rows),
            }
        }
        Err(ToolError::TargetUnavailable)
    }

    /// Loads every settings row.
    fn settings(&self) -> Result<Settings, ToolError> {
        let rows = self.all(Query::table("settings").limit(100), 2)?;
        Ok(Settings(
            rows.iter()
                .filter_map(|r| {
                    Some(Setting {
                        key: col_t(r, "skey"),
                        ival: col_i(r, "ival").ok()?,
                        row_id: r.row_id.clone(),
                        version: r.version,
                    })
                })
                .collect(),
        ))
    }

    /// Queued rows, oldest first.
    fn queue(&self) -> Result<Vec<Req>, ToolError> {
        let q = Query::table("requests")
            .filter(Cond::eq("status", STATUS_QUEUED))
            .order_by("queued_at", false)
            .limit(200);
        self.all(q, 5)?.iter().map(parse_req).collect()
    }

    /// The row currently playing, if any.
    fn playing(&self) -> Result<Option<Req>, ToolError> {
        let q = Query::table("requests")
            .filter(Cond::eq("status", STATUS_PLAYING))
            .order_by("queued_at", false)
            .limit(1);
        let page = self.db.query(&q).map_err(db_err)?;
        page.rows.first().map(parse_req).transpose()
    }

    /// Builds the insert op for a new queue row (requester omitted = NULL). Callers have
    /// already validated the item and the actor.
    fn insert_req(
        &self,
        it: &NewItem,
        seq: i64,
        requester: Option<&str>,
        kind: i64,
        queued_at: &str,
    ) -> TxOp {
        let mut c: Vec<(String, Db)> = vec![
            ("title".into(), it.title.as_str().into()),
            ("artist".into(), it.artist.as_str().into()),
            ("source_url".into(), it.source_url.as_str().into()),
            ("provider".into(), it.provider.into()),
            ("duration_ms".into(), Db::Int(it.duration_ms)),
            ("kind".into(), Db::Int(kind)),
            ("status".into(), Db::Int(STATUS_QUEUED)),
            ("queued_at".into(), queued_at.into()),
            ("seq".into(), Db::Int(seq)),
        ];
        if let Some(r) = requester {
            c.push(("requester".into(), r.into()));
        }
        TxOp::Insert {
            table: "requests".into(),
            columns: c,
        }
    }

    /// `requests.list`.
    fn list(&self, args: Value) -> Result<Value, ToolError> {
        fields(&args, &["limit"])?;
        let limit = opt_range(&args, "limit", 1, 100, 50)? as usize;
        let cfg = self.settings()?.config();
        let queue = self.queue()?;
        let now_playing = self.playing()?.map(|p| p.item());
        Ok(json!({
            "now_playing": now_playing,
            "queue": queue.iter().take(limit).map(Req::item).collect::<Vec<_>>(),
            "total_queued": queue.len(),
            "ai_dj_on": cfg.ai_dj_on,
            "dmca_safe": cfg.dmca_safe,
        }))
    }

    /// `requests.add`.
    fn add(&self, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        write(ctx)?;
        fields(
            &args,
            &["title", "artist", "source_url", "duration_ms", "kind"],
        )?;
        let item = parse_new(&args)?;
        let dj = match args.get("kind") {
            None | Some(Value::Null) => false,
            Some(Value::String(k)) if k == "viewer" => false,
            Some(Value::String(k)) if k == "dj" => true,
            _ => return Err(invalid("invalid kind")),
        };
        let requester = if dj {
            need_scope(ctx, SCOPE_MANAGE)?;
            None
        } else {
            Some(ctx.actor.clone().ok_or(ToolError::Forbidden)?)
        };
        for _ in 0..ATTEMPTS {
            let st = self.settings()?;
            let cfg = st.config();
            if cfg.dmca_safe && item.provider == PROVIDER_OTHER {
                return Err(not_dmca_safe());
            }
            let queue = self.queue()?;
            let playing = self.playing()?;
            if queue.len() as i64 >= cfg.max_queue {
                return Err(invalid("queue_full"));
            }
            if let Some(u) = &requester {
                let mine = queue
                    .iter()
                    .filter(|r| r.requester.as_deref() == Some(u))
                    .count();
                if mine as i64 >= cfg.max_per_user {
                    return Err(invalid("per_user_limit"));
                }
            }
            if queue
                .iter()
                .chain(playing.iter())
                .any(|r| is_duplicate(r, &item))
            {
                return Err(invalid("duplicate"));
            }
            let (seq_op, seq) = st.seq_ops(1)?;
            let mut ops = vec![
                seq_op,
                st.dmca_guard(),
                TxOp::GuardCount {
                    table: "requests".into(),
                    filter: vec![Cond::eq("status", STATUS_QUEUED)],
                    min: 0,
                    max: (cfg.max_queue - 1) as u64,
                },
            ];
            if let Some(u) = &requester {
                ops.push(TxOp::GuardCount {
                    table: "requests".into(),
                    filter: vec![
                        Cond::eq("status", STATUS_QUEUED),
                        Cond::eq("requester", u.as_str()),
                    ],
                    min: 0,
                    max: (cfg.max_per_user - 1) as u64,
                });
            }
            let kind = if dj { KIND_DJ } else { KIND_VIEWER };
            ops.push(self.insert_req(&item, seq, requester.as_deref(), kind, &self.now()));
            if let Tx::Done(res) = self.tx(&ops)? {
                let Some(TxResult::Written(row)) = res.last() else {
                    return Err(ToolError::TargetUnavailable);
                };
                return Ok(json!({
                    "item": parse_req(row)?.item(),
                    "position": queue.len() + 1,
                }));
            }
        }
        Err(invalid("conflict"))
    }

    /// Deletes one queued row by item id, optionally only when owned by `owner`.
    fn delete_queued(&self, id: i64, owner: Option<&str>) -> Result<Value, ToolError> {
        for _ in 0..ATTEMPTS {
            let q = Query::table("requests")
                .filter(Cond::eq("status", STATUS_QUEUED))
                .filter(Cond::eq("seq", id))
                .limit(1);
            let page = self.db.query(&q).map_err(db_err)?;
            let Some(row) = page.rows.first() else {
                return Ok(json!({ "removed": false }));
            };
            let r = parse_req(row)?;
            if let Some(o) = owner
                && r.requester.as_deref() != Some(o)
            {
                return Ok(json!({ "removed": false }));
            }
            let op = TxOp::Delete {
                table: "requests".into(),
                row_id: r.row_id,
                expected_version: r.version,
            };
            if let Tx::Done(_) = self.tx(&[op])? {
                return Ok(json!({ "removed": true }));
            }
        }
        Err(invalid("conflict"))
    }

    /// Ops that bump the artist and song play counters for a finished item.
    fn play_ops(&self, p: &Req) -> Result<Vec<TxOp>, ToolError> {
        let mut ops = Vec::new();
        let mut bump = |table: &str, key_col: &str, key: &str, extra: Vec<(String, Db)>| {
            let q = Query::table(table).filter(Cond::eq(key_col, key)).limit(1);
            let page = self.db.query(&q).map_err(db_err)?;
            ops.push(match page.rows.first() {
                Some(r) => TxOp::Update {
                    table: table.into(),
                    row_id: r.row_id.clone(),
                    expected_version: r.version,
                    columns: vec![("plays".into(), Db::Int(col_i(r, "plays")? + 1))],
                },
                None => {
                    let mut c: Vec<(String, Db)> = vec![(key_col.into(), key.into())];
                    c.extend(extra);
                    c.push(("plays".into(), Db::Int(1)));
                    TxOp::Insert {
                        table: table.into(),
                        columns: c,
                    }
                }
            });
            Ok::<(), ToolError>(())
        };
        if !p.artist.is_empty() {
            bump(
                "artist_plays",
                "artist_key",
                &p.artist.to_lowercase(),
                vec![("artist".into(), p.artist.as_str().into())],
            )?;
        }
        bump(
            "song_plays",
            "song_key",
            &song_key(&p.title, &p.artist),
            vec![
                ("title".into(), p.title.as_str().into()),
                ("artist".into(), p.artist.as_str().into()),
            ],
        )?;
        Ok(ops)
    }

    /// True when the playing row's duration has elapsed (or it has no start time to trust).
    fn due(&self, p: &Req) -> bool {
        if p.duration_ms <= 0 {
            return false;
        }
        match p.started_at.as_deref().and_then(parse_rfc3339) {
            Some(start) => self.clock.now_millis() >= start + p.duration_ms as u64,
            None => true,
        }
    }

    /// Shared advance: finish the playing row (history + stats in the SAME transaction) and
    /// start the next queued one. With `only_if_due` nothing happens unless it is time.
    fn advance(&self, only_if_due: bool) -> Result<Value, ToolError> {
        for _ in 0..ATTEMPTS {
            let playing = self.playing()?;
            let next = self.queue()?.into_iter().next();
            if only_if_due {
                match &playing {
                    Some(p) if !self.due(p) => {
                        return Ok(json!({ "finished": null, "now_playing": p.item() }));
                    }
                    None if next.is_none() => {
                        return Ok(json!({ "finished": null, "now_playing": null }));
                    }
                    _ => {}
                }
            }
            if playing.is_none() && next.is_none() {
                return Ok(json!({ "finished": null, "now_playing": null }));
            }
            let now = self.now();
            let mut ops = Vec::new();
            if let Some(p) = &playing {
                let mut c: Vec<(String, Db)> = vec![
                    ("title".into(), p.title.as_str().into()),
                    ("artist".into(), p.artist.as_str().into()),
                    ("source_url".into(), p.source_url.as_str().into()),
                    ("provider".into(), p.provider.as_str().into()),
                    ("duration_ms".into(), Db::Int(p.duration_ms)),
                    ("played_at".into(), now.as_str().into()),
                    ("kind".into(), Db::Int(p.kind)),
                ];
                if let Some(r) = &p.requester {
                    c.push(("requester".into(), r.as_str().into()));
                }
                ops.push(TxOp::Insert {
                    table: "history".into(),
                    columns: c,
                });
                ops.extend(self.play_ops(p)?);
                ops.push(TxOp::Delete {
                    table: "requests".into(),
                    row_id: p.row_id.clone(),
                    expected_version: p.version,
                });
            }
            if let Some(n) = &next {
                ops.push(TxOp::Update {
                    table: "requests".into(),
                    row_id: n.row_id.clone(),
                    expected_version: n.version,
                    columns: vec![
                        ("status".into(), Db::Int(STATUS_PLAYING)),
                        ("started_at".into(), now.as_str().into()),
                    ],
                });
            }
            if let Tx::Done(_) = self.tx(&ops)? {
                self.prune_history();
                let mut started = next;
                if let Some(n) = started.as_mut() {
                    n.started_at = Some(now);
                }
                return Ok(json!({
                    "finished": playing.as_ref().map(Req::item),
                    "now_playing": started.as_ref().map(Req::item),
                }));
            }
        }
        Err(invalid("conflict"))
    }

    /// Best-effort: keeps history bounded so the community row quota is never exhausted.
    fn prune_history(&self) {
        let ever = vec![Cond::ge("played_at", EPOCH)];
        let Ok(n) = self.db.count("history", &ever) else {
            return;
        };
        if n <= HISTORY_KEEP {
            return;
        }
        let take = (n - HISTORY_KEEP).min(16) as u32;
        let q = Query::table("history")
            .filter(Cond::ge("played_at", EPOCH))
            .order_by("played_at", false)
            .limit(take);
        let Ok(page) = self.db.query(&q) else { return };
        let ops: Vec<TxOp> = page
            .rows
            .iter()
            .map(|r| TxOp::Delete {
                table: "history".into(),
                row_id: r.row_id.clone(),
                expected_version: r.version,
            })
            .collect();
        if !ops.is_empty() {
            let _ = self.db.transact(&ops);
        }
    }

    /// `requests.import`: one-shot seed of an empty community from a legacy queue.
    fn import(&self, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        write(ctx)?;
        fields(&args, &["items"])?;
        let arr = args
            .get("items")
            .and_then(Value::as_array)
            .filter(|a| a.len() <= 100)
            .ok_or_else(|| invalid("items must be an array of at most 100"))?;
        let mut items = Vec::new();
        for v in arr {
            fields(v, &["title", "artist", "source_url", "duration_ms"])?;
            items.push(parse_new(v)?);
        }
        let st = self.settings()?;
        if st.find("imported").is_some() {
            return Err(invalid("already_imported"));
        }
        if st.config().dmca_safe && items.iter().any(|i| i.provider == PROVIDER_OTHER) {
            return Err(not_dmca_safe());
        }
        if items.is_empty() {
            return Ok(json!({ "imported": 0 }));
        }
        let (seq_op, first) = st.seq_ops(items.len() as i64)?;
        let empty = |table: &str, filter: Vec<Cond>| TxOp::GuardCount {
            table: table.into(),
            filter,
            min: 0,
            max: 0,
        };
        let claim = vec![
            empty("requests", vec![Cond::eq("status", STATUS_QUEUED)]),
            empty("requests", vec![Cond::eq("status", STATUS_PLAYING)]),
            empty("history", vec![Cond::ge("played_at", EPOCH)]),
            st.dmca_guard(),
            st.put("imported", 1),
            seq_op,
        ];
        match self.tx(&claim)? {
            Tx::Done(_) => {}
            Tx::Retry => return Err(invalid("not_empty")),
        }
        let base = self.clock.now_millis();
        for (ci, chunk) in items.chunks(16).enumerate() {
            let ops: Vec<TxOp> = chunk
                .iter()
                .enumerate()
                .map(|(i, it)| {
                    let n = ci * 16 + i;
                    self.insert_req(
                        it,
                        first + n as i64,
                        None,
                        KIND_IMPORTED,
                        &rfc3339(base + n as u64),
                    )
                })
                .collect();
            if let Tx::Retry = self.tx(&ops)? {
                return Err(invalid("import_partial"));
            }
        }
        Ok(json!({ "imported": items.len() }))
    }

    /// `history.list`.
    fn history(&self, args: Value) -> Result<Value, ToolError> {
        fields(&args, &["limit"])?;
        let limit = opt_range(&args, "limit", 1, 50, 5)? as u32;
        let q = Query::table("history")
            .filter(Cond::ge("played_at", EPOCH))
            .order_by("played_at", true)
            .limit(limit);
        let page = self.db.query(&q).map_err(db_err)?;
        let items: Vec<Value> = page
            .rows
            .iter()
            .map(|r| {
                let p = col_t(r, "provider");
                json!({
                    "title": col_t(r, "title"), "artist": col_t(r, "artist"),
                    "source_url": col_t(r, "source_url"),
                    "duration_ms": col_i(r, "duration_ms").unwrap_or(0),
                    "played_at": col_t(r, "played_at"),
                    "dmca_safe_item": !p.is_empty() && p != PROVIDER_OTHER,
                })
            })
            .collect();
        Ok(json!({ "items": items }))
    }

    /// `music.stats.top`.
    fn stats_top(&self, args: Value) -> Result<Value, ToolError> {
        fields(&args, &["kind", "limit"])?;
        let kind = args.get("kind").and_then(Value::as_str).unwrap_or("");
        let limit = opt_range(&args, "limit", 1, 25, 10)? as u32;
        let (table, with_title) = match kind {
            "artist" => ("artist_plays", false),
            "song" => ("song_plays", true),
            _ => return Err(invalid("kind must be artist or song")),
        };
        let q = Query::table(table).order_by("plays", true).limit(limit);
        let page = self.db.query(&q).map_err(db_err)?;
        let items: Vec<Value> = page
            .rows
            .iter()
            .map(|r| {
                let mut o = json!({ "artist": col_t(r, "artist"),
                    "plays": col_i(r, "plays").unwrap_or(0) });
                if with_title {
                    o["title"] = json!(col_t(r, "title"));
                }
                o
            })
            .collect();
        Ok(json!({ "items": items }))
    }

    /// `config.get` output.
    fn config_json(&self) -> Result<Value, ToolError> {
        let c = self.settings()?.config();
        Ok(json!({
            "max_per_user": c.max_per_user, "max_queue": c.max_queue,
            "ai_dj_on": c.ai_dj_on, "dmca_safe": c.dmca_safe,
        }))
    }

    /// Upserts one settings value atomically (insert races retry).
    fn set_setting(&self, key: &str, val: i64) -> Result<(), ToolError> {
        for _ in 0..ATTEMPTS {
            let st = self.settings()?;
            if let Tx::Done(_) = self.tx(&[st.put(key, val)])? {
                return Ok(());
            }
        }
        Err(invalid("conflict"))
    }

    /// `config.set`.
    fn config_set(&self, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        write(ctx)?;
        fields(&args, &["max_per_user", "max_queue"])?;
        let per_user = opt_present(&args, "max_per_user", 1, 50)?;
        let max_queue = opt_present(&args, "max_queue", 1, 500)?;
        if let Some(v) = per_user {
            self.set_setting("max_per_user", v)?;
        }
        if let Some(v) = max_queue {
            self.set_setting("max_queue", v)?;
        }
        self.config_json()
    }

    /// `dj.set`.
    fn dj_set(&self, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        write(ctx)?;
        fields(&args, &["enabled"])?;
        let on = args
            .get("enabled")
            .and_then(Value::as_bool)
            .ok_or_else(|| invalid("enabled must be a boolean"))?;
        self.set_setting("ai_dj_on", i64::from(on))?;
        Ok(json!({ "enabled": on }))
    }

    /// `settings.set`: DMCA-safe mode; enabling purges queued non-safe rows (not now playing).
    fn settings_set(&self, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        write(ctx)?;
        fields(&args, &["dmca_safe"])?;
        let on = args
            .get("dmca_safe")
            .and_then(Value::as_bool)
            .ok_or_else(|| invalid("dmca_safe must be a boolean"))?;
        self.set_setting("dmca_safe", i64::from(on))?;
        let mut purged = 0usize;
        if on {
            for _ in 0..32 {
                let victims: Vec<TxOp> = self
                    .queue()?
                    .into_iter()
                    .filter(|r| r.provider == PROVIDER_OTHER)
                    .take(16)
                    .map(|r| TxOp::Delete {
                        table: "requests".into(),
                        row_id: r.row_id,
                        expected_version: r.version,
                    })
                    .collect();
                if victims.is_empty() {
                    break;
                }
                if let Tx::Done(_) = self.tx(&victims)? {
                    purged += victims.len();
                }
            }
        }
        Ok(json!({ "dmca_safe": on, "purged": purged }))
    }

    /// `page.link`: platform web base URL + the numeric community id; fails loud when unset.
    fn page_link(&self) -> Result<Value, ToolError> {
        let base = self
            .platform
            .web_base_url()
            .map(|b| b.trim().trim_end_matches('/').to_string());
        let id = self.platform.community_id();
        match base {
            Some(b)
                if (b.starts_with("https://") || b.starts_with("http://"))
                    && b.len() > 8
                    && id > 0 =>
            {
                Ok(json!({ "url": format!("{b}/community/{id}/music") }))
            }
            _ => Err(invalid("link_not_configured")),
        }
    }
}

/// True when `r` is the same track as `it`: same normalised title+artist, or same non-empty URL.
fn is_duplicate(r: &Req, it: &NewItem) -> bool {
    (r.title.to_lowercase() == it.title.to_lowercase()
        && r.artist.to_lowercase() == it.artist.to_lowercase())
        || (!it.source_url.is_empty() && r.source_url.eq_ignore_ascii_case(&it.source_url))
}

/// Optional integer in `lo..=hi` with a default when absent.
fn opt_range(args: &Value, key: &str, lo: i64, hi: i64, default: i64) -> Result<i64, ToolError> {
    Ok(opt_present(args, key, lo, hi)?.unwrap_or(default))
}

/// Optional integer in `lo..=hi`; `None` when absent, error when present but out of range.
fn opt_present(args: &Value, key: &str, lo: i64, hi: i64) -> Result<Option<i64>, ToolError> {
    match args.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(v) => v
            .as_i64()
            .filter(|n| (lo..=hi).contains(n))
            .map(Some)
            .ok_or_else(|| invalid(&format!("invalid {key}"))),
    }
}

/// Exported tool names, used as bounded log labels.
pub const TOOLS: [&str; 15] = [
    "requests.list",
    "requests.add",
    "requests.cancel_own",
    "requests.remove",
    "requests.advance",
    "requests.advance_due",
    "requests.import",
    "history.list",
    "config.get",
    "config.set",
    "dj.get",
    "dj.set",
    "page.link",
    "music.stats.top",
    "settings.set",
];

/// Maps a tool name to a static log label (never a caller string).
pub fn tool_log_label(name: &str) -> &'static str {
    TOOLS
        .iter()
        .find(|t| **t == name)
        .copied()
        .unwrap_or("unknown")
}

const OUTCOME_CODES: [&str; 11] = [
    "queue_full",
    "per_user_limit",
    "duplicate",
    "invalid_url",
    "not_dmca_safe",
    "link_not_configured",
    "already_imported",
    "not_empty",
    "import_partial",
    "conflict",
    "storage_quota",
];

/// Stable error codes safe to log; anything else collapses to `invalid_args`.
pub fn outcome_code(msg: &str) -> &'static str {
    let code = msg.split(':').next().unwrap_or("");
    OUTCOME_CODES
        .iter()
        .find(|c| **c == code)
        .copied()
        .unwrap_or("invalid_args")
}

/// Builds the full tool table. Scopes are enforced by the host from the manifest's
/// `required_scope`; sensitive ones are re-checked here against the verified context.
pub fn build_registry(svc: Arc<Service>) -> ToolRegistry {
    let mut r = ToolRegistry::new();
    macro_rules! tool {
        ($name:expr, |$s:ident, $c:ident, $a:ident| $body:expr) => {{
            let $s = svc.clone();
            r.register($name, move |$c: &ToolContext, $a: Value| {
                let _ = &$c;
                $body
            });
        }};
    }
    tool!("requests.list", |s, _c, a| s.list(a));
    tool!("requests.add", |s, c, a| s.add(c, a));
    tool!("requests.cancel_own", |s, c, a| {
        write(c)?;
        fields(&a, &["id"])?;
        let actor = c.actor.clone().ok_or(ToolError::Forbidden)?;
        let id = a
            .get("id")
            .and_then(Value::as_i64)
            .ok_or_else(|| invalid("id must be an integer"))?;
        s.delete_queued(id, Some(&actor))
    });
    tool!("requests.remove", |s, c, a| {
        write(c)?;
        need_scope(c, SCOPE_MANAGE)?;
        fields(&a, &["id"])?;
        let id = a
            .get("id")
            .and_then(Value::as_i64)
            .ok_or_else(|| invalid("id must be an integer"))?;
        s.delete_queued(id, None)
    });
    tool!("requests.advance", |s, c, a| {
        write(c)?;
        need_scope(c, SCOPE_MANAGE)?;
        fields(&a, &[])?;
        s.advance(false)
    });
    tool!("requests.advance_due", |s, _c, a| {
        fields(&a, &[])?;
        s.advance(true)
    });
    tool!("requests.import", |s, c, a| s.import(c, a));
    tool!("history.list", |s, _c, a| s.history(a));
    tool!("config.get", |s, _c, a| {
        fields(&a, &[])?;
        s.config_json()
    });
    tool!("config.set", |s, c, a| {
        need_scope(c, SCOPE_MANAGE)?;
        s.config_set(c, a)
    });
    tool!("dj.get", |s, _c, a| {
        fields(&a, &[])?;
        Ok(json!({ "enabled": s.settings()?.config().ai_dj_on }))
    });
    tool!("dj.set", |s, c, a| {
        need_scope(c, SCOPE_DJ_MANAGE)?;
        s.dj_set(c, a)
    });
    tool!("music.stats.top", |s, _c, a| s.stats_top(a));
    tool!("settings.set", |s, c, a| {
        need_scope(c, SCOPE_SETTINGS_WRITE)?;
        s.settings_set(c, a)
    });
    tool!("page.link", |s, _c, a| {
        fields(&a, &[])?;
        s.page_link()
    });
    r
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
