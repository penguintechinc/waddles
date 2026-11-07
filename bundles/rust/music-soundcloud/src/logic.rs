//! Host-independent SoundCloud tool logic.
//!
//! Every tool is a function over three seams -- [`Upstream`] (the guarded `http`
//! import), [`Store`] (the bundle's own community-scoped kv) and [`Clock`] -- so
//! the whole tool table is unit-tested on the host with fakes while `lib.rs`
//! supplies the real wasm implementations.
//!
//! Upstream API (verified 2026-11-07):
//! - Guide / auth: <https://developers.soundcloud.com/docs/api/guide> (OAuth 2.1,
//!   `Authorization: OAuth ACCESS_TOKEN`, client-credentials grant for public
//!   resources, base URL `https://api.soundcloud.com`, token endpoint
//!   `https://secure.soundcloud.com/oauth/token`).
//! - OpenAPI: <https://developers.soundcloud.com/docs/api/explorer/api.json>
//!   (`GET /tracks`, `/tracks/{track_urn}`, `/playlists/{playlist_urn}`,
//!   `/playlists/{playlist_urn}/tracks`, `/resolve` which answers 302).
//!
//! Safety shape: args are validated first and unknown fields rejected; the token
//! never exists in this component (a header secret-ref the host fills in); the
//! bundle returns METADATA + license/permission flags only -- never a stream
//! URL, never audio (see the catalog spec section 4: embeds only). Upstream
//! calls are cached with a TTL, counted against a per-community fixed-window
//! limit, and a 429 opens a kv-backed backoff so later calls fail fast without
//! touching SoundCloud. A limiter that cannot count fails closed (loud error).

use std::sync::Arc;

use serde_json::{Value, json};
use waddle_sdk::tool::{ToolContext, ToolError, ToolRegistry};

/// Provider tag in every output.
pub const PROVIDER: &str = "soundcloud";
/// SoundCloud API base (the only egress host).
pub const API_BASE: &str = "https://api.soundcloud.com";
/// Header secret-ref: header `Authorization` is filled host-side with the
/// full value `OAuth <access token>` from connection handle
/// `soundcloud.access_token`.
pub const AUTH_REF: (&str, &str) = ("Authorization", "soundcloud.access_token");

/// Exported tool names (must match both manifests).
pub const TOOL_SEARCH: &str = "music.soundcloud.search";
pub const TOOL_TRACK: &str = "music.soundcloud.track";
pub const TOOL_PLAYLIST: &str = "music.soundcloud.playlist.get";
pub const TOOL_RESOLVE: &str = "music.soundcloud.resolve";
/// All tool names, in manifest order.
pub const TOOL_NAMES: [&str; 4] = [TOOL_SEARCH, TOOL_TRACK, TOOL_PLAYLIST, TOOL_RESOLVE];

/// Cache TTLs in seconds.
pub const TTL_SEARCH: u32 = 300;
pub const TTL_TRACK: u32 = 3600;
pub const TTL_PLAYLIST: u32 = 600;
pub const TTL_RESOLVE: u32 = 3600;
/// Upstream calls allowed per community per minute (cache hits are free).
pub const RATE_LIMIT_PER_MIN: i64 = 30;
/// Backoff bounds when SoundCloud answers 429.
pub const BACKOFF_DEFAULT_S: u32 = 30;
pub const BACKOFF_MAX_S: u32 = 300;
/// Search/playlist page bounds.
pub const MAX_SEARCH_LIMIT: u64 = 25;
pub const MAX_PLAYLIST_LIMIT: u64 = 50;
pub const DEFAULT_SEARCH_LIMIT: u64 = 10;
pub const DEFAULT_PLAYLIST_LIMIT: u64 = 25;
const MAX_QUERY_LEN: usize = 200;
const MAX_URL_LEN: usize = 200;
const MAX_TEXT: usize = 300;

const KEY_BACKOFF: &str = "backoff";

/// One upstream HTTP answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpOut {
    pub status: u16,
    pub body: Vec<u8>,
    /// `Location` response header, when present (the `/resolve` 302).
    pub location: Option<String>,
    /// `Retry-After` seconds, when present and numeric.
    pub retry_after: Option<u32>,
}

/// Transport-level failure of the guarded `http` import.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HttpFail {
    /// Egress / secret-ref denied by the host (also: credential unresolved).
    Denied,
    Timeout,
    Transport,
    /// Host-side rate limit with its suggested wait.
    RateLimited(u32),
}

/// Guarded outbound GET carrying [`AUTH_REF`] (wasm glue: the `http` import).
pub trait Upstream: Send + Sync {
    fn get(&self, url: &str) -> Result<HttpOut, HttpFail>;
}

/// The bundle's own community-scoped kv.
pub trait Store: Send + Sync {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    fn set(&self, key: &str, value: &[u8], ttl_s: u32) -> Result<(), String>;
    fn incr(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, String>;
}

/// Wall clock (seconds since the epoch).
pub trait Clock: Send + Sync {
    fn now_secs(&self) -> u64;
}

fn invalid(msg: impl Into<String>) -> ToolError {
    ToolError::InvalidArgs(msg.into())
}

/// Rejects any field not in `allowed` (a typo must fail, not be ignored).
fn only_fields(args: &Value, allowed: &[&str]) -> Result<(), ToolError> {
    let obj = args
        .as_object()
        .ok_or_else(|| invalid("args must be an object"))?;
    match obj.keys().find(|k| !allowed.contains(&k.as_str())) {
        Some(k) => Err(invalid(format!("unknown field {k}"))),
        None => Ok(()),
    }
}

fn bounded_u64(
    args: &Value,
    key: &str,
    min: u64,
    max: u64,
    default: u64,
) -> Result<u64, ToolError> {
    match args.get(key) {
        None | Some(Value::Null) => Ok(default),
        Some(v) => v
            .as_u64()
            .filter(|n| (min..=max).contains(n))
            .ok_or_else(|| invalid(format!("{key} must be an integer {min}..={max}"))),
    }
}

fn str_field<'a>(args: &'a Value, key: &str) -> Result<&'a str, ToolError> {
    args.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("{key} must be a string")))
}

/// Percent-encodes everything but RFC 3986 unreserved characters.
pub fn pct_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// FNV-1a 64 as 16 hex chars: a kv-safe cache key fragment (kv keys contain no
/// ':' and may not carry raw user text).
pub fn fnv_hex(s: &str) -> String {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in s.bytes() {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    format!("{h:016x}")
}

/// Normalizes a track id (`123` or `soundcloud:tracks:123`) to the URN.
pub fn parse_track_id(raw: &str) -> Result<String, ToolError> {
    parse_urn(raw, "tracks")
}

/// Normalizes a playlist id (`123` or `soundcloud:playlists:123`) to the URN.
pub fn parse_playlist_id(raw: &str) -> Result<String, ToolError> {
    parse_urn(raw, "playlists")
}

fn parse_urn(raw: &str, kind: &str) -> Result<String, ToolError> {
    let prefix = format!("soundcloud:{kind}:");
    let digits = raw.strip_prefix(prefix.as_str()).unwrap_or(raw);
    if (1..=20).contains(&digits.len()) && digits.bytes().all(|b| b.is_ascii_digit()) {
        Ok(format!("{prefix}{digits}"))
    } else {
        Err(invalid(format!("id must be a SoundCloud {kind} id or URN")))
    }
}

/// Validates a public SoundCloud permalink and returns its canonical form
/// (`https://soundcloud.com/<path>`). Only https, only the soundcloud.com
/// hosts, no query/fragment/userinfo/port, 1..=3 plain path segments.
pub fn normalize_permalink(raw: &str) -> Result<String, ToolError> {
    let bad = || invalid("url must be a https://soundcloud.com permalink");
    if raw.len() > MAX_URL_LEN {
        return Err(bad());
    }
    let rest = raw.strip_prefix("https://").ok_or_else(bad)?;
    let (host, path) = rest.split_once('/').ok_or_else(bad)?;
    if !matches!(
        host.to_ascii_lowercase().as_str(),
        "soundcloud.com" | "www.soundcloud.com" | "m.soundcloud.com"
    ) {
        return Err(bad());
    }
    let path = path.trim_end_matches('/');
    let ok_char = |b: u8| b.is_ascii_alphanumeric() || b"._~%-".contains(&b);
    let segs: Vec<&str> = path.split('/').collect();
    if !(1..=3).contains(&segs.len())
        || segs
            .iter()
            .any(|s| s.is_empty() || *s == "." || *s == ".." || !s.bytes().all(ok_char))
    {
        return Err(bad());
    }
    Ok(format!("https://soundcloud.com/{path}"))
}

fn trunc(s: &str) -> String {
    s.chars().take(MAX_TEXT).collect()
}

fn opt_str(v: &Value, key: &str) -> Value {
    match v.get(key).and_then(Value::as_str) {
        Some(s) if !s.is_empty() => Value::String(trunc(s)),
        _ => Value::Null,
    }
}

/// License + permission block (a documented extension of the shared music
/// track contract). These are the flags the DJ app enforces DMCA-safe rules
/// from; absence is reported as `null`, never defaulted to "allowed".
fn license_json(v: &Value) -> Value {
    let lic = v
        .get("license")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    json!({
        "type": lic,
        "creative_commons": lic.is_some_and(|l| l.starts_with("cc-")),
        "all_rights_reserved": lic.map(|l| l == "all-rights-reserved"),
        "streamable": v.get("streamable").and_then(Value::as_bool),
        "downloadable": v.get("downloadable").and_then(Value::as_bool),
        "access": v.get("access").and_then(Value::as_str),
        "sharing": v.get("sharing").and_then(Value::as_str),
    })
}

fn malformed() -> ToolError {
    ToolError::TargetUnavailable
}

/// Maps one upstream track object to the shared music-track contract
/// (`bundles/contracts/music-tool-contract.v1.json`) plus provider extras:
/// `artist_id`, `genre`, `license{..}`, `embeddable`, `full_playback`.
/// A track missing a contract-required field is malformed (callers in list
/// tools skip + count it; single-track tools fail loud).
pub fn track_json(v: &Value) -> Result<Value, ToolError> {
    let obj = v.as_object().ok_or_else(malformed)?;
    let urn = match obj.get("urn").and_then(Value::as_str) {
        Some(u) if !u.is_empty() => u.to_string(),
        _ => format!(
            "soundcloud:tracks:{}",
            obj.get("id")
                .and_then(Value::as_u64)
                .ok_or_else(malformed)?
        ),
    };
    let title = obj
        .get("title")
        .and_then(Value::as_str)
        .ok_or_else(malformed)?;
    let user = obj.get("user");
    let artist = obj
        .get("metadata_artist")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or_else(|| user.and_then(|u| u.get("username")).and_then(Value::as_str))
        .map(trunc)
        .ok_or_else(malformed)?;
    let duration_ms = obj
        .get("duration")
        .and_then(Value::as_u64)
        .ok_or_else(malformed)?;
    let source_url = obj
        .get("permalink_url")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(malformed)?;
    let access = obj.get("access").and_then(Value::as_str);
    let streamable = obj.get("streamable").and_then(Value::as_bool);
    let sharing = obj.get("sharing").and_then(Value::as_str);
    let embeddable_by = obj.get("embeddable_by").and_then(Value::as_str);
    let embeddable = access.is_some_and(|a| a != "blocked")
        && streamable != Some(false)
        && sharing != Some("private")
        && !matches!(embeddable_by, Some("none") | Some("me"));
    let full_playback = embeddable && access == Some("playable");
    Ok(json!({
        "provider": PROVIDER,
        "id": urn,
        "title": trunc(title),
        "artist": artist,
        "duration_ms": duration_ms,
        "thumbnail": opt_str(v, "artwork_url"),
        // SoundCloud exposes no explicit-content flag: unknown, not "clean".
        "explicit": Value::Null,
        "source_url": trunc(source_url),
        "artist_id": user.and_then(|u| u.get("urn")).and_then(Value::as_str),
        "genre": opt_str(v, "genre"),
        "license": license_json(v),
        // Provider widget embed is the only playback; per-track permission.
        "embeddable": embeddable,
        "full_playback": full_playback,
    }))
}

/// Contract `capability` for one track: widget embed when permitted, else
/// none. Waddles never serves or downloads audio (`server_audio:false`) and
/// SoundCloud has no queue API (`queue_add:false`).
fn track_capability(track: &Value) -> Value {
    let playback = if track["embeddable"].as_bool() == Some(true) {
        "embed"
    } else {
        "none"
    };
    json!({ "playback": playback, "server_audio": false, "queue_add": false })
}

/// Provider-level capability for list tools (per-track `embeddable` refines it).
fn provider_capability() -> Value {
    json!({ "playback": "embed", "server_audio": false, "queue_add": false })
}

fn tracks_of(v: &Value) -> Result<&Vec<Value>, ToolError> {
    match v {
        Value::Array(a) => Ok(a),
        Value::Object(o) => o
            .get("collection")
            .and_then(Value::as_array)
            .ok_or_else(malformed),
        _ => Err(malformed()),
    }
}

/// Maps rows, dropping (and counting) tracks missing contract-required fields.
fn collect_tracks<'a>(rows: impl Iterator<Item = &'a Value>) -> (Vec<Value>, u64) {
    let (mut out, mut skipped) = (Vec::new(), 0u64);
    for r in rows {
        match track_json(r) {
            Ok(t) => out.push(t),
            Err(_) => skipped += 1,
        }
    }
    (out, skipped)
}

/// `{track, cached, capability}` for single-track outputs.
fn track_out(track: Value, cached: bool) -> Value {
    let capability = track_capability(&track);
    json!({ "track": track, "cached": cached, "capability": capability })
}

/// Outcome of a successful upstream GET.
enum Fetched {
    Json(Value),
    Redirect(String),
}

/// The SoundCloud tool implementation over its three seams.
pub struct Soundcloud {
    up: Arc<dyn Upstream>,
    store: Arc<dyn Store>,
    clock: Arc<dyn Clock>,
}

impl Soundcloud {
    pub fn new(up: Arc<dyn Upstream>, store: Arc<dyn Store>, clock: Arc<dyn Clock>) -> Self {
        Self { up, store, clock }
    }

    fn cache_get(&self, key: &str) -> Option<Value> {
        let bytes = self.store.get(key).ok().flatten()?;
        serde_json::from_slice(&bytes).ok()
    }

    /// Best-effort cache write: a full/failed cache costs a refetch, never a result.
    fn cache_put(&self, key: &str, v: &Value, ttl: u32) {
        if let Ok(bytes) = serde_json::to_vec(v) {
            let _ = self.store.set(key, &bytes, ttl);
        }
    }

    fn open_backoff(&self, secs: Option<u32>) {
        let s = secs.unwrap_or(BACKOFF_DEFAULT_S).clamp(1, BACKOFF_MAX_S);
        // If even the backoff marker cannot be written the 429 is still
        // returned to the caller; the per-minute limiter bounds the retry rate.
        let _ = self.store.set(KEY_BACKOFF, b"1", s);
    }

    /// Backoff gate + per-community fixed-window limiter. Fails closed.
    fn admit(&self) -> Result<(), ToolError> {
        match self.store.get(KEY_BACKOFF) {
            Ok(Some(_)) => return Err(ToolError::PoolExhausted),
            Ok(None) => {}
            Err(_) => return Err(ToolError::TargetUnavailable),
        }
        let window = self.clock.now_secs() / 60;
        let n = self
            .store
            .incr(&format!("rl.{window}"), 1, 120)
            .map_err(|_| ToolError::TargetUnavailable)?;
        if n > RATE_LIMIT_PER_MIN {
            Err(ToolError::PoolExhausted)
        } else {
            Ok(())
        }
    }

    /// One admitted, authenticated upstream GET with status mapping.
    fn fetch(&self, path_and_query: &str) -> Result<Fetched, ToolError> {
        self.admit()?;
        let url = format!("{API_BASE}{path_and_query}");
        let out = match self.up.get(&url) {
            Ok(o) => o,
            Err(HttpFail::RateLimited(s)) => {
                self.open_backoff(Some(s));
                return Err(ToolError::PoolExhausted);
            }
            Err(HttpFail::Denied) => return Err(ToolError::Forbidden),
            Err(HttpFail::Timeout) => return Err(ToolError::Timeout),
            Err(HttpFail::Transport) => return Err(ToolError::TargetUnavailable),
        };
        match out.status {
            200 => serde_json::from_slice(&out.body)
                .map(Fetched::Json)
                .map_err(|_| malformed()),
            301 | 302 | 303 | 307 | 308 => {
                out.location.map(Fetched::Redirect).ok_or_else(malformed)
            }
            401 | 403 => Err(ToolError::Forbidden),
            400 | 404 | 422 => Err(invalid(if out.status == 404 {
                "not_found"
            } else {
                "rejected_by_soundcloud"
            })),
            429 => {
                self.open_backoff(out.retry_after);
                Err(ToolError::PoolExhausted)
            }
            _ => Err(ToolError::TargetUnavailable),
        }
    }

    fn fetch_json(&self, path_and_query: &str) -> Result<Value, ToolError> {
        match self.fetch(path_and_query)? {
            Fetched::Json(v) => Ok(v),
            Fetched::Redirect(_) => Err(malformed()),
        }
    }

    /// Cache read: marks the stored output `cached: true`.
    fn cached_out(&self, key: &str) -> Option<Value> {
        let mut v = self.cache_get(key)?;
        v["cached"] = Value::Bool(true);
        Some(v)
    }

    /// `music.soundcloud.search {query, limit?}`.
    pub fn search(&self, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        only_fields(&args, &["query", "limit"])?;
        let q = str_field(&args, "query")?.trim();
        if q.is_empty() || q.chars().count() > MAX_QUERY_LEN || q.chars().any(char::is_control) {
            return Err(invalid(format!(
                "query must be 1..={MAX_QUERY_LEN} printable characters"
            )));
        }
        let limit = bounded_u64(&args, "limit", 1, MAX_SEARCH_LIMIT, DEFAULT_SEARCH_LIMIT)?;
        let key = format!(
            "cache.search.{}",
            fnv_hex(&format!("{}|{limit}", q.to_lowercase()))
        );
        if let Some(hit) = self.cached_out(&key) {
            return Ok(hit);
        }
        let body = self.fetch_json(&format!("/tracks?q={}&limit={limit}", pct_encode(q)))?;
        let (tracks, skipped) = collect_tracks(tracks_of(&body)?.iter());
        let out = json!({
            "tracks": tracks,
            "cached": false,
            "skipped": skipped,
            "capability": provider_capability(),
        });
        self.cache_put(&key, &out, TTL_SEARCH);
        Ok(out)
    }

    /// `music.soundcloud.track {id}`.
    pub fn track(&self, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        only_fields(&args, &["id"])?;
        let urn = parse_track_id(str_field(&args, "id")?)?;
        self.track_by_urn(&urn)
    }

    fn track_by_urn(&self, urn: &str) -> Result<Value, ToolError> {
        let key = format!("cache.track.{}", fnv_hex(urn));
        if let Some(hit) = self.cached_out(&key) {
            return Ok(hit);
        }
        let body = self.fetch_json(&format!("/tracks/{urn}"))?;
        let out = track_out(track_json(&body)?, false);
        self.cache_put(&key, &out, TTL_TRACK);
        Ok(out)
    }

    /// `music.soundcloud.playlist.get {playlist_id, limit?, page_token?}`.
    /// `page_token` is an opaque `o<offset>` cursor into the upstream list.
    pub fn playlist_get(&self, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        only_fields(&args, &["playlist_id", "limit", "page_token"])?;
        let urn = parse_playlist_id(str_field(&args, "playlist_id")?)?;
        let limit = bounded_u64(
            &args,
            "limit",
            1,
            MAX_PLAYLIST_LIMIT,
            DEFAULT_PLAYLIST_LIMIT,
        )?;
        let offset = match args.get("page_token") {
            None | Some(Value::Null) => 0usize,
            Some(Value::String(t)) => t
                .strip_prefix('o')
                .filter(|d| (1..=6).contains(&d.len()) && d.bytes().all(|b| b.is_ascii_digit()))
                .and_then(|d| d.parse().ok())
                .ok_or_else(|| invalid("page_token is not a valid cursor"))?,
            Some(_) => return Err(invalid("page_token must be a string")),
        };
        let key = format!(
            "cache.playlist.{}",
            fnv_hex(&format!("{urn}|{limit}|{offset}"))
        );
        if let Some(hit) = self.cached_out(&key) {
            return Ok(hit);
        }
        let body = self.fetch_json(&format!("/playlists/{urn}/tracks"))?;
        let rows = tracks_of(&body)?;
        let page: Vec<&Value> = rows.iter().skip(offset).take(limit as usize).collect();
        let (tracks, skipped) = collect_tracks(page.into_iter());
        let next = offset + limit as usize;
        let next_page_token = (rows.len() > next).then(|| format!("o{next}"));
        let out = json!({
            "tracks": tracks,
            "next_page_token": next_page_token,
            "cached": false,
            "skipped": skipped,
            "capability": provider_capability(),
        });
        self.cache_put(&key, &out, TTL_PLAYLIST);
        Ok(out)
    }

    /// `music.soundcloud.resolve {url}` -> `{track, cached, capability}`.
    /// Playlist/user permalinks fail loud (`unsupported_resource_kind`): use
    /// `playlist.get` with the playlist id.
    pub fn resolve(&self, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        only_fields(&args, &["url"])?;
        let permalink = normalize_permalink(str_field(&args, "url")?)?;
        let key = format!("cache.resolve.{}", fnv_hex(&permalink));
        if let Some(hit) = self.cached_out(&key) {
            return Ok(hit);
        }
        let out = match self.fetch(&format!("/resolve?url={}", pct_encode(&permalink)))? {
            // 302 -> the resource URL on the API host; re-fetch it ourselves so
            // the credential only ever rides to api.soundcloud.com.
            Fetched::Redirect(loc) => match resource_from_location(&loc)? {
                Resource::Track(urn) => self.track_by_urn(&urn)?,
                Resource::Playlist => return Err(invalid("unsupported_resource_kind")),
            },
            // The host followed the redirect: classify the final object.
            Fetched::Json(v) => match v.get("kind").and_then(Value::as_str) {
                Some("track") => track_out(track_json(&v)?, false),
                _ => return Err(invalid("unsupported_resource_kind")),
            },
        };
        let mut stored = out.clone();
        stored["cached"] = Value::Bool(false);
        self.cache_put(&key, &stored, TTL_RESOLVE);
        Ok(out)
    }
}

enum Resource {
    Track(String),
    Playlist,
}

/// Parses a `/resolve` redirect target. Only the API host and the
/// track/playlist collections are followed; anything else fails loud.
fn resource_from_location(loc: &str) -> Result<Resource, ToolError> {
    let rest = loc
        .strip_prefix("https://api.soundcloud.com/")
        .ok_or_else(malformed)?;
    let rest = rest.split(['?', '#']).next().unwrap_or(rest);
    let rest = rest.replace("%3A", ":").replace("%3a", ":");
    let (kind, id) = rest.split_once('/').ok_or_else(malformed)?;
    match kind {
        "tracks" => parse_track_id(id)
            .map(Resource::Track)
            .map_err(|_| malformed()),
        "playlists" => parse_playlist_id(id)
            .map(|_| Resource::Playlist)
            .map_err(|_| malformed()),
        _ => Err(invalid("unsupported_resource_kind")),
    }
}

/// A stable, caller-text-free log label for a tool name.
pub fn tool_log_label(name: &str) -> &'static str {
    TOOL_NAMES
        .iter()
        .copied()
        .find(|t| *t == name)
        .unwrap_or("unknown")
}

/// Builds the tool table.
pub fn build_registry(sc: Arc<Soundcloud>) -> ToolRegistry {
    let mut r = ToolRegistry::new();
    let s = sc.clone();
    r.register(TOOL_SEARCH, move |c, a| s.search(c, a));
    let s = sc.clone();
    r.register(TOOL_TRACK, move |c, a| s.track(c, a));
    let s = sc.clone();
    r.register(TOOL_PLAYLIST, move |c, a| s.playlist_get(c, a));
    let s = sc;
    r.register(TOOL_RESOLVE, move |c, a| s.resolve(c, a));
    r
}

/// In-memory fakes shared by the unit and integration tests.
pub mod fakes {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Mutex;

    /// Scripted upstream: pops queued answers, records every URL.
    #[derive(Default)]
    pub struct FakeUpstream {
        pub queue: Mutex<Vec<Result<HttpOut, HttpFail>>>,
        pub urls: Mutex<Vec<String>>,
    }
    impl FakeUpstream {
        pub fn with(answers: Vec<Result<HttpOut, HttpFail>>) -> Arc<Self> {
            let mut a = answers;
            a.reverse();
            Arc::new(Self {
                queue: Mutex::new(a),
                urls: Mutex::default(),
            })
        }
        pub fn calls(&self) -> Vec<String> {
            self.urls.lock().unwrap().clone()
        }
    }
    impl Upstream for FakeUpstream {
        fn get(&self, url: &str) -> Result<HttpOut, HttpFail> {
            self.urls.lock().unwrap().push(url.to_string());
            self.queue
                .lock()
                .unwrap()
                .pop()
                .unwrap_or(Err(HttpFail::Transport))
        }
    }

    /// A 200 JSON answer.
    pub fn ok(v: Value) -> Result<HttpOut, HttpFail> {
        Ok(HttpOut {
            status: 200,
            body: serde_json::to_vec(&v).unwrap(),
            location: None,
            retry_after: None,
        })
    }
    /// A bare status answer.
    pub fn status(
        s: u16,
        location: Option<&str>,
        retry_after: Option<u32>,
    ) -> Result<HttpOut, HttpFail> {
        Ok(HttpOut {
            status: s,
            body: Vec::new(),
            location: location.map(str::to_string),
            retry_after,
        })
    }

    /// kv with TTL tracking; `fail` makes every op error.
    #[derive(Default)]
    pub struct FakeStore {
        pub map: Mutex<HashMap<String, (Vec<u8>, u32)>>,
        pub fail: Mutex<bool>,
    }
    impl Store for FakeStore {
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            if *self.fail.lock().unwrap() {
                return Err("kv".into());
            }
            Ok(self.map.lock().unwrap().get(key).map(|(v, _)| v.clone()))
        }
        fn set(&self, key: &str, value: &[u8], ttl_s: u32) -> Result<(), String> {
            if *self.fail.lock().unwrap() {
                return Err("kv".into());
            }
            self.map
                .lock()
                .unwrap()
                .insert(key.to_string(), (value.to_vec(), ttl_s));
            Ok(())
        }
        fn incr(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, String> {
            if *self.fail.lock().unwrap() {
                return Err("kv".into());
            }
            let mut m = self.map.lock().unwrap();
            let cur = m
                .get(key)
                .map(|(v, _)| String::from_utf8_lossy(v).parse::<i64>().unwrap_or(0))
                .unwrap_or(0);
            let n = cur + delta;
            m.insert(key.to_string(), (n.to_string().into_bytes(), ttl_s));
            Ok(n)
        }
    }

    /// Fixed clock.
    pub struct FixedClock(pub u64);
    impl Clock for FixedClock {
        fn now_secs(&self) -> u64 {
            self.0
        }
    }

    /// Wires a service over fakes.
    pub fn service(up: Arc<FakeUpstream>, store: Arc<FakeStore>) -> Arc<Soundcloud> {
        Arc::new(Soundcloud::new(
            up,
            store,
            Arc::new(FixedClock(1_800_000_000)),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::fakes::*;
    use super::*;

    fn track(id: u64, extra: Value) -> Value {
        let mut t = json!({
            "urn": format!("soundcloud:tracks:{id}"), "title": "Song", "duration": 200_001,
            "permalink_url": "https://soundcloud.com/a/song", "access": "playable",
            "artwork_url": "https://i1.sndcdn.com/a.jpg",
            "streamable": true, "downloadable": false, "license": "cc-by", "sharing": "public",
            "user": {"username": "Artist", "urn": "soundcloud:users:9"}
        });
        for (k, v) in extra.as_object().unwrap() {
            t[k] = v.clone();
        }
        t
    }

    fn call(sc: &Arc<Soundcloud>, name: &str, args: Value) -> Result<Value, ToolError> {
        let reg = build_registry(sc.clone());
        reg.call(name, &serde_json::to_vec(&args).unwrap())
            .map(|b| serde_json::from_slice(&b).unwrap())
    }

    #[test]
    fn track_matches_contract_shape_and_keeps_license() {
        let v = track_json(&track(1, json!({}))).unwrap();
        assert_eq!(v["provider"], "soundcloud");
        assert_eq!(v["id"], "soundcloud:tracks:1");
        assert_eq!(v["duration_ms"], 200_001);
        assert_eq!(v["source_url"], "https://soundcloud.com/a/song");
        assert_eq!(v["thumbnail"], "https://i1.sndcdn.com/a.jpg");
        assert_eq!(v["explicit"], Value::Null);
        assert_eq!(v["license"]["creative_commons"], true);
        assert_eq!(v["license"]["type"], "cc-by");
        assert_eq!(v["embeddable"], true);
        for old in ["url", "duration_s", "capability", "stream_url"] {
            assert!(v.get(old).is_none(), "{old} must not be on a track");
        }
    }

    #[test]
    fn blocked_private_or_unembeddable_tracks_get_playback_none() {
        for extra in [
            json!({"access": "blocked"}),
            json!({"sharing": "private"}),
            json!({"streamable": false}),
            json!({"embeddable_by": "none"}),
        ] {
            let t = track_json(&track(1, extra)).unwrap();
            assert_eq!(t["embeddable"], false);
            assert_eq!(t["full_playback"], false);
            assert_eq!(track_capability(&t)["playback"], "none");
        }
        let p = track_json(&track(1, json!({"access": "preview"}))).unwrap();
        assert_eq!(p["embeddable"], true);
        assert_eq!(p["full_playback"], false);
        assert_eq!(track_capability(&p)["playback"], "embed");
        // Missing access is unknown -> not embeddable (never default-allow).
        let mut t = track(1, json!({}));
        t.as_object_mut().unwrap().remove("access");
        assert_eq!(track_json(&t).unwrap()["embeddable"], false);
    }

    #[test]
    fn capability_never_claims_server_audio_or_queue() {
        let t = track_json(&track(1, json!({}))).unwrap();
        let c = track_capability(&t);
        assert_eq!(c["server_audio"], false);
        assert_eq!(c["queue_add"], false);
        assert_eq!(provider_capability()["server_audio"], false);
    }

    #[test]
    fn all_rights_reserved_is_flagged() {
        let v = track_json(&track(1, json!({"license": "all-rights-reserved"}))).unwrap();
        assert_eq!(v["license"]["all_rights_reserved"], true);
        assert_eq!(v["license"]["creative_commons"], false);
    }

    #[test]
    fn upstream_track_missing_contract_field_is_malformed() {
        assert_eq!(
            track_json(&json!({"urn": "x"})),
            Err(ToolError::TargetUnavailable)
        );
        assert_eq!(track_json(&json!("x")), Err(ToolError::TargetUnavailable));
        for k in ["title", "duration", "permalink_url"] {
            let mut t = track(1, json!({}));
            t.as_object_mut().unwrap().remove(k);
            assert_eq!(track_json(&t), Err(ToolError::TargetUnavailable), "{k}");
        }
        let mut t = track(1, json!({}));
        t["user"] = json!({});
        assert_eq!(track_json(&t), Err(ToolError::TargetUnavailable));
    }

    #[test]
    fn ids_and_permalinks_are_validated() {
        assert_eq!(parse_track_id("42").unwrap(), "soundcloud:tracks:42");
        assert_eq!(
            parse_track_id("soundcloud:tracks:42").unwrap(),
            "soundcloud:tracks:42"
        );
        assert!(parse_track_id("soundcloud:playlists:42").is_err());
        assert!(parse_track_id("4/../2").is_err());
        assert!(parse_track_id("").is_err());
        assert_eq!(
            normalize_permalink("https://www.soundcloud.com/a/b/").unwrap(),
            "https://soundcloud.com/a/b"
        );
        for bad in [
            "http://soundcloud.com/a",
            "https://evil.com/a",
            "https://soundcloud.com.evil.com/a",
            "https://user@soundcloud.com/a",
            "https://soundcloud.com:8443/a",
            "https://soundcloud.com/a?x=1",
            "https://soundcloud.com/a#f",
            "https://soundcloud.com/a/../b",
            "https://soundcloud.com/",
            "https://soundcloud.com/a/b/c/d",
        ] {
            assert!(normalize_permalink(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn search_builds_encoded_url_caches_and_skips_malformed() {
        let mut broken = track(3, json!({}));
        broken.as_object_mut().unwrap().remove("permalink_url");
        let up = FakeUpstream::with(vec![ok(
            json!({"collection": [track(1, json!({})), track(2, json!({})), broken]}),
        )]);
        let store = Arc::new(FakeStore::default());
        let sc = service(up.clone(), store.clone());
        let a = json!({"query": "lo fi & beats", "limit": 3});
        let out = call(&sc, TOOL_SEARCH, a.clone()).unwrap();
        assert_eq!(out["tracks"].as_array().unwrap().len(), 2);
        assert_eq!(out["skipped"], 1);
        assert_eq!(out["cached"], false);
        assert_eq!(out["capability"]["playback"], "embed");
        assert!(out.get("provider").is_none() && out.get("next_offset").is_none());
        assert_eq!(
            up.calls(),
            vec![format!("{API_BASE}/tracks?q=lo%20fi%20%26%20beats&limit=3")]
        );
        // Second identical call is a cache hit: no upstream call, no rate budget.
        let hit = call(&sc, TOOL_SEARCH, a).unwrap();
        assert_eq!(hit["cached"], true);
        assert_eq!(hit["tracks"], out["tracks"]);
        assert_eq!(up.calls().len(), 1);
        let m = store.map.lock().unwrap();
        let cache = m
            .iter()
            .find(|(k, _)| k.starts_with("cache.search."))
            .unwrap();
        assert_eq!(cache.1.1, TTL_SEARCH);
        assert!(m.keys().all(|k| !k.contains(':')), "kv keys contain no ':'");
        assert_eq!(m.iter().filter(|(k, _)| k.starts_with("rl.")).count(), 1);
    }

    #[test]
    fn search_rejects_bad_args() {
        let sc = service(FakeUpstream::with(vec![]), Arc::new(FakeStore::default()));
        for a in [
            json!({}),
            json!({"q": "legacy name"}),
            json!({"query": ""}),
            json!({"query": "  "}),
            json!({"query": "a\nb"}),
            json!({"query": "a", "limit": 0}),
            json!({"query": "a", "limit": 26}),
            json!({"query": "a", "offset": 1}),
            json!({"query": "a", "access": ["playable"]}),
            json!({"query": "a", "community_id": 3}),
            json!("str"),
        ] {
            assert!(
                matches!(
                    call(&sc, TOOL_SEARCH, a.clone()),
                    Err(ToolError::InvalidArgs(_))
                ),
                "{a}"
            );
        }
        assert!(matches!(
            call(&sc, "music.soundcloud.nope", json!({})),
            Err(ToolError::InvalidArgs(_))
        ));
    }

    #[test]
    fn track_returns_track_and_capability_and_maps_statuses() {
        let up = FakeUpstream::with(vec![
            ok(track(7, json!({}))),
            status(404, None, None),
            status(401, None, None),
            status(500, None, None),
            Err(HttpFail::Timeout),
            Err(HttpFail::Denied),
        ]);
        let sc = service(up.clone(), Arc::new(FakeStore::default()));
        let out = call(&sc, TOOL_TRACK, json!({"id": "7"})).unwrap();
        assert_eq!(out["track"]["id"], "soundcloud:tracks:7");
        assert_eq!(out["capability"]["playback"], "embed");
        assert_eq!(out["cached"], false);
        assert_eq!(
            up.calls()[0],
            format!("{API_BASE}/tracks/soundcloud:tracks:7")
        );
        assert_eq!(
            call(&sc, TOOL_TRACK, json!({"id": "8"})),
            Err(invalid("not_found"))
        );
        assert_eq!(
            call(&sc, TOOL_TRACK, json!({"id": "9"})),
            Err(ToolError::Forbidden)
        );
        assert_eq!(
            call(&sc, TOOL_TRACK, json!({"id": "10"})),
            Err(ToolError::TargetUnavailable)
        );
        assert_eq!(
            call(&sc, TOOL_TRACK, json!({"id": "11"})),
            Err(ToolError::Timeout)
        );
        assert_eq!(
            call(&sc, TOOL_TRACK, json!({"id": "12"})),
            Err(ToolError::Forbidden)
        );
    }

    #[test]
    fn upstream_429_opens_backoff_and_later_calls_skip_upstream() {
        let up = FakeUpstream::with(vec![status(429, None, Some(45))]);
        let store = Arc::new(FakeStore::default());
        let sc = service(up.clone(), store.clone());
        assert_eq!(
            call(&sc, TOOL_TRACK, json!({"id": "1"})),
            Err(ToolError::PoolExhausted)
        );
        assert_eq!(store.map.lock().unwrap()["backoff"].1, 45);
        assert_eq!(
            call(&sc, TOOL_TRACK, json!({"id": "2"})),
            Err(ToolError::PoolExhausted)
        );
        assert_eq!(up.calls().len(), 1, "second call must not reach SoundCloud");
    }

    #[test]
    fn backoff_ttl_is_clamped_and_host_ratelimit_counts() {
        let up = FakeUpstream::with(vec![
            status(429, None, Some(99_999)),
            Err(HttpFail::RateLimited(0)),
        ]);
        let store = Arc::new(FakeStore::default());
        let sc = service(up, store.clone());
        let _ = call(&sc, TOOL_TRACK, json!({"id": "1"}));
        assert_eq!(store.map.lock().unwrap()["backoff"].1, BACKOFF_MAX_S);
        store.map.lock().unwrap().remove("backoff");
        assert_eq!(
            call(&sc, TOOL_TRACK, json!({"id": "2"})),
            Err(ToolError::PoolExhausted)
        );
        assert_eq!(store.map.lock().unwrap()["backoff"].1, 1);
    }

    #[test]
    fn per_community_rate_limit_trips_after_budget() {
        let answers = (0..RATE_LIMIT_PER_MIN)
            .map(|i| ok(track(i as u64, json!({}))))
            .collect();
        let up = FakeUpstream::with(answers);
        let sc = service(up.clone(), Arc::new(FakeStore::default()));
        for i in 0..RATE_LIMIT_PER_MIN {
            call(&sc, TOOL_TRACK, json!({"id": i.to_string()})).unwrap();
        }
        assert_eq!(
            call(&sc, TOOL_TRACK, json!({"id": "9999"})),
            Err(ToolError::PoolExhausted)
        );
        assert_eq!(up.calls().len() as i64, RATE_LIMIT_PER_MIN);
    }

    #[test]
    fn limiter_fails_closed_when_kv_is_down() {
        let store = Arc::new(FakeStore::default());
        *store.fail.lock().unwrap() = true;
        let up = FakeUpstream::with(vec![ok(track(1, json!({})))]);
        let sc = service(up.clone(), store);
        assert_eq!(
            call(&sc, TOOL_TRACK, json!({"id": "1"})),
            Err(ToolError::TargetUnavailable)
        );
        assert!(up.calls().is_empty());
    }

    #[test]
    fn playlist_pages_with_opaque_token() {
        let tracks = json!({"collection": [
            track(1, json!({})), track(2, json!({})), track(3, json!({}))]});
        let up = FakeUpstream::with(vec![ok(tracks.clone()), ok(tracks)]);
        let sc = service(up.clone(), Arc::new(FakeStore::default()));
        let p1 = call(&sc, TOOL_PLAYLIST, json!({"playlist_id": "5", "limit": 2})).unwrap();
        assert_eq!(p1["tracks"].as_array().unwrap().len(), 2);
        assert_eq!(p1["next_page_token"], "o2");
        assert_eq!(p1["capability"]["playback"], "embed");
        assert!(p1.get("truncated").is_none() && p1.get("license").is_none());
        assert_eq!(
            up.calls()[0],
            format!("{API_BASE}/playlists/soundcloud:playlists:5/tracks")
        );
        let p2 = call(
            &sc,
            TOOL_PLAYLIST,
            json!({"playlist_id": "5", "limit": 2, "page_token": "o2"}),
        )
        .unwrap();
        assert_eq!(p2["tracks"].as_array().unwrap().len(), 1);
        assert_eq!(p2["tracks"][0]["id"], "soundcloud:tracks:3");
        assert_eq!(p2["next_page_token"], Value::Null);
    }

    #[test]
    fn playlist_rejects_bad_args() {
        let sc = service(FakeUpstream::with(vec![]), Arc::new(FakeStore::default()));
        for a in [
            json!({"id": "5"}),
            json!({"playlist_id": "x"}),
            json!({"playlist_id": "5", "limit": 51}),
            json!({"playlist_id": "5", "page_token": "2"}),
            json!({"playlist_id": "5", "page_token": "o"}),
            json!({"playlist_id": "5", "page_token": 2}),
            json!({"playlist_id": "5", "page_token": "o1234567"}),
        ] {
            assert!(
                matches!(
                    call(&sc, TOOL_PLAYLIST, a.clone()),
                    Err(ToolError::InvalidArgs(_))
                ),
                "{a}"
            );
        }
    }

    #[test]
    fn resolve_follows_302_to_track_via_api_host_only() {
        let up = FakeUpstream::with(vec![
            status(
                302,
                Some("https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A77"),
                None,
            ),
            ok(track(77, json!({}))),
        ]);
        let sc = service(up.clone(), Arc::new(FakeStore::default()));
        let out = call(
            &sc,
            TOOL_RESOLVE,
            json!({"url": "https://soundcloud.com/a/song"}),
        )
        .unwrap();
        assert_eq!(out["track"]["id"], "soundcloud:tracks:77");
        assert!(out.get("kind").is_none());
        assert_eq!(out["capability"]["playback"], "embed");
        assert_eq!(
            up.calls()[0],
            format!("{API_BASE}/resolve?url=https%3A%2F%2Fsoundcloud.com%2Fa%2Fsong")
        );
        assert_eq!(
            up.calls()[1],
            format!("{API_BASE}/tracks/soundcloud:tracks:77")
        );
        // Cached resolve answers without upstream.
        let again = call(
            &sc,
            TOOL_RESOLVE,
            json!({"url": "https://soundcloud.com/a/song"}),
        )
        .unwrap();
        assert_eq!(again["cached"], true);
        assert_eq!(up.calls().len(), 2);
    }

    #[test]
    fn resolve_rejects_foreign_playlist_user_redirects_and_handles_followed_json() {
        let up = FakeUpstream::with(vec![
            status(302, Some("https://evil.example/tracks/1"), None),
            status(302, Some("https://api.soundcloud.com/users/1"), None),
            status(302, None, None),
            ok(json!({"kind": "user", "username": "x"})),
            ok({
                let mut t = track(3, json!({}));
                t["kind"] = json!("track");
                t
            }),
            status(302, Some("https://api.soundcloud.com/playlists/9"), None),
        ]);
        let sc = service(up, Arc::new(FakeStore::default()));
        let u = |p: &str| json!({"url": format!("https://soundcloud.com/{p}")});
        assert_eq!(
            call(&sc, TOOL_RESOLVE, u("a")),
            Err(ToolError::TargetUnavailable)
        );
        assert_eq!(
            call(&sc, TOOL_RESOLVE, u("b")),
            Err(invalid("unsupported_resource_kind"))
        );
        assert_eq!(
            call(&sc, TOOL_RESOLVE, u("c")),
            Err(ToolError::TargetUnavailable)
        );
        assert_eq!(
            call(&sc, TOOL_RESOLVE, u("d")),
            Err(invalid("unsupported_resource_kind"))
        );
        assert_eq!(
            call(&sc, TOOL_RESOLVE, u("e")).unwrap()["track"]["id"],
            "soundcloud:tracks:3"
        );
        assert_eq!(
            call(&sc, TOOL_RESOLVE, u("f/sets/x")),
            Err(invalid("unsupported_resource_kind"))
        );
    }

    #[test]
    fn credential_is_a_secret_ref_not_a_value() {
        assert_eq!(AUTH_REF, ("Authorization", "soundcloud.access_token"));
        assert_eq!(
            tool_log_label("music.soundcloud.search"),
            "music.soundcloud.search"
        );
        assert_eq!(tool_log_label("attacker text"), "unknown");
    }
}
