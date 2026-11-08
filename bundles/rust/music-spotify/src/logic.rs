//! Host-independent Spotify tool logic.
//!
//! Every tool is a function over three seams -- [`Http`] (the host `http`
//! capability), [`Store`] (the bundle's own community-scoped kv) and [`Clock`]
//! -- so the whole tool table is unit-tested on the host target with fakes;
//! `lib.rs` supplies the real wasm implementations.
//!
//! Credentials: this module never sees a token. Requests carry only a
//! secret-ref NAME (`spotify.app` for catalog metadata, `spotify.oauth` for the
//! community's connected account); the host resolves it to the full
//! `Authorization` header value, refreshes it, and scrubs it from responses.
//!
//! Safety shape: args are validated first (unknown fields rejected, ids/URIs
//! shape-checked, URLs host-pinned), upstream calls are rate-limited per
//! community and honor `Retry-After`, and every failure is a typed, stable
//! error code -- never a default answer.

use std::sync::Arc;

use serde::Deserialize;
use serde_json::{Value, json};
use waddle_sdk::tool::{ToolContext, ToolError, ToolRegistry};

/// Spotify Web API base.
pub const API_BASE: &str = "https://api.spotify.com/v1";
/// Secret-ref resolved host-side to the app (client-credentials) bearer.
pub const REF_APP: &str = "spotify.app";
/// Secret-ref resolved host-side to the community's connected-account bearer.
pub const REF_USER: &str = "spotify.oauth";

/// Default and maximum search page (legacy parity: 10).
pub const DEFAULT_SEARCH_LIMIT: u32 = 10;
/// Largest search page Spotify allows.
pub const MAX_SEARCH_LIMIT: u32 = 25;
/// Default playlist page.
pub const DEFAULT_PLAYLIST_LIMIT: u32 = 25;
/// Largest playlist page Spotify allows.
pub const MAX_PLAYLIST_LIMIT: u32 = 50;
/// Longest search query accepted.
pub const MAX_QUERY_LEN: usize = 200;
/// Longest Spotify reference (URL/URI) accepted.
pub const MAX_REF_LEN: usize = 512;
/// Most URIs one `player.play` may carry (Spotify's cap).
pub const MAX_PLAY_URIS: usize = 100;
/// Default upstream calls per community per minute.
pub const DEFAULT_RATE_PER_MIN: u32 = 60;
/// Longest honored `Retry-After` (seconds).
pub const MAX_RETRY_AFTER_S: u32 = 3600;
/// `Retry-After` assumed when a 429 omits the header.
pub const DEFAULT_RETRY_AFTER_S: u32 = 5;
/// Seconds an idempotency record lives.
pub const IDEM_TTL_S: u32 = 600;

/// Scope the host-verified actor must hold for player state/play/pause/next
/// (the connected account is the community's; control is moderator-level).
pub const SCOPE_PLAYER: &str = "community:moderate";
/// Scope for `player.queue_add`: a verified community member. Viewer requests
/// are mediated and policy-checked by the DJ app (which alone holds the tool
/// grant); this only guarantees a real, verified member actor.
pub const SCOPE_QUEUE: &str = "community:member";

/// kv key holding the community's optional config JSON.
pub const KEY_CONFIG: &str = "config";
/// kv key holding the 429 backoff deadline (epoch ms).
pub const KEY_BACKOFF: &str = "backoff.until";

/// Every tool this bundle exports (must match both manifests).
pub const TOOL_NAMES: [&str; 9] = [
    "music.spotify.search",
    "music.spotify.track",
    "music.spotify.playlist.get",
    "music.spotify.resolve",
    "music.spotify.player.state",
    "music.spotify.player.play",
    "music.spotify.player.pause",
    "music.spotify.player.next",
    "music.spotify.player.queue_add",
];

/// Which credential an upstream call rides on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Cred {
    /// App-level client-credentials bearer (public catalog data).
    App,
    /// The community's connected Spotify account (playback, private data).
    User,
}

impl Cred {
    /// The secret-ref name the host resolves for this credential.
    pub fn secret_ref(self) -> &'static str {
        match self {
            Cred::App => REF_APP,
            Cred::User => REF_USER,
        }
    }

    fn tag(self) -> &'static str {
        match self {
            Cred::App => "app",
            Cred::User => "user",
        }
    }
}

/// One outbound request (host maps it onto the WIT `http.request`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpRequest {
    pub method: &'static str,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Option<Vec<u8>>,
    pub cred: Cred,
}

/// One upstream response.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
    pub truncated: bool,
}

/// Every way the host `http` capability can fail (mirrors the WIT error).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HttpError {
    Denied(String),
    Timeout,
    TooLarge(u64),
    RateLimited(u32),
    Transport(String),
}

/// The host `http` capability.
pub trait Http: Send + Sync {
    /// Sends one request; credentials are injected host-side from `cred`.
    fn send(&self, req: &HttpRequest) -> Result<HttpResponse, HttpError>;
}

/// The bundle's own community-scoped kv.
pub trait Store: Send + Sync {
    /// Raw bytes at `key`, `None` if unset.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    /// Stores `value` for `ttl_s` seconds (0 = no expiry).
    fn set(&self, key: &str, value: &[u8], ttl_s: u32) -> Result<(), String>;
    /// Atomically adds `delta`, (re)setting the TTL; returns the new value.
    fn increment(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, String>;
}

/// Wall clock.
pub trait Clock: Send + Sync {
    /// Milliseconds since the Unix epoch.
    fn now_millis(&self) -> u64;
}

fn invalid(code: impl Into<String>) -> ToolError {
    ToolError::InvalidArgs(code.into())
}

/// Stable machine code carried by a tool error (for PII-free logging).
pub fn outcome_code(e: &ToolError) -> String {
    match e {
        ToolError::InvalidArgs(m) => m.split([':', ' ']).next().unwrap_or("invalid").to_string(),
        ToolError::NotGranted => "not_granted".into(),
        ToolError::Forbidden => "forbidden".into(),
        ToolError::Timeout => "timeout".into(),
        ToolError::TargetUnavailable => "unavailable".into(),
        _ => "error".into(),
    }
}

/// Known tool name for logging, else `"unknown"` (never a caller-supplied string).
pub fn tool_log_label(name: &str) -> &'static str {
    TOOL_NAMES
        .iter()
        .find(|t| **t == name)
        .copied()
        .unwrap_or("unknown")
}

// ---- helpers ------------------------------------------------------------

/// Percent-encodes everything but RFC 3986 unreserved characters.
pub fn enc(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// FNV-1a 64 as 16 hex chars: a kv-key-safe digest (always verified against
/// the stored material, so a collision can only miss, never misanswer).
pub fn fnv_hex(s: &str) -> String {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in s.bytes() {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    format!("{h:016x}")
}

/// A Spotify id: 22 base62 characters.
pub fn valid_id(s: &str) -> bool {
    s.len() == 22 && s.bytes().all(|b| b.is_ascii_alphanumeric())
}

/// What a Spotify reference points at.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Track,
    Playlist,
}

/// Parses a Spotify URI (`spotify:track:<id>`) or an `https://open.spotify.com`
/// link into `(kind, id)`. The host is pinned exactly; short links
/// (`spotify.link`) are refused so the bundle never follows a redirect.
pub fn parse_ref(input: &str) -> Result<(Kind, String), ToolError> {
    let s = input.trim();
    if s.is_empty() || s.len() > MAX_REF_LEN || s.chars().any(char::is_control) {
        return Err(invalid("invalid_spotify_reference"));
    }
    let (kind_s, id): (String, String) = if let Some(rest) = s.strip_prefix("spotify:") {
        let parts: Vec<&str> = rest.split(':').collect();
        if parts.len() != 2 {
            return Err(invalid("invalid_spotify_reference"));
        }
        (parts[0].to_string(), parts[1].to_string())
    } else if let Some(rest) = s.strip_prefix("https://open.spotify.com/") {
        let path = rest.split(['?', '#']).next().unwrap_or("");
        let mut segs: Vec<&str> = path.split('/').filter(|p| !p.is_empty()).collect();
        if segs.first().is_some_and(|f| f.starts_with("intl-")) {
            segs.remove(0);
        }
        if segs.len() != 2 {
            return Err(invalid("invalid_spotify_reference"));
        }
        (segs[0].to_string(), segs[1].to_string())
    } else {
        return Err(invalid("invalid_spotify_reference"));
    };
    let kind = match kind_s.as_str() {
        "track" => Kind::Track,
        "playlist" => Kind::Playlist,
        "album" | "artist" | "episode" | "show" | "user" => {
            return Err(invalid("unsupported_kind"));
        }
        _ => return Err(invalid("invalid_spotify_reference")),
    };
    if !valid_id(&id) {
        return Err(invalid("invalid_spotify_reference"));
    }
    Ok((kind, id))
}

fn track_ref(raw: &str) -> Result<String, ToolError> {
    match parse_ref(raw)? {
        (Kind::Track, id) => Ok(id),
        _ => Err(invalid("expected_track_reference")),
    }
}

fn playlist_ref(raw: &str) -> Result<String, ToolError> {
    match parse_ref(raw)? {
        (Kind::Playlist, id) => Ok(id),
        _ => Err(invalid("expected_playlist_reference")),
    }
}

/// A contract `id` argument: <= 64 chars, a bare 22-char Spotify id or a
/// `spotify:<kind>:<id>` URI of the expected kind.
fn id_arg(raw: &str, want: Kind) -> Result<String, ToolError> {
    if raw.is_empty() || raw.len() > 64 {
        return Err(invalid("invalid_id"));
    }
    if valid_id(raw) {
        return Ok(raw.to_string());
    }
    match parse_ref(raw)? {
        (k, id) if k == want => Ok(id),
        _ => Err(invalid(match want {
            Kind::Track => "expected_track_reference",
            Kind::Playlist => "expected_playlist_reference",
        })),
    }
}

fn device_ok(d: &str) -> bool {
    !d.is_empty()
        && d.len() <= 80
        && d.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
}

fn parse_args<T: for<'de> Deserialize<'de>>(args: Value) -> Result<T, ToolError> {
    serde_json::from_value(args).map_err(|e| invalid(format!("invalid_args: {e}")))
}

/// Second authz layer (the host also enforces the manifest `required_scope`):
/// the verified actor must hold `scope`, else `forbidden` BEFORE any host call.
fn require_scope(ctx: &ToolContext, scope: &str) -> Result<(), ToolError> {
    if ctx.has_scope(scope) {
        Ok(())
    } else {
        Err(ToolError::Forbidden)
    }
}

fn require_key(ctx: &ToolContext) -> Result<&str, ToolError> {
    match ctx.idempotency_key.as_deref() {
        Some(k) if !k.is_empty() => Ok(k),
        _ => Err(invalid("idempotency_key_required")),
    }
}

/// Capability flags every output carries (shared music contract: `playback`,
/// `server_audio`, `queue_add`) so a caller degrades loudly. Spotify plays via
/// Connect on the streamer's Premium device and never streams audio itself.
pub fn capability() -> Value {
    json!({
        "playback": "connect",
        "server_audio": false,
        "queue_add": true,
        "requires_premium": true,
    })
}

// ---- config -------------------------------------------------------------

/// Per-community optional config (kv `config`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    pub market: Option<String>,
    pub rate_per_min: u32,
    pub cache_ttl_s: u32,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawConfig {
    market: Option<String>,
    rate_limit_per_min: Option<u32>,
    cache_ttl_s: Option<u32>,
}

// ---- service ------------------------------------------------------------

/// The tool implementation over its three seams.
pub struct Spotify {
    http: Arc<dyn Http>,
    kv: Arc<dyn Store>,
    clock: Arc<dyn Clock>,
}

fn kv_err(_: String) -> ToolError {
    ToolError::TargetUnavailable
}

fn body_reason(body: &[u8]) -> Option<String> {
    let v: Value = serde_json::from_slice(body).ok()?;
    v.get("error")?.get("reason")?.as_str().map(str::to_string)
}

fn header<'a>(h: &'a [(String, String)], name: &str) -> Option<&'a str> {
    h.iter()
        .find(|(k, _)| k.eq_ignore_ascii_case(name))
        .map(|(_, v)| v.as_str())
}

fn best_image(images: &Value) -> Option<String> {
    images
        .as_array()?
        .iter()
        .filter(|i| i.get("url").and_then(Value::as_str).is_some())
        .max_by_key(|i| i.get("width").and_then(Value::as_u64).unwrap_or(0))
        .and_then(|i| i.get("url")?.as_str().map(str::to_string))
}

/// Maps one Spotify track object to the normalized Track; `None` for local
/// files, episodes and removed items (no playable Spotify id).
pub fn track_json(item: &Value) -> Option<Value> {
    if item
        .get("type")
        .and_then(Value::as_str)
        .is_some_and(|t| t != "track")
    {
        return None;
    }
    let id = item.get("id")?.as_str()?;
    if !valid_id(id) {
        return None;
    }
    let artists: Vec<String> = item
        .get("artists")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|x| x.get("name")?.as_str().map(str::to_string))
        .collect();
    let album = item.get("album");
    let url = item
        .get("external_urls")
        .and_then(|e| e.get("spotify"))
        .and_then(Value::as_str)
        .map_or_else(
            || format!("https://open.spotify.com/track/{id}"),
            str::to_string,
        );
    Some(json!({
        "provider": "spotify",
        "id": id,
        "title": item.get("name").and_then(Value::as_str).unwrap_or(""),
        "artist": artists.first().map_or("", String::as_str),
        "artists": artists,
        "album": album.and_then(|a| a.get("name")).and_then(Value::as_str).unwrap_or(""),
        "duration_ms": item.get("duration_ms").and_then(Value::as_u64).unwrap_or(0),
        "thumbnail": album.and_then(|a| a.get("images")).and_then(best_image),
        "source_url": url,
        "uri": format!("spotify:track:{id}"),
        "explicit": item.get("explicit").and_then(Value::as_bool),
    }))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SearchArgs {
    query: String,
    limit: Option<u32>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TrackArgs {
    id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PlaylistArgs {
    playlist_id: String,
    limit: Option<u32>,
    page_token: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResolveArgs {
    url: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StateArgs {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PlayArgs {
    uris: Option<Vec<String>>,
    context_uri: Option<String>,
    offset: Option<u32>,
    position_ms: Option<u64>,
    device_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeviceArgs {
    device_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct QueueArgs {
    track: String,
    device_id: Option<String>,
}

impl Spotify {
    /// Wires the tool logic to its seams.
    pub fn new(http: Arc<dyn Http>, kv: Arc<dyn Store>, clock: Arc<dyn Clock>) -> Self {
        Self { http, kv, clock }
    }

    /// Loads the optional community config: absent -> defaults, invalid -> LOUD.
    pub fn config(&self) -> Result<Config, ToolError> {
        let d = Config {
            market: None,
            rate_per_min: DEFAULT_RATE_PER_MIN,
            cache_ttl_s: 300,
        };
        let Some(bytes) = self.kv.get(KEY_CONFIG).map_err(kv_err)? else {
            return Ok(d);
        };
        let raw: RawConfig =
            serde_json::from_slice(&bytes).map_err(|_| invalid("invalid_config"))?;
        let market_ok = raw
            .market
            .as_ref()
            .is_none_or(|m| m.len() == 2 && m.bytes().all(|b| b.is_ascii_uppercase()));
        let rate = raw.rate_limit_per_min.unwrap_or(d.rate_per_min);
        let ttl = raw.cache_ttl_s.unwrap_or(d.cache_ttl_s);
        if !market_ok || !(1..=600).contains(&rate) || !(1..=86_400).contains(&ttl) {
            return Err(invalid("invalid_config"));
        }
        Ok(Config {
            market: raw.market,
            rate_per_min: rate,
            cache_ttl_s: ttl,
        })
    }

    /// One upstream call: honors a stored 429 backoff, enforces the per-community
    /// fixed-window rate limit, then sends. A 429 stores the backoff and fails
    /// loud with the retry delay; other statuses are returned for mapping.
    fn upstream(&self, cfg: &Config, req: HttpRequest) -> Result<HttpResponse, ToolError> {
        let now = self.clock.now_millis();
        if let Some(raw) = self.kv.get(KEY_BACKOFF).map_err(kv_err)? {
            let until = std::str::from_utf8(&raw)
                .ok()
                .and_then(|s| s.parse::<u64>().ok())
                .ok_or(ToolError::TargetUnavailable)?;
            if until > now {
                return Err(rate_limited((until - now).div_ceil(1000)));
            }
        }
        let window = now / 60_000;
        let count = self
            .kv
            .increment(&format!("rl.{window}"), 1, 120)
            .map_err(kv_err)?;
        if count > i64::from(cfg.rate_per_min) {
            return Err(rate_limited(((window + 1) * 60_000 - now).div_ceil(1000)));
        }
        let resp = self
            .http
            .send(&req)
            .map_err(|e| map_http_err(e, req.cred))?;
        if resp.status == 429 {
            let secs = header(&resp.headers, "retry-after")
                .and_then(|v| v.trim().parse::<u32>().ok())
                .unwrap_or(DEFAULT_RETRY_AFTER_S)
                .clamp(1, MAX_RETRY_AFTER_S);
            let until = now + u64::from(secs) * 1000;
            self.kv
                .set(KEY_BACKOFF, until.to_string().as_bytes(), secs)
                .map_err(kv_err)?;
            return Err(rate_limited(u64::from(secs)));
        }
        Ok(resp)
    }

    fn get(&self, cfg: &Config, cred: Cred, path_query: &str) -> Result<HttpResponse, ToolError> {
        self.upstream(
            cfg,
            HttpRequest {
                method: "GET",
                url: format!("{API_BASE}{path_query}"),
                headers: vec![("Accept".into(), "application/json".into())],
                body: None,
                cred,
            },
        )
    }

    fn json_ok(resp: &HttpResponse, cred: Cred) -> Result<Value, ToolError> {
        check_status(resp, cred)?;
        if resp.truncated {
            return Err(invalid("upstream_too_large"));
        }
        serde_json::from_slice(&resp.body).map_err(|_| invalid("upstream_malformed"))
    }

    /// Cache read-through keyed by `key`, verified against `material`;
    /// returns `(value, was_cache_hit)`.
    fn cached(
        &self,
        cfg: &Config,
        key: &str,
        material: &str,
        fetch: impl FnOnce() -> Result<Value, ToolError>,
    ) -> Result<(Value, bool), ToolError> {
        let now = self.clock.now_millis();
        if let Some(raw) = self.kv.get(key).map_err(kv_err)?
            && let Ok(entry) = serde_json::from_slice::<Value>(&raw)
        {
            let fresh = entry
                .get("exp")
                .and_then(Value::as_u64)
                .is_some_and(|e| e > now);
            if fresh
                && entry.get("m").and_then(Value::as_str) == Some(material)
                && let Some(v) = entry.get("v")
            {
                return Ok((v.clone(), true));
            }
        }
        let v = fetch()?;
        let entry = json!({
            "m": material,
            "exp": now + u64::from(cfg.cache_ttl_s) * 1000,
            "v": v,
        });
        let bytes = serde_json::to_vec(&entry).map_err(|_| invalid("internal"))?;
        self.kv.set(key, &bytes, cfg.cache_ttl_s).map_err(kv_err)?;
        Ok((v, false))
    }

    fn market_q(cfg: &Config, first: bool) -> String {
        cfg.market.as_ref().map_or_else(String::new, |m| {
            format!("{}market={m}", if first { "?" } else { "&" })
        })
    }

    fn fetch_track(&self, cfg: &Config, cred: Cred, id: &str) -> Result<(Value, bool), ToolError> {
        let key = format!("trk.{}.{id}", cred.tag());
        self.cached(cfg, &key, &key, || {
            let resp = self.get(
                cfg,
                cred,
                &format!("/tracks/{id}{}", Self::market_q(cfg, true)),
            )?;
            let v = Self::json_ok(&resp, cred)?;
            track_json(&v).ok_or_else(|| invalid("not_found"))
        })
    }

    fn fetch_playlist(
        &self,
        cfg: &Config,
        cred: Cred,
        id: &str,
        limit: u32,
        offset: u32,
    ) -> Result<(Value, bool), ToolError> {
        let key = format!("pl.{}.{id}.{offset}.{limit}", cred.tag());
        self.cached(cfg, &key, &key, || {
            let head = self.get(
                cfg,
                cred,
                &format!(
                    "/playlists/{id}?fields={}{}",
                    enc("id,name,owner(display_name),tracks(total)"),
                    Self::market_q(cfg, false)
                ),
            )?;
            let head = Self::json_ok(&head, cred)?;
            let page = self.get(
                cfg,
                cred,
                &format!(
                    "/playlists/{id}/tracks?limit={limit}&offset={offset}&additional_types=track{}",
                    Self::market_q(cfg, false)
                ),
            )?;
            let page = Self::json_ok(&page, cred)?;
            let rows = page
                .get("items")
                .and_then(Value::as_array)
                .ok_or_else(|| invalid("upstream_malformed"))?;
            let mut tracks = Vec::new();
            let mut skipped = 0u32;
            for row in rows {
                // Spotify nests the entity under `track` (legacy) or `item`.
                let inner = row.get("track").or_else(|| row.get("item"));
                match inner.and_then(track_json) {
                    Some(t) => tracks.push(t),
                    None => skipped += 1,
                }
            }
            let total = head
                .get("tracks")
                .and_then(|t| t.get("total"))
                .and_then(Value::as_u64)
                .or_else(|| page.get("total").and_then(Value::as_u64))
                .unwrap_or(0);
            // Opaque page token = the next numeric offset; absent on the last page.
            let next_off = u64::from(offset) + rows.len() as u64;
            let next_page_token = if !rows.is_empty() && next_off < total {
                json!(next_off.to_string())
            } else {
                Value::Null
            };
            Ok(json!({
                "playlist": {
                    "id": id,
                    "name": head.get("name").and_then(Value::as_str).unwrap_or(""),
                    "owner": head.get("owner").and_then(|o| o.get("display_name")).and_then(Value::as_str),
                    "total": total,
                    "source_url": format!("https://open.spotify.com/playlist/{id}"),
                },
                "tracks": tracks,
                "skipped": skipped,
                "next_page_token": next_page_token,
            }))
        })
    }

    /// Adds `cached` + `capability` (every output carries both).
    fn finish(mut v: Value, cached: bool) -> Value {
        v["cached"] = json!(cached);
        v["capability"] = capability();
        v
    }

    /// Adds `capability` only (player tools: nothing cacheable).
    fn with_cap(mut v: Value) -> Value {
        v["capability"] = capability();
        v
    }

    // ---- metadata tools -------------------------------------------------

    /// `music.spotify.search {query, limit?<=25}` -> `{tracks, cached, skipped, capability}`.
    pub fn search(&self, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        let a: SearchArgs = parse_args(args)?;
        let query = a.query.trim();
        if query.is_empty()
            || query.chars().count() > MAX_QUERY_LEN
            || query.chars().any(char::is_control)
        {
            return Err(invalid("invalid_query"));
        }
        let limit = a.limit.unwrap_or(DEFAULT_SEARCH_LIMIT);
        if !(1..=MAX_SEARCH_LIMIT).contains(&limit) {
            return Err(invalid("invalid_limit"));
        }
        let cfg = self.config()?;
        let cred = Cred::App;
        let material = format!("{}|{limit}|{query}", cred.tag());
        let key = format!("srch.{}", fnv_hex(&material));
        let (v, hit) = self.cached(&cfg, &key, &material, || {
            let resp = self.get(
                &cfg,
                cred,
                &format!(
                    "/search?q={}&type=track&limit={limit}{}",
                    enc(query),
                    Self::market_q(&cfg, false)
                ),
            )?;
            let v = Self::json_ok(&resp, cred)?;
            let items = v
                .get("tracks")
                .and_then(|t| t.get("items"))
                .and_then(Value::as_array)
                .ok_or_else(|| invalid("upstream_malformed"))?;
            let tracks: Vec<Value> = items.iter().filter_map(track_json).collect();
            Ok(json!({ "skipped": items.len() - tracks.len(), "tracks": tracks }))
        })?;
        Ok(Self::finish(v, hit))
    }

    /// `music.spotify.track {id}` -> `{track, cached, capability}`. `id` is a bare
    /// track id or a `spotify:track:` URI (<= 64 chars, per the shared contract).
    pub fn track(&self, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        let a: TrackArgs = parse_args(args)?;
        let id = id_arg(&a.id, Kind::Track)?;
        let cfg = self.config()?;
        let (t, hit) = self.fetch_track(&cfg, Cred::App, &id)?;
        Ok(Self::finish(json!({ "track": t }), hit))
    }

    /// `music.spotify.playlist.get {playlist_id, limit?<=50, page_token?}` ->
    /// `{tracks, next_page_token, cached, skipped, capability, playlist}`.
    pub fn playlist_get(&self, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        let a: PlaylistArgs = parse_args(args)?;
        let id = id_arg(&a.playlist_id, Kind::Playlist)?;
        let limit = a.limit.unwrap_or(DEFAULT_PLAYLIST_LIMIT);
        if !(1..=MAX_PLAYLIST_LIMIT).contains(&limit) {
            return Err(invalid("invalid_limit"));
        }
        let offset = match a.page_token.as_deref() {
            None => 0,
            Some(t) if t.len() <= 200 && !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit()) => {
                t.parse::<u32>()
                    .ok()
                    .filter(|o| *o <= 100_000)
                    .ok_or_else(|| invalid("invalid_page_token"))?
            }
            Some(_) => return Err(invalid("invalid_page_token")),
        };
        let cfg = self.config()?;
        let (v, hit) = self.fetch_playlist(&cfg, Cred::App, &id, limit, offset)?;
        Ok(Self::finish(v, hit))
    }

    /// `music.spotify.resolve {url}` -> `{track, cached, capability}`. Only track
    /// links resolve (shared contract); a playlist link is `expected_track_reference`
    /// - fetch it with `playlist.get`.
    pub fn resolve(&self, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        let a: ResolveArgs = parse_args(args)?;
        let id = track_ref(&a.url)?;
        let cfg = self.config()?;
        let (t, hit) = self.fetch_track(&cfg, Cred::App, &id)?;
        Ok(Self::finish(json!({ "track": t }), hit))
    }

    // ---- player tools (connected account) --------------------------------

    fn player(
        &self,
        method: &'static str,
        path: &str,
        query: &[(&str, String)],
        body: Option<Value>,
    ) -> Result<HttpResponse, ToolError> {
        let cfg = self.config()?;
        let q: Vec<String> = query
            .iter()
            .map(|(k, v)| format!("{k}={}", enc(v)))
            .collect();
        let url = if q.is_empty() {
            format!("{API_BASE}{path}")
        } else {
            format!("{API_BASE}{path}?{}", q.join("&"))
        };
        let mut headers = vec![("Accept".to_string(), "application/json".to_string())];
        let body = match body {
            Some(b) => {
                headers.push(("Content-Type".into(), "application/json".into()));
                Some(serde_json::to_vec(&b).map_err(|_| invalid("internal"))?)
            }
            None => None,
        };
        self.upstream(
            &cfg,
            HttpRequest {
                method,
                url,
                headers,
                body,
                cred: Cred::User,
            },
        )
    }

    fn device_query(device: &Option<String>) -> Result<Vec<(&'static str, String)>, ToolError> {
        match device {
            None => Ok(vec![]),
            Some(d) if device_ok(d) => Ok(vec![("device_id", d.clone())]),
            Some(_) => Err(invalid("invalid_device_id")),
        }
    }

    /// `music.spotify.player.state {}` -> playback state (`active:false` if no device).
    pub fn player_state(&self, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        require_scope(ctx, SCOPE_PLAYER)?;
        let _: StateArgs = parse_args(args)?;
        let resp = self.player(
            "GET",
            "/me/player",
            &[("additional_types", "track".into())],
            None,
        )?;
        if resp.status == 204 {
            return Ok(Self::with_cap(
                json!({ "active": false, "is_playing": false }),
            ));
        }
        let v = Self::json_ok(&resp, Cred::User)?;
        let dev = v.get("device").cloned().unwrap_or(Value::Null);
        Ok(Self::with_cap(json!({
            "active": true,
            "is_playing": v.get("is_playing").and_then(Value::as_bool).unwrap_or(false),
            "progress_ms": v.get("progress_ms").and_then(Value::as_u64),
            "shuffle": v.get("shuffle_state").and_then(Value::as_bool).unwrap_or(false),
            "repeat": v.get("repeat_state").and_then(Value::as_str).unwrap_or("off"),
            "currently_playing_type": v.get("currently_playing_type").and_then(Value::as_str),
            "device": {
                "id": dev.get("id").and_then(Value::as_str),
                "name": dev.get("name").and_then(Value::as_str),
                "type": dev.get("type").and_then(Value::as_str),
                "volume_percent": dev.get("volume_percent").and_then(Value::as_u64),
            },
            "track": v.get("item").and_then(track_json),
        })))
    }

    fn ok_action(resp: &HttpResponse, action: &str) -> Result<Value, ToolError> {
        check_status(resp, Cred::User)?;
        Ok(Self::with_cap(json!({ "ok": true, "action": action })))
    }

    /// `music.spotify.player.play {uris? | context_uri?, offset?, position_ms?, device_id?}`;
    /// no content args = resume.
    pub fn player_play(&self, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        require_scope(ctx, SCOPE_PLAYER)?;
        let a: PlayArgs = parse_args(args)?;
        require_key(ctx)?;
        if a.uris.is_some() && a.context_uri.is_some() {
            return Err(invalid("uris_and_context_exclusive"));
        }
        let mut body = serde_json::Map::new();
        if let Some(uris) = &a.uris {
            if uris.is_empty() || uris.len() > MAX_PLAY_URIS {
                return Err(invalid("invalid_uris"));
            }
            let norm: Result<Vec<String>, ToolError> = uris
                .iter()
                .map(|u| track_ref(u).map(|id| format!("spotify:track:{id}")))
                .collect();
            body.insert("uris".into(), json!(norm?));
        }
        if let Some(c) = &a.context_uri {
            let id = playlist_ref(c)?;
            body.insert(
                "context_uri".into(),
                json!(format!("spotify:playlist:{id}")),
            );
        }
        if let Some(o) = a.offset {
            if a.uris.is_none() && a.context_uri.is_none() {
                return Err(invalid("offset_requires_content"));
            }
            body.insert("offset".into(), json!({ "position": o }));
        }
        if let Some(p) = a.position_ms {
            if p > 86_400_000 {
                return Err(invalid("invalid_position_ms"));
            }
            body.insert("position_ms".into(), json!(p));
        }
        let q = Self::device_query(&a.device_id)?;
        let body = if body.is_empty() {
            None
        } else {
            Some(Value::Object(body))
        };
        let resp = self.player("PUT", "/me/player/play", &q, body)?;
        Self::ok_action(&resp, "play")
    }

    /// `music.spotify.player.pause {device_id?}`.
    pub fn player_pause(&self, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        require_scope(ctx, SCOPE_PLAYER)?;
        let a: DeviceArgs = parse_args(args)?;
        require_key(ctx)?;
        let q = Self::device_query(&a.device_id)?;
        let resp = self.player("PUT", "/me/player/pause", &q, None)?;
        Self::ok_action(&resp, "pause")
    }

    /// Runs `f` at most once per `(action, idempotency key)` within [`IDEM_TTL_S`]:
    /// a redelivered non-idempotent write returns the first outcome.
    fn once(
        &self,
        ctx: &ToolContext,
        action: &str,
        f: impl FnOnce() -> Result<Value, ToolError>,
    ) -> Result<Value, ToolError> {
        let key_raw = require_key(ctx)?;
        let kv_key = format!("idem.{action}.{}", fnv_hex(key_raw));
        if let Some(raw) = self.kv.get(&kv_key).map_err(kv_err)?
            && let Ok(e) = serde_json::from_slice::<Value>(&raw)
            && e.get("k").and_then(Value::as_str) == Some(key_raw)
            && let Some(r) = e.get("r")
        {
            return Ok(r.clone());
        }
        let r = f()?;
        let bytes = serde_json::to_vec(&json!({ "k": key_raw, "r": r }))
            .map_err(|_| invalid("internal"))?;
        self.kv.set(&kv_key, &bytes, IDEM_TTL_S).map_err(kv_err)?;
        Ok(r)
    }

    /// `music.spotify.player.next {device_id?}` (deduped per idempotency key).
    pub fn player_next(&self, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        require_scope(ctx, SCOPE_PLAYER)?;
        let a: DeviceArgs = parse_args(args)?;
        let q = Self::device_query(&a.device_id)?;
        self.once(ctx, "next", || {
            let resp = self.player("POST", "/me/player/next", &q, None)?;
            Self::ok_action(&resp, "next")
        })
    }

    /// `music.spotify.player.queue_add {track, device_id?}` (deduped per idempotency key).
    pub fn player_queue_add(&self, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
        require_scope(ctx, SCOPE_QUEUE)?;
        let a: QueueArgs = parse_args(args)?;
        let id = track_ref(&a.track)?;
        let mut q = vec![("uri", format!("spotify:track:{id}"))];
        q.extend(Self::device_query(&a.device_id)?);
        self.once(ctx, "queue_add", || {
            let resp = self.player("POST", "/me/player/queue", &q, None)?;
            let mut v = Self::ok_action(&resp, "queue_add")?;
            v["uri"] = json!(format!("spotify:track:{id}"));
            Ok(v)
        })
    }
}

fn rate_limited(secs: u64) -> ToolError {
    invalid(format!("rate_limited:retry_after={}", secs.max(1)))
}

fn map_http_err(e: HttpError, cred: Cred) -> ToolError {
    match e {
        HttpError::Denied(_) => invalid(match cred {
            Cred::User => "not_connected",
            Cred::App => "app_credentials_unavailable",
        }),
        HttpError::Timeout => ToolError::Timeout,
        HttpError::TooLarge(_) => invalid("upstream_too_large"),
        HttpError::RateLimited(s) => rate_limited(u64::from(s)),
        HttpError::Transport(_) => ToolError::TargetUnavailable,
    }
}

/// Maps a non-2xx upstream status onto a stable error code (2xx = `Ok`).
fn check_status(resp: &HttpResponse, cred: Cred) -> Result<(), ToolError> {
    let reason = body_reason(&resp.body);
    match resp.status {
        200..=299 => Ok(()),
        401 => Err(invalid(match cred {
            Cred::User => "connection_expired",
            Cred::App => "app_credentials_rejected",
        })),
        403 if reason.as_deref() == Some("PREMIUM_REQUIRED") => Err(invalid("premium_required")),
        403 => Err(invalid(match cred {
            Cred::User => "insufficient_scope",
            Cred::App => "forbidden",
        })),
        404 if reason.as_deref() == Some("NO_ACTIVE_DEVICE") => Err(invalid("no_active_device")),
        404 => Err(invalid("not_found")),
        400 => Err(invalid("upstream_bad_request")),
        500..=599 => Err(ToolError::TargetUnavailable),
        s => Err(invalid(format!("upstream_status_{s}"))),
    }
}

/// Builds the tool table over `svc` (shared by the wasm glue and the tests).
pub fn build_registry(svc: Arc<Spotify>) -> ToolRegistry {
    let mut r = ToolRegistry::new();
    type Tool = fn(&Spotify, &ToolContext, Value) -> Result<Value, ToolError>;
    let tools: [(&str, Tool); TOOL_NAMES.len()] = [
        ("music.spotify.search", Spotify::search),
        ("music.spotify.track", Spotify::track),
        ("music.spotify.playlist.get", Spotify::playlist_get),
        ("music.spotify.resolve", Spotify::resolve),
        ("music.spotify.player.state", Spotify::player_state),
        ("music.spotify.player.play", Spotify::player_play),
        ("music.spotify.player.pause", Spotify::player_pause),
        ("music.spotify.player.next", Spotify::player_next),
        ("music.spotify.player.queue_add", Spotify::player_queue_add),
    ];
    for (name, f) in tools {
        let s = Arc::clone(&svc);
        r.register(name, move |ctx, args| f(s.as_ref(), ctx, args));
    }
    r
}

/// In-memory fakes shared by unit and integration tests.
#[doc(hidden)]
pub mod testing {
    use super::*;
    use std::collections::{HashMap, VecDeque};
    use std::sync::Mutex;

    /// Scripted [`Http`]: pops responses in order, records requests.
    #[derive(Default)]
    pub struct FakeHttp {
        pub script: Mutex<VecDeque<Result<HttpResponse, HttpError>>>,
        pub seen: Mutex<Vec<HttpRequest>>,
    }

    impl FakeHttp {
        pub fn new() -> Arc<Self> {
            Arc::new(Self::default())
        }
        pub fn push(&self, status: u16, body: &Value) {
            self.push_h(status, &[], body);
        }
        pub fn push_h(&self, status: u16, headers: &[(&str, &str)], body: &Value) {
            self.script.lock().unwrap().push_back(Ok(HttpResponse {
                status,
                headers: headers
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.to_string()))
                    .collect(),
                body: if body.is_null() {
                    vec![]
                } else {
                    serde_json::to_vec(body).unwrap()
                },
                truncated: false,
            }));
        }
        pub fn push_err(&self, e: HttpError) {
            self.script.lock().unwrap().push_back(Err(e));
        }
        pub fn requests(&self) -> Vec<HttpRequest> {
            self.seen.lock().unwrap().clone()
        }
    }

    impl Http for FakeHttp {
        fn send(&self, req: &HttpRequest) -> Result<HttpResponse, HttpError> {
            self.seen.lock().unwrap().push(req.clone());
            self.script
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or(Err(HttpError::Transport("script exhausted".into())))
        }
    }

    /// In-memory [`Store`] (TTL ignored; expiry is carried in cached entries).
    #[derive(Default)]
    pub struct FakeStore {
        pub map: Mutex<HashMap<String, Vec<u8>>>,
    }

    impl FakeStore {
        pub fn new() -> Arc<Self> {
            Arc::new(Self::default())
        }
        pub fn put(&self, k: &str, v: &[u8]) {
            self.map.lock().unwrap().insert(k.into(), v.to_vec());
        }
    }

    impl Store for FakeStore {
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(self.map.lock().unwrap().get(key).cloned())
        }
        fn set(&self, key: &str, value: &[u8], _ttl_s: u32) -> Result<(), String> {
            self.map.lock().unwrap().insert(key.into(), value.to_vec());
            Ok(())
        }
        fn increment(&self, key: &str, delta: i64, _ttl_s: u32) -> Result<i64, String> {
            let mut m = self.map.lock().unwrap();
            let cur: i64 = m
                .get(key)
                .and_then(|b| std::str::from_utf8(b).ok())
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            let next = cur + delta;
            m.insert(key.into(), next.to_string().into_bytes());
            Ok(next)
        }
    }

    /// Settable [`Clock`].
    pub struct FakeClock(pub Mutex<u64>);

    impl FakeClock {
        pub fn new(ms: u64) -> Arc<Self> {
            Arc::new(Self(Mutex::new(ms)))
        }
        pub fn advance(&self, ms: u64) {
            *self.0.lock().unwrap() += ms;
        }
    }

    impl Clock for FakeClock {
        fn now_millis(&self) -> u64 {
            *self.0.lock().unwrap()
        }
    }

    /// A ready service plus handles on its fakes.
    pub struct Rig {
        pub http: Arc<FakeHttp>,
        pub kv: Arc<FakeStore>,
        pub clock: Arc<FakeClock>,
        pub svc: Arc<Spotify>,
    }

    impl Rig {
        pub fn new() -> Self {
            let (http, kv, clock) = (FakeHttp::new(), FakeStore::new(), FakeClock::new(1_000_000));
            let svc = Arc::new(Spotify::new(http.clone(), kv.clone(), clock.clone()));
            Self {
                http,
                kv,
                clock,
                svc,
            }
        }
        pub fn registry(&self) -> ToolRegistry {
            build_registry(self.svc.clone())
        }
        /// Calls a tool through the real registry as a fully-scoped actor.
        pub fn call(&self, name: &str, args: Value, key: Option<&str>) -> Result<Value, ToolError> {
            self.call_as(name, args, key, Some(&[SCOPE_PLAYER, SCOPE_QUEUE]))
        }

        /// Calls a tool with an explicit host context: `None` = no verified actor,
        /// `Some(scopes)` = a verified actor holding exactly those scopes.
        pub fn call_as(
            &self,
            name: &str,
            args: Value,
            key: Option<&str>,
            scopes: Option<&[&str]>,
        ) -> Result<Value, ToolError> {
            let mut a = args;
            if let Some(k) = key {
                a[waddle_sdk::tool::IDEMPOTENCY_ARG] = json!(k);
            }
            if let Some(sc) = scopes {
                a[waddle_sdk::tool::CTX_ARG] =
                    json!({"actor": "11111111-1111-4111-8111-111111111111", "scopes": sc});
            }
            let out = self
                .registry()
                .call(name, &serde_json::to_vec(&a).unwrap())?;
            Ok(serde_json::from_slice(&out).unwrap())
        }
    }

    impl Default for Rig {
        fn default() -> Self {
            Self::new()
        }
    }

    /// A realistic Spotify track object.
    pub fn spotify_track(id: &str, name: &str) -> Value {
        json!({
            "type": "track", "id": id, "name": name, "duration_ms": 215000, "explicit": false,
            "artists": [{"name": "Artist One"}, {"name": "Artist Two"}],
            "album": {"name": "Album", "images": [
                {"url": "https://i.scdn.co/small", "width": 64},
                {"url": "https://i.scdn.co/big", "width": 640}]},
            "external_urls": {"spotify": format!("https://open.spotify.com/track/{id}")},
        })
    }
}

#[cfg(test)]
mod tests {
    use super::testing::*;
    use super::*;

    const T1: &str = "4uLU6hMCjMI75M1A2tKUQC";
    const T2: &str = "7ouMYWpwJ422jRcDASZB7P";
    const P1: &str = "37i9dQZF1DXcBWIGoYBM5M";

    fn code(r: Result<Value, ToolError>) -> String {
        match r {
            Err(ToolError::InvalidArgs(m)) => m,
            Err(e) => format!("{e:?}"),
            Ok(v) => panic!("expected error, got {v}"),
        }
    }

    #[test]
    fn parse_ref_accepts_urls_and_uris() {
        let want = (Kind::Track, T1.to_string());
        for s in [
            format!("spotify:track:{T1}"),
            format!("https://open.spotify.com/track/{T1}"),
            format!("https://open.spotify.com/track/{T1}?si=abc#x"),
            format!("https://open.spotify.com/intl-de/track/{T1}"),
        ] {
            assert_eq!(parse_ref(&s).unwrap(), want, "{s}");
        }
        assert_eq!(
            parse_ref(&format!("spotify:playlist:{P1}")).unwrap().0,
            Kind::Playlist
        );
    }

    #[test]
    fn parse_ref_rejects_hostile_input() {
        for s in [
            "",
            "4uLU6hMCjMI75M1A2tKUQC",
            "http://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
            "https://open.spotify.com.evil.com/track/4uLU6hMCjMI75M1A2tKUQC",
            "https://evil.com/open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
            "https://spotify.link/abc123",
            "https://open.spotify.com/track/short",
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC/extra",
            "spotify:track:4uLU6hMCjMI75M1A2tKUQC:x",
            "spotify:track:../../me",
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQ\n",
        ] {
            assert!(parse_ref(s).is_err(), "{s:?}");
        }
        assert_eq!(
            code(parse_ref(&format!("spotify:album:{T1}")).map(|_| Value::Null)),
            "unsupported_kind"
        );
    }

    #[test]
    fn search_maps_tracks_and_uses_app_ref() {
        let r = Rig::new();
        r.http.push(200, &json!({"tracks": {"items": [spotify_track(T1, "Song"), {"type":"track","id":null,"name":"local"}]}}));
        let out = r
            .call(
                "music.spotify.search",
                json!({"query": "daft punk & co"}),
                None,
            )
            .unwrap();
        assert_eq!(out["tracks"].as_array().unwrap().len(), 1);
        assert_eq!(out["skipped"], 1);
        assert_eq!(out["cached"], false);
        assert!(out.get("count").is_none());
        let t = &out["tracks"][0];
        assert_eq!(t["artist"], "Artist One");
        assert_eq!(t["thumbnail"], "https://i.scdn.co/big");
        assert_eq!(t["uri"], format!("spotify:track:{T1}"));
        assert_eq!(out["capability"]["requires_premium"], true);
        let req = &r.http.requests()[0];
        assert_eq!(req.cred.secret_ref(), "spotify.app");
        assert_eq!(req.method, "GET");
        assert!(
            req.url.starts_with(
                "https://api.spotify.com/v1/search?q=daft%20punk%20%26%20co&type=track&limit=10"
            ),
            "{}",
            req.url
        );
    }

    #[test]
    fn search_empty_result_is_empty_list_not_error() {
        let r = Rig::new();
        r.http.push(200, &json!({"tracks": {"items": []}}));
        let out = r
            .call("music.spotify.search", json!({"query": "zzzz"}), None)
            .unwrap();
        assert_eq!(out["tracks"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn search_caches_until_ttl_expiry() {
        let r = Rig::new();
        r.http
            .push(200, &json!({"tracks": {"items": [spotify_track(T1, "A")]}}));
        r.http
            .push(200, &json!({"tracks": {"items": [spotify_track(T2, "B")]}}));
        let a = r
            .call("music.spotify.search", json!({"query": "x"}), None)
            .unwrap();
        let b = r
            .call("music.spotify.search", json!({"query": "x"}), None)
            .unwrap();
        assert_eq!(a["tracks"], b["tracks"]);
        assert_eq!(
            (a["cached"].clone(), b["cached"].clone()),
            (json!(false), json!(true))
        );
        assert_eq!(r.http.requests().len(), 1, "second call is a cache hit");
        r.clock.advance(301_000);
        let c = r
            .call("music.spotify.search", json!({"query": "x"}), None)
            .unwrap();
        assert_eq!(c["tracks"][0]["id"], T2);
        assert_eq!(r.http.requests().len(), 2);
    }

    #[test]
    fn search_cache_is_keyed_by_query() {
        let r = Rig::new();
        r.http
            .push(200, &json!({"tracks": {"items": [spotify_track(T1, "A")]}}));
        r.http
            .push(200, &json!({"tracks": {"items": [spotify_track(T2, "B")]}}));
        r.call("music.spotify.search", json!({"query": "x"}), None)
            .unwrap();
        r.call("music.spotify.search", json!({"query": "y"}), None)
            .unwrap();
        assert_eq!(r.http.requests().len(), 2);
    }

    #[test]
    fn read_tools_cannot_select_the_user_credential() {
        let r = Rig::new();
        let p = format!("spotify:playlist:{P1}");
        for (tool, args) in [
            (
                "music.spotify.search",
                json!({"query": "x", "auth": "connection"}),
            ),
            (
                "music.spotify.track",
                json!({"id": T2_URI(), "auth": "connection"}),
            ),
            (
                "music.spotify.playlist.get",
                json!({"playlist_id": p, "auth": "connection"}),
            ),
            (
                "music.spotify.resolve",
                json!({"url": T2_URI(), "auth": "connection"}),
            ),
            ("music.spotify.search", json!({"query": "x", "auth": "app"})),
        ] {
            assert!(r.call(tool, args.clone(), None).is_err(), "{tool} {args}");
        }
        assert!(r.http.requests().is_empty());
        // and every accepted metadata call rides the app credential only
        r.http.push(200, &spotify_track(T2, "B"));
        r.call("music.spotify.track", json!({"id": T2_URI()}), None)
            .unwrap();
        r.http.push(200, &json!({"tracks": {"items": []}}));
        r.call_as(
            "music.spotify.search",
            json!({"query": "z"}),
            None,
            Some(&[SCOPE_PLAYER, SCOPE_QUEUE]),
        )
        .unwrap();
        assert!(r.http.requests().iter().all(|q| q.cred == Cred::App));
    }

    #[test]
    fn player_tools_require_scope_before_any_host_call() {
        let tools = [
            ("music.spotify.player.state", json!({}), SCOPE_PLAYER),
            ("music.spotify.player.play", json!({}), SCOPE_PLAYER),
            ("music.spotify.player.pause", json!({}), SCOPE_PLAYER),
            ("music.spotify.player.next", json!({}), SCOPE_PLAYER),
            (
                "music.spotify.player.queue_add",
                json!({"track": T2_URI()}),
                SCOPE_QUEUE,
            ),
        ];
        for (tool, args, scope) in tools {
            let r = Rig::new();
            // no verified actor
            assert!(
                matches!(
                    r.call_as(tool, args.clone(), Some("k"), None),
                    Err(ToolError::Forbidden)
                ),
                "{tool} no actor"
            );
            // actor without the scope (holds only an unrelated one)
            assert!(
                matches!(
                    r.call_as(tool, args.clone(), Some("k"), Some(&["community:vip"])),
                    Err(ToolError::Forbidden)
                ),
                "{tool} wrong scope"
            );
            assert!(
                matches!(
                    r.call_as(tool, args.clone(), Some("k"), Some(&[])),
                    Err(ToolError::Forbidden)
                ),
                "{tool} empty scopes"
            );
            assert!(
                r.http.requests().is_empty(),
                "{tool}: no upstream before authz"
            );
            // correct scope allowed
            r.http.push(204, &Value::Null);
            assert!(
                r.call_as(tool, args.clone(), Some("k"), Some(&[scope]))
                    .is_ok(),
                "{tool} with {scope}"
            );
            assert_eq!(r.http.requests().len(), 1, "{tool}");
        }
    }

    #[test]
    fn queue_scope_does_not_grant_player_control_and_vice_versa() {
        let r = Rig::new();
        assert!(matches!(
            r.call_as(
                "music.spotify.player.pause",
                json!({}),
                Some("k"),
                Some(&[SCOPE_QUEUE])
            ),
            Err(ToolError::Forbidden)
        ));
        assert!(matches!(
            r.call_as(
                "music.spotify.player.queue_add",
                json!({"track": T2_URI()}),
                Some("k"),
                Some(&[SCOPE_PLAYER])
            ),
            Err(ToolError::Forbidden)
        ));
        assert!(r.http.requests().is_empty());
    }

    #[test]
    fn search_validates_args() {
        let r = Rig::new();
        for a in [
            json!({"query": ""}),
            json!({"query": "   "}),
            json!({"query": "a\u{0}b"}),
            json!({"query": "x".repeat(201)}),
            json!({"query": "x", "limit": 0}),
            json!({"query": "x", "limit": 26}),
            json!({"query": "x", "bogus": 1}),
            json!({}),
        ] {
            assert!(
                r.call("music.spotify.search", a.clone(), None).is_err(),
                "{a}"
            );
        }
        assert!(r.http.requests().is_empty(), "no upstream call on bad args");
    }

    #[test]
    fn track_by_url_and_errors() {
        let r = Rig::new();
        r.http.push(200, &spotify_track(T1, "Song"));
        let out = r
            .call(
                "music.spotify.track",
                json!({"id": format!("https://open.spotify.com/track/{T1}")}),
                None,
            )
            .unwrap();
        assert_eq!(out["track"]["title"], "Song");
        assert!(r.http.requests()[0].url.ends_with(&format!("/tracks/{T1}")));

        r.http.push(404, &json!({"error": {"status": 404}}));
        assert_eq!(
            code(r.call("music.spotify.track", json!({"id": T2_URI()}), None)),
            "not_found"
        );
        assert_eq!(
            code(r.call(
                "music.spotify.track",
                json!({"id": format!("spotify:playlist:{P1}")}),
                None
            )),
            "expected_track_reference"
        );
        assert_eq!(
            code(r.call("music.spotify.track", json!({"id": "nope"}), None)),
            "invalid_spotify_reference"
        );
    }

    #[allow(non_snake_case)]
    fn T2_URI() -> String {
        format!("spotify:track:{T2}")
    }

    #[test]
    fn playlist_skips_unplayable_and_reads_both_row_shapes() {
        let r = Rig::new();
        r.http.push(200, &json!({"id": P1, "name": "Mix", "owner": {"display_name": "Someone"}, "tracks": {"total": 4}}));
        r.http.push(
            200,
            &json!({"total": 4, "items": [
                {"track": spotify_track(T1, "A")},
                {"item": spotify_track(T2, "B")},
                {"track": null},
                {"track": {"type": "episode", "id": T1, "name": "pod"}},
            ]}),
        );
        let out = r
            .call(
                "music.spotify.playlist.get",
                json!({"playlist_id": P1_URI(), "limit": 25, "page_token": "50"}),
                None,
            )
            .unwrap();
        assert_eq!(out["tracks"].as_array().unwrap().len(), 2);
        assert_eq!(out["skipped"], 2);
        assert_eq!(out["next_page_token"], Value::Null, "50+4 rows >= total 4");
        assert_eq!(out["playlist"]["total"], 4);
        assert_eq!(out["playlist"]["name"], "Mix");
        let reqs = r.http.requests();
        assert!(
            reqs[1]
                .url
                .contains("/playlists/37i9dQZF1DXcBWIGoYBM5M/tracks?limit=25&offset=50"),
            "{}",
            reqs[1].url
        );
    }

    #[allow(non_snake_case)]
    fn P1_URI() -> String {
        format!("spotify:playlist:{P1}")
    }

    #[test]
    fn playlist_paging_bounds_and_tokens() {
        let r = Rig::new();
        for (a, want) in [
            (
                json!({"playlist_id": P1_URI(), "limit": 0}),
                "invalid_limit",
            ),
            (
                json!({"playlist_id": P1_URI(), "limit": 51}),
                "invalid_limit",
            ),
            (
                json!({"playlist_id": P1_URI(), "page_token": "abc"}),
                "invalid_page_token",
            ),
            (
                json!({"playlist_id": P1_URI(), "page_token": ""}),
                "invalid_page_token",
            ),
            (
                json!({"playlist_id": P1_URI(), "page_token": "100001"}),
                "invalid_page_token",
            ),
            (
                json!({"playlist_id": P1_URI(), "page_token": "-1"}),
                "invalid_page_token",
            ),
            (json!({"playlist": P1_URI()}), "invalid_args"),
            (
                json!({"playlist_id": P1_URI(), "offset": 1}),
                "invalid_args",
            ),
            (
                json!({"playlist_id": format!("spotify:track:{T1}")}),
                "expected_playlist_reference",
            ),
            (json!({"playlist_id": "x".repeat(65)}), "invalid_id"),
        ] {
            assert!(
                code(r.call("music.spotify.playlist.get", a.clone(), None)).starts_with(want),
                "{a}"
            );
        }
        assert!(r.http.requests().is_empty());
    }

    #[test]
    fn playlist_next_page_token_walks_pages() {
        let r = Rig::new();
        r.http
            .push(200, &json!({"name": "Mix", "tracks": {"total": 3}}));
        r.http.push(200, &json!({"items": [{"track": spotify_track(T1, "A")}, {"track": spotify_track(T2, "B")}]}));
        let a = r
            .call(
                "music.spotify.playlist.get",
                json!({"playlist_id": P1, "limit": 2}),
                None,
            )
            .unwrap();
        assert_eq!(a["next_page_token"], "2");
        r.http
            .push(200, &json!({"name": "Mix", "tracks": {"total": 3}}));
        r.http
            .push(200, &json!({"items": [{"track": spotify_track(T1, "A")}]}));
        let b = r
            .call(
                "music.spotify.playlist.get",
                json!({"playlist_id": P1, "limit": 2, "page_token": "2"}),
                None,
            )
            .unwrap();
        assert_eq!(b["next_page_token"], Value::Null);
        assert!(r.http.requests()[3].url.contains("limit=2&offset=2"));
        // second identical page read is a cache hit
        let c = r
            .call(
                "music.spotify.playlist.get",
                json!({"playlist_id": P1, "limit": 2, "page_token": "2"}),
                None,
            )
            .unwrap();
        assert_eq!(c["cached"], true);
        assert_eq!(r.http.requests().len(), 4);
    }

    #[test]
    fn resolve_returns_track_only_and_rejects_playlists() {
        let r = Rig::new();
        r.http.push(200, &spotify_track(T1, "Song"));
        let t = r
            .call(
                "music.spotify.resolve",
                json!({"url": format!("https://open.spotify.com/track/{T1}?si=1")}),
                None,
            )
            .unwrap();
        assert_eq!(t["track"]["id"], T1);
        assert_eq!(t["cached"], false);
        assert!(t.get("kind").is_none());
        assert_eq!(
            code(r.call("music.spotify.resolve", json!({"url": P1_URI()}), None)),
            "expected_track_reference"
        );
        assert_eq!(
            code(r.call(
                "music.spotify.resolve",
                json!({"url": "https://spotify.link/x"}),
                None
            )),
            "invalid_spotify_reference"
        );
        assert_eq!(r.http.requests().len(), 1);
    }

    #[test]
    fn upstream_429_stores_backoff_and_fails_fast() {
        let r = Rig::new();
        r.http.push_h(429, &[("Retry-After", "30")], &Value::Null);
        assert_eq!(
            code(r.call("music.spotify.track", json!({"id": T2_URI()}), None)),
            "rate_limited:retry_after=30"
        );
        // within the window: no upstream call at all
        r.clock.advance(10_000);
        assert_eq!(
            code(r.call("music.spotify.track", json!({"id": T2_URI()}), None)),
            "rate_limited:retry_after=20"
        );
        assert_eq!(r.http.requests().len(), 1);
        r.clock.advance(20_001);
        r.http.push(200, &spotify_track(T2, "B"));
        assert!(
            r.call("music.spotify.track", json!({"id": T2_URI()}), None)
                .is_ok()
        );
    }

    #[test]
    fn retry_after_is_clamped_and_defaulted() {
        let r = Rig::new();
        r.http
            .push_h(429, &[("retry-after", "999999")], &Value::Null);
        assert_eq!(
            code(r.call("music.spotify.track", json!({"id": T2_URI()}), None)),
            "rate_limited:retry_after=3600"
        );
        let r = Rig::new();
        r.http.push(429, &Value::Null);
        assert_eq!(
            code(r.call("music.spotify.track", json!({"id": T2_URI()}), None)),
            "rate_limited:retry_after=5"
        );
        let r = Rig::new();
        r.http.push_h(429, &[("Retry-After", "0")], &Value::Null);
        assert_eq!(
            code(r.call("music.spotify.track", json!({"id": T2_URI()}), None)),
            "rate_limited:retry_after=1"
        );
    }

    #[test]
    fn per_community_rate_limit_blocks_before_upstream() {
        let r = Rig::new();
        r.kv.put(KEY_CONFIG, br#"{"rate_limit_per_min": 2}"#);
        for _ in 0..2 {
            r.http.push(200, &json!({"tracks": {"items": []}}));
        }
        r.call("music.spotify.search", json!({"query": "a"}), None)
            .unwrap();
        r.call("music.spotify.search", json!({"query": "b"}), None)
            .unwrap();
        let e = code(r.call("music.spotify.search", json!({"query": "c"}), None));
        assert!(e.starts_with("rate_limited:retry_after="), "{e}");
        assert_eq!(r.http.requests().len(), 2);
        r.clock.advance(60_000);
        r.http.push(200, &json!({"tracks": {"items": []}}));
        assert!(
            r.call("music.spotify.search", json!({"query": "d"}), None)
                .is_ok()
        );
    }

    #[test]
    fn cache_hits_do_not_consume_rate_budget() {
        let r = Rig::new();
        r.kv.put(KEY_CONFIG, br#"{"rate_limit_per_min": 1}"#);
        r.http.push(200, &json!({"tracks": {"items": []}}));
        for _ in 0..5 {
            r.call("music.spotify.search", json!({"query": "a"}), None)
                .unwrap();
        }
        assert_eq!(r.http.requests().len(), 1);
    }

    #[test]
    fn config_invalid_is_loud_and_market_applies() {
        for bad in [
            &br#"{"market":"usa"}"#[..],
            br#"{"rate_limit_per_min":0}"#,
            br#"{"x":1}"#,
            b"not json",
        ] {
            let r = Rig::new();
            r.kv.put(KEY_CONFIG, bad);
            assert_eq!(
                code(r.call("music.spotify.track", json!({"id": T2_URI()}), None)),
                "invalid_config"
            );
            assert!(r.http.requests().is_empty());
        }
        let r = Rig::new();
        r.kv.put(KEY_CONFIG, br#"{"market":"DE"}"#);
        r.http.push(200, &spotify_track(T1, "A"));
        r.call(
            "music.spotify.track",
            json!({"id": format!("spotify:track:{T1}")}),
            None,
        )
        .unwrap();
        assert!(r.http.requests()[0].url.ends_with("?market=DE"));
    }

    #[test]
    fn upstream_status_mapping() {
        let cases: [(u16, Value, &str); 6] = [
            (401, Value::Null, "app_credentials_rejected"),
            (403, Value::Null, "forbidden"),
            (400, Value::Null, "upstream_bad_request"),
            (418, Value::Null, "upstream_status_418"),
            (200, Value::Null, "upstream_malformed"),
            (404, Value::Null, "not_found"),
        ];
        for (status, body, want) in cases {
            let r = Rig::new();
            r.http.push(status, &body);
            assert_eq!(
                code(r.call("music.spotify.track", json!({"id": T2_URI()}), None)),
                want
            );
        }
        let r = Rig::new();
        r.http.push(503, &Value::Null);
        assert!(matches!(
            r.call("music.spotify.track", json!({"id": T2_URI()}), None),
            Err(ToolError::TargetUnavailable)
        ));
    }

    #[test]
    fn host_http_errors_map_to_typed_codes() {
        let cases = [
            (HttpError::Denied("x".into()), "app_credentials_unavailable"),
            (HttpError::TooLarge(9), "upstream_too_large"),
            (HttpError::RateLimited(7), "rate_limited:retry_after=7"),
        ];
        for (e, want) in cases {
            let r = Rig::new();
            r.http.push_err(e);
            assert_eq!(
                code(r.call("music.spotify.track", json!({"id": T2_URI()}), None)),
                want
            );
        }
        let r = Rig::new();
        r.http.push_err(HttpError::Timeout);
        assert!(matches!(
            r.call("music.spotify.track", json!({"id": T2_URI()}), None),
            Err(ToolError::Timeout)
        ));
        let r = Rig::new();
        r.http.push_err(HttpError::Transport("dns".into()));
        assert!(matches!(
            r.call("music.spotify.track", json!({"id": T2_URI()}), None),
            Err(ToolError::TargetUnavailable)
        ));
    }

    #[test]
    fn player_state_inactive_and_active() {
        let r = Rig::new();
        r.http.push(204, &Value::Null);
        let out = r
            .call("music.spotify.player.state", json!({}), None)
            .unwrap();
        assert_eq!(out["active"], false);
        assert_eq!(r.http.requests()[0].cred.secret_ref(), "spotify.oauth");

        r.http.push(200, &json!({"is_playing": true, "progress_ms": 1234, "shuffle_state": true, "repeat_state": "track",
            "currently_playing_type": "track",
            "device": {"id": "abc123", "name": "Desk", "type": "Computer", "volume_percent": 40},
            "item": spotify_track(T1, "Song")}));
        let out = r
            .call("music.spotify.player.state", json!({}), None)
            .unwrap();
        assert_eq!(out["device"]["name"], "Desk");
        assert_eq!(out["track"]["id"], T1);
        assert_eq!(out["repeat"], "track");
    }

    #[test]
    fn player_tools_map_user_errors_explicitly() {
        let cases: [(u16, Value, &str); 5] = [
            (
                403,
                json!({"error": {"status": 403, "reason": "PREMIUM_REQUIRED"}}),
                "premium_required",
            ),
            (403, json!({"error": {"status": 403}}), "insufficient_scope"),
            (
                404,
                json!({"error": {"status": 404, "reason": "NO_ACTIVE_DEVICE"}}),
                "no_active_device",
            ),
            (401, Value::Null, "connection_expired"),
            (404, Value::Null, "not_found"),
        ];
        for (status, body, want) in cases {
            let r = Rig::new();
            r.http.push(status, &body);
            assert_eq!(
                code(r.call("music.spotify.player.pause", json!({}), Some("k1"))),
                want
            );
        }
        let r = Rig::new();
        r.http
            .push_err(HttpError::Denied("secret_unresolved".into()));
        assert_eq!(
            code(r.call("music.spotify.player.state", json!({}), None)),
            "not_connected"
        );
    }

    #[test]
    fn play_builds_bodies_and_validates() {
        let r = Rig::new();
        r.http.push(204, &Value::Null);
        r.call("music.spotify.player.play", json!({}), Some("k"))
            .unwrap();
        assert_eq!(r.http.requests()[0].body, None);
        assert_eq!(r.http.requests()[0].method, "PUT");

        r.http.push(204, &Value::Null);
        r.call("music.spotify.player.play", json!({"uris": [format!("https://open.spotify.com/track/{T1}"), T2_URI()], "position_ms": 5, "device_id": "dev-1"}), Some("k")).unwrap();
        let req = &r.http.requests()[1];
        assert!(req.url.ends_with("/me/player/play?device_id=dev-1"));
        let body: Value = serde_json::from_slice(req.body.as_ref().unwrap()).unwrap();
        assert_eq!(
            body["uris"],
            json!([format!("spotify:track:{T1}"), format!("spotify:track:{T2}")])
        );
        assert_eq!(body["position_ms"], 5);

        r.http.push(204, &Value::Null);
        r.call(
            "music.spotify.player.play",
            json!({"context_uri": P1_URI(), "offset": 3}),
            Some("k"),
        )
        .unwrap();
        let body: Value =
            serde_json::from_slice(r.http.requests()[2].body.as_ref().unwrap()).unwrap();
        assert_eq!(body["context_uri"], format!("spotify:playlist:{P1}"));
        assert_eq!(body["offset"]["position"], 3);

        let n = r.http.requests().len();
        for a in [
            json!({"uris": [T2_URI()], "context_uri": P1_URI()}),
            json!({"uris": []}),
            json!({"uris": ["spotify:track:bad"]}),
            json!({"context_uri": T2_URI()}),
            json!({"offset": 1}),
            json!({"device_id": "a b"}),
            json!({"device_id": "x&y=1"}),
            json!({"position_ms": 999999999999u64}),
            json!({"bogus": 1}),
        ] {
            assert!(
                r.call("music.spotify.player.play", a.clone(), Some("k"))
                    .is_err(),
                "{a}"
            );
        }
        assert_eq!(r.http.requests().len(), n);
    }

    #[test]
    fn writes_require_idempotency_key() {
        let r = Rig::new();
        for (t, a) in [
            ("music.spotify.player.play", json!({})),
            ("music.spotify.player.pause", json!({})),
            ("music.spotify.player.next", json!({})),
            ("music.spotify.player.queue_add", json!({"track": T2_URI()})),
        ] {
            assert_eq!(code(r.call(t, a, None)), "idempotency_key_required", "{t}");
        }
        assert!(r.http.requests().is_empty());
    }

    #[test]
    fn next_and_queue_add_apply_once_per_key() {
        let r = Rig::new();
        r.http.push(204, &Value::Null);
        let a = r
            .call("music.spotify.player.next", json!({}), Some("same"))
            .unwrap();
        let b = r
            .call("music.spotify.player.next", json!({}), Some("same"))
            .unwrap();
        assert_eq!(a, b);
        assert_eq!(r.http.requests().len(), 1, "redelivery must not skip twice");
        r.http.push(204, &Value::Null);
        r.call("music.spotify.player.next", json!({}), Some("other"))
            .unwrap();
        assert_eq!(r.http.requests().len(), 2);

        r.http.push(204, &Value::Null);
        let q = r
            .call(
                "music.spotify.player.queue_add",
                json!({"track": T2_URI(), "device_id": "d1"}),
                Some("q1"),
            )
            .unwrap();
        r.call(
            "music.spotify.player.queue_add",
            json!({"track": T2_URI()}),
            Some("q1"),
        )
        .unwrap();
        assert_eq!(q["uri"], format!("spotify:track:{T2}"));
        assert_eq!(r.http.requests().len(), 3);
        let req = &r.http.requests()[2];
        assert_eq!(req.method, "POST");
        assert!(
            req.url.contains(
                "/me/player/queue?uri=spotify%3Atrack%3A7ouMYWpwJ422jRcDASZB7P&device_id=d1"
            ),
            "{}",
            req.url
        );
    }

    #[test]
    fn failed_write_is_not_recorded_as_done() {
        let r = Rig::new();
        r.http
            .push(403, &json!({"error": {"reason": "PREMIUM_REQUIRED"}}));
        assert_eq!(
            code(r.call("music.spotify.player.next", json!({}), Some("k"))),
            "premium_required"
        );
        r.http.push(204, &Value::Null);
        assert!(
            r.call("music.spotify.player.next", json!({}), Some("k"))
                .is_ok()
        );
    }

    #[test]
    fn requests_never_carry_credentials() {
        let r = Rig::new();
        r.http.push(200, &spotify_track(T1, "A"));
        r.http.push(204, &Value::Null);
        r.call("music.spotify.track", json!({"id": T2_URI()}), None)
            .unwrap();
        r.call("music.spotify.player.pause", json!({}), Some("k"))
            .unwrap();
        for req in r.http.requests() {
            let all = format!("{req:?}").to_lowercase();
            assert!(
                !all.contains("bearer") && !all.contains("authorization"),
                "{all}"
            );
        }
    }

    #[test]
    fn unknown_tool_and_non_json_fail_loud() {
        let r = Rig::new();
        assert!(r.registry().call("music.spotify.nope", b"{}").is_err());
        assert!(
            r.registry()
                .call("music.spotify.search", b"not json")
                .is_err()
        );
        assert_eq!(
            tool_log_label("music.spotify.search"),
            "music.spotify.search"
        );
        assert_eq!(tool_log_label("attacker\nname"), "unknown");
    }

    #[test]
    fn kv_failure_fails_closed() {
        struct BadKv;
        impl Store for BadKv {
            fn get(&self, _: &str) -> Result<Option<Vec<u8>>, String> {
                Err("x".into())
            }
            fn set(&self, _: &str, _: &[u8], _: u32) -> Result<(), String> {
                Err("x".into())
            }
            fn increment(&self, _: &str, _: i64, _: u32) -> Result<i64, String> {
                Err("x".into())
            }
        }
        let http = FakeHttp::new();
        let svc = Arc::new(Spotify::new(
            http.clone(),
            Arc::new(BadKv),
            FakeClock::new(1),
        ));
        let reg = build_registry(svc);
        let out = reg.call(
            "music.spotify.track",
            format!(r#"{{"id":"spotify:track:{T1}"}}"#).as_bytes(),
        );
        assert!(matches!(out, Err(ToolError::TargetUnavailable)));
        assert!(http.requests().is_empty());
    }

    #[test]
    fn fnv_and_enc_are_stable() {
        assert_eq!(fnv_hex(""), "cbf29ce484222325");
        assert_eq!(enc("a b&c/é"), "a%20b%26c%2F%C3%A9");
    }
}
