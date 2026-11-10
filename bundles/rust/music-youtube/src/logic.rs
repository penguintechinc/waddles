//! Host-independent music-youtube tool logic.
//!
//! Every tool is a function over three seams -- [`HttpHost`] (host HTTP egress),
//! [`KvHost`] (the bundle's own community-scoped kv) and [`Clock`] -- so the whole
//! table is unit-tested on the host with fakes; `lib.rs` supplies the real wasm
//! implementations.
//!
//! Safety shape: args are validated first (unknown fields rejected, bounded sizes,
//! strict id charsets), every upstream call passes a per-community per-minute rate
//! limit AND a per-community daily quota-unit budget, metadata is cached with a TTL,
//! and every failure is a stable machine code -- never a default answer. Credentials
//! never appear here: requests carry only secret-REF names the host resolves.

use std::sync::Arc;

use serde::Deserialize;
use serde_json::{Value, json};
use waddle_sdk::tool::{ToolContext, ToolError, ToolRegistry};

use crate::parse;

/// Host-granted secret ref holding the Data API key (injected as `?key=`).
pub const SECRET_API_KEY: &str = "YOUTUBE_API_KEY";
/// Host-granted secret ref holding `Bearer <token>` for a community's connected account.
pub const SECRET_OAUTH_AUTHORIZATION: &str = "YOUTUBE_OAUTH_AUTHORIZATION";
/// YouTube Data API v3 base (the only egress host: `www.googleapis.com`).
pub const API_BASE: &str = "https://www.googleapis.com/youtube/v3";
/// kv key of the optional per-community config JSON.
pub const KEY_CONFIG: &str = "config";

/// Quota units per Data API method (YouTube's published cost table).
const COST_SEARCH: i64 = 100;
const COST_LIST: i64 = 1;
/// Longest accepted search query / url.
const MAX_QUERY: usize = 200;
const MAX_URL: usize = 512;
/// Largest `videos.list` batch the API accepts.
const MAX_BATCH: usize = 50;
const DAY_MS: u64 = 86_400_000;
/// Daily quota units of the ONE shared platform API key (YouTube's default project quota).
pub const GLOBAL_KEY_DAILY_UNITS: u32 = 10_000;
/// Largest share (percent) of the shared key's daily quota one community may spend in
/// `api_key` mode. Bundle kv is community-scoped (no global counter reachable from the
/// guest), so this hard clamp is what stops one community draining the shared key; the
/// host-side guard keyed by secret ref is the authoritative global counter. `oauth` mode
/// spends the community's OWN credential and is not clamped.
pub const COMMUNITY_SHARE_PCT: u32 = 10;
const MINUTE_MS: u64 = 60_000;

/// An HTTP response as the logic sees it.
#[derive(Debug, Clone)]
pub struct HttpResp {
    pub status: u16,
    pub body: Vec<u8>,
    pub truncated: bool,
}

/// Every way the host HTTP capability can fail (mirrors WIT `http.error`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HttpFail {
    Denied(String),
    Timeout,
    TooLarge,
    RateLimited(u32),
    Transport,
}

/// Host HTTP egress with host-side secret injection.
pub trait HttpHost: Send + Sync {
    /// GET `url`; `secret_refs` maps slot (`?key` / header name) -> secret REF name.
    fn get(&self, url: &str, secret_refs: &[(String, String)]) -> Result<HttpResp, HttpFail>;
}

/// The bundle's community-scoped kv.
pub trait KvHost: Send + Sync {
    /// Raw read; `Err` is a PII-free reason.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    /// Write with TTL seconds (0 = none).
    fn set(&self, key: &str, value: &[u8], ttl_seconds: u32) -> Result<(), String>;
    /// Atomic add, returns the new value.
    fn increment(&self, key: &str, delta: i64, ttl_seconds: u32) -> Result<i64, String>;
}

/// Wall clock (host-provided, ms since epoch).
pub trait Clock: Send + Sync {
    fn now_millis(&self) -> u64;
}

/// Auth mode for upstream calls.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AuthMode {
    ApiKey,
    Oauth,
}

/// Per-community config (kv `config`); absent = defaults, invalid = loud error.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct Config {
    pub auth_mode: AuthMode,
    /// Max upstream calls per community per minute.
    pub rate_per_min: u32,
    /// Max quota units per community per UTC day (clamped, see [`Config::effective_daily_units`]).
    pub daily_quota_units: u32,
    /// Track metadata cache TTL.
    pub cache_ttl_s: u32,
    /// Search/playlist result-id cache TTL.
    pub search_ttl_s: u32,
    /// Optional ISO 3166-1 alpha-2 `regionCode` for search.
    pub region_code: Option<String>,
    /// `none` | `moderate` | `strict`.
    pub safe_search: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            auth_mode: AuthMode::ApiKey,
            rate_per_min: 20,
            daily_quota_units: GLOBAL_KEY_DAILY_UNITS / 100 * COMMUNITY_SHARE_PCT,
            cache_ttl_s: 6 * 3600,
            search_ttl_s: 3600,
            region_code: None,
            safe_search: "moderate".to_string(),
        }
    }
}

impl Config {
    /// The community's real daily budget: in `api_key` mode (shared credential) clamped to
    /// its fraction of the global key budget; in `oauth` mode (own credential) as configured.
    pub fn effective_daily_units(&self) -> u32 {
        match self.auth_mode {
            AuthMode::ApiKey => self
                .daily_quota_units
                .min(GLOBAL_KEY_DAILY_UNITS / 100 * COMMUNITY_SHARE_PCT),
            AuthMode::Oauth => self.daily_quota_units,
        }
    }

    /// Bounds-checks a parsed config (cache <= 7 days: provider terms bound retention).
    fn validate(&self) -> Result<(), ToolError> {
        let ok = (1..=600).contains(&self.rate_per_min)
            && (100..=1_000_000).contains(&self.daily_quota_units)
            && (60..=604_800).contains(&self.cache_ttl_s)
            && (60..=604_800).contains(&self.search_ttl_s)
            && matches!(self.safe_search.as_str(), "none" | "moderate" | "strict")
            && self
                .region_code
                .as_deref()
                .is_none_or(|r| r.len() == 2 && r.bytes().all(|b| b.is_ascii_uppercase()));
        if ok {
            Ok(())
        } else {
            Err(invalid("config_invalid"))
        }
    }
}

fn invalid(msg: impl Into<String>) -> ToolError {
    ToolError::InvalidArgs(msg.into())
}

/// The tool table's shared state.
pub struct Service {
    http: Arc<dyn HttpHost>,
    kv: Arc<dyn KvHost>,
    clock: Arc<dyn Clock>,
}

/// Capability flags returned with every response so the DJ degrades loudly: YouTube
/// has no sanctioned server-side audio, so playback is an overlay embed only.
fn capability() -> Value {
    json!({"playback": "embed", "server_audio": false, "queue_add": false})
}

/// Rejects any field not in `allowed` (a typo must fail, never be ignored).
fn only_fields(args: &Value, allowed: &[&str]) -> Result<(), ToolError> {
    let obj = args
        .as_object()
        .ok_or_else(|| invalid("args must be an object"))?;
    match obj.keys().find(|k| !allowed.contains(&k.as_str())) {
        Some(k) => Err(invalid(format!("unknown field {k}"))),
        None => Ok(()),
    }
}

fn str_field<'a>(args: &'a Value, key: &str) -> Result<&'a str, ToolError> {
    args.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("{key} must be a string")))
}

fn limit_field(args: &Value, default: u64, max: u64) -> Result<u64, ToolError> {
    match args.get("limit") {
        None | Some(Value::Null) => Ok(default),
        Some(v) => v
            .as_u64()
            .filter(|n| (1..=max).contains(n))
            .ok_or_else(|| invalid(format!("limit must be an integer 1..={max}"))),
    }
}

/// Parses a cached/kv JSON blob; `None` on a corrupt entry (treated as a miss).
fn parse_json(bytes: &[u8]) -> Option<Value> {
    serde_json::from_slice(bytes).ok()
}

impl Service {
    /// Builds the service over the three seams.
    pub fn new(http: Arc<dyn HttpHost>, kv: Arc<dyn KvHost>, clock: Arc<dyn Clock>) -> Self {
        Self { http, kv, clock }
    }

    fn kv_err<T>(r: Result<T, String>) -> Result<T, ToolError> {
        r.map_err(|_| ToolError::TargetUnavailable)
    }

    /// Loads the community config: absent = defaults, present-but-invalid = error.
    fn config(&self) -> Result<Config, ToolError> {
        match Self::kv_err(self.kv.get(KEY_CONFIG))? {
            None => Ok(Config::default()),
            Some(raw) => {
                let cfg: Config =
                    serde_json::from_slice(&raw).map_err(|_| invalid("config_invalid"))?;
                cfg.validate()?;
                Ok(cfg)
            }
        }
    }

    /// One metered upstream GET: rate limit, quota budget, request, status mapping.
    fn upstream(
        &self,
        cfg: &Config,
        endpoint: &str,
        params: &[(&str, String)],
        units: i64,
    ) -> Result<Value, ToolError> {
        let now = self.clock.now_millis();
        let day = now / DAY_MS;
        let xkey = format!("quota.x.{day}");
        if Self::kv_err(self.kv.get(&xkey))?.is_some() {
            // The credential itself was reported exhausted today: fail fast, no upstream call.
            return Err(invalid("quota_exhausted"));
        }
        let minute = now / MINUTE_MS;
        let n = Self::kv_err(self.kv.increment(&format!("rl.m.{minute}"), 1, 120))?;
        if n > i64::from(cfg.rate_per_min) {
            return Err(invalid("rate_limited"));
        }
        let qkey = format!("quota.d.{day}");
        let used = Self::kv_err(self.kv.increment(&qkey, units, 2 * 86_400))?;
        if used > i64::from(cfg.effective_daily_units()) {
            // Refund: the call is not made, so it must not eat budget.
            Self::kv_err(self.kv.increment(&qkey, -units, 2 * 86_400))?;
            return Err(invalid("quota_exceeded"));
        }

        let query: Vec<String> = params
            .iter()
            .map(|(k, v)| format!("{k}={}", parse::percent_encode(v)))
            .collect();
        let url = format!("{API_BASE}/{endpoint}?{}", query.join("&"));
        let refs = match cfg.auth_mode {
            AuthMode::ApiKey => vec![("?key".to_string(), SECRET_API_KEY.to_string())],
            AuthMode::Oauth => vec![(
                "Authorization".to_string(),
                SECRET_OAUTH_AUTHORIZATION.to_string(),
            )],
        };
        let resp = self.http.get(&url, &refs).map_err(map_http_fail)?;
        let result = map_status(&resp);
        if result == Err(invalid("quota_exhausted")) {
            // Remember until the next UTC day so every later call fails fast, loudly.
            let ttl = ((day + 1) * DAY_MS - now).div_ceil(1000).clamp(60, 86_400) as u32;
            Self::kv_err(self.kv.set(&xkey, b"1", ttl))?;
        }
        result
    }

    /// Hydrates `ids` into tracks (cache first, one batched `videos.list` for the
    /// misses), then applies the community's `safe_search` policy to EVERY path (search,
    /// playlist, track, resolve). Cached rows are stored unfiltered, so a config change
    /// takes effect immediately.
    fn tracks_for(&self, cfg: &Config, ids: &[String]) -> Result<Hydrated, ToolError> {
        let mut found: Vec<Option<Value>> = Vec::with_capacity(ids.len());
        let mut missing: Vec<String> = Vec::new();
        let mut hit = false;
        for id in ids {
            let cached = Self::kv_err(self.kv.get(&format!("trk.{id}")))?
                .and_then(|b| parse_json(&b))
                .filter(|v| v["id"] == json!(id));
            if cached.is_some() {
                hit = true;
            } else {
                missing.push(id.clone());
            }
            found.push(cached);
        }
        for chunk in missing.chunks(MAX_BATCH) {
            let data = self.upstream(
                cfg,
                "videos",
                &[
                    (
                        "part",
                        "snippet,contentDetails,status,topicDetails".to_string(),
                    ),
                    ("id", chunk.join(",")),
                    ("maxResults", chunk.len().to_string()),
                ],
                COST_LIST,
            )?;
            for item in data["items"].as_array().map(Vec::as_slice).unwrap_or(&[]) {
                let track = parse::track_from_item(item).map_err(invalid)?;
                let id = track["id"]
                    .as_str()
                    .ok_or_else(|| invalid("upstream_track_missing_id"))?
                    .to_string();
                Self::kv_err(self.kv.set(
                    &format!("trk.{id}"),
                    track.to_string().as_bytes(),
                    cfg.cache_ttl_s,
                ))?;
                if let Some(pos) = ids.iter().position(|i| *i == id) {
                    found[pos] = Some(track);
                }
            }
        }
        let unavailable = found.iter().filter(|f| f.is_none()).count();
        let (tracks, blocked): (Vec<Value>, Vec<Value>) = found
            .into_iter()
            .flatten()
            .partition(|t| passes_safe_search(&cfg.safe_search, t));
        Ok(Hydrated {
            tracks,
            hit,
            unavailable,
            restricted: blocked.len(),
        })
    }

    /// `music.youtube.track`: id -> metadata.
    pub fn track(&self, args: Value) -> Result<Value, ToolError> {
        only_fields(&args, &["id"])?;
        let id = str_field(&args, "id")?;
        if !parse::is_video_id(id) {
            return Err(invalid("id must be an 11-character YouTube video id"));
        }
        self.track_by_id(id)
    }

    fn track_by_id(&self, id: &str) -> Result<Value, ToolError> {
        let cfg = self.config()?;
        let mut h = self.tracks_for(&cfg, &[id.to_string()])?;
        if h.restricted > 0 {
            return Err(invalid("restricted"));
        }
        let track = h.tracks.pop().ok_or_else(|| invalid("not_found"))?;
        Ok(json!({"track": track, "cached": h.hit, "capability": capability()}))
    }

    /// `music.youtube.resolve`: url -> track.
    pub fn resolve(&self, args: Value) -> Result<Value, ToolError> {
        only_fields(&args, &["url"])?;
        let url = str_field(&args, "url")?;
        if url.len() > MAX_URL || url.chars().any(char::is_control) {
            return Err(invalid("url invalid"));
        }
        let id = parse::extract_video_id(url).map_err(|e| {
            invalid(match e {
                parse::UrlError::NotYoutube => "not_a_youtube_url",
                parse::UrlError::PlaylistOnly => "playlist_url_use_playlist.get",
                parse::UrlError::NoVideoId => "no_video_id",
            })
        })?;
        self.track_by_id(&id)
    }

    /// `music.youtube.search`: query -> music tracks (videoCategoryId=10).
    pub fn search(&self, args: Value) -> Result<Value, ToolError> {
        only_fields(&args, &["query", "limit"])?;
        let query = str_field(&args, "query")?.trim();
        if query.is_empty() || query.len() > MAX_QUERY || query.chars().any(char::is_control) {
            return Err(invalid("query must be 1..=200 printable characters"));
        }
        let limit = limit_field(&args, 10, 25)?;
        let cfg = self.config()?;
        // No configured region is a valid configuration: search worldwide.
        let region = cfg.region_code.as_deref().unwrap_or("").to_string();
        let key = format!(
            "srch.{}",
            parse::fnv_hex(&format!(
                "{query}\u{1}{limit}\u{1}{region}\u{1}{}",
                cfg.safe_search
            ))
        );
        let cached = Self::kv_err(self.kv.get(&key))?
            .and_then(|b| parse_json(&b))
            .filter(|v| v["q"] == json!(query))
            .and_then(|v| id_list(&v["ids"]));
        let (ids, search_hit) = match cached {
            Some(ids) => (ids, true),
            None => {
                let mut params = vec![
                    ("part", "snippet".to_string()),
                    ("type", "video".to_string()),
                    ("videoCategoryId", "10".to_string()),
                    ("q", query.to_string()),
                    ("maxResults", limit.to_string()),
                    ("safeSearch", cfg.safe_search.clone()),
                ];
                if !region.is_empty() {
                    params.push(("regionCode", region));
                }
                let data = self.upstream(&cfg, "search", &params, COST_SEARCH)?;
                let ids = ids_from(&data, |i| i["id"]["videoId"].as_str())?;
                Self::kv_err(self.kv.set(
                    &key,
                    json!({"q": query, "ids": ids}).to_string().as_bytes(),
                    cfg.search_ttl_s,
                ))?;
                (ids, false)
            }
        };
        let h = self.tracks_for(&cfg, &ids)?;
        Ok(json!({
            "tracks": h.tracks,
            "cached": search_hit && h.hit,
            "skipped": h.unavailable + h.restricted,
            "restricted": h.restricted,
            "capability": capability(),
        }))
    }

    /// `music.youtube.playlist.get`: playlist id (+ page token) -> tracks.
    pub fn playlist_get(&self, args: Value) -> Result<Value, ToolError> {
        only_fields(&args, &["playlist_id", "limit", "page_token"])?;
        let pid = str_field(&args, "playlist_id")?;
        if !parse::is_playlist_id(pid) {
            return Err(invalid("playlist_id invalid"));
        }
        let limit = limit_field(&args, 50, 50)?;
        let token = match args.get("page_token") {
            None | Some(Value::Null) => None,
            Some(v) => Some(
                v.as_str()
                    .filter(|t| parse::is_page_token(t))
                    .ok_or_else(|| invalid("page_token invalid"))?,
            ),
        };
        let cfg = self.config()?;
        let key = format!(
            "pl.{}",
            parse::fnv_hex(&format!("{pid}\u{1}{limit}\u{1}{}", token.unwrap_or("")))
        );
        let cached = Self::kv_err(self.kv.get(&key))?
            .and_then(|b| parse_json(&b))
            .filter(|v| v["p"] == json!(pid))
            .and_then(|v| {
                id_list(&v["ids"]).map(|ids| (ids, v["next"].as_str().map(str::to_string)))
            });
        let ((ids, next), list_hit) = match cached {
            Some(c) => (c, true),
            None => {
                let mut params = vec![
                    ("part", "contentDetails".to_string()),
                    ("playlistId", pid.to_string()),
                    ("maxResults", limit.to_string()),
                ];
                if let Some(t) = token {
                    params.push(("pageToken", t.to_string()));
                }
                let data = self.upstream(&cfg, "playlistItems", &params, COST_LIST)?;
                let ids = ids_from(&data, |i| i["contentDetails"]["videoId"].as_str())?;
                let next = data["nextPageToken"]
                    .as_str()
                    .filter(|t| parse::is_page_token(t))
                    .map(str::to_string);
                Self::kv_err(
                    self.kv.set(
                        &key,
                        json!({"p": pid, "ids": ids, "next": next})
                            .to_string()
                            .as_bytes(),
                        cfg.search_ttl_s,
                    ),
                )?;
                ((ids, next), false)
            }
        };
        let h = self.tracks_for(&cfg, &ids)?;
        Ok(json!({
            "tracks": h.tracks,
            "next_page_token": next,
            "cached": list_hit && h.hit,
            "skipped": h.unavailable + h.restricted,
            "restricted": h.restricted,
            "capability": capability(),
        }))
    }
}

/// Result of hydrating ids: visible tracks plus how many were unavailable / policy-blocked.
struct Hydrated {
    tracks: Vec<Value>,
    hit: bool,
    unavailable: usize,
    restricted: usize,
}

/// `safe_search` policy on one track: `none` allows all; `moderate` drops age-restricted;
/// `strict` also drops anything whose rating could not be confirmed (fail closed -- a row
/// without `rating_known` is treated as unconfirmed).
fn passes_safe_search(mode: &str, track: &Value) -> bool {
    match mode {
        "none" => true,
        "moderate" => track["explicit"] != json!(true),
        _ => track["explicit"] != json!(true) && track["rating_known"] == json!(true),
    }
}

/// Validated, de-duplicated video ids from a cached array; `None` if any entry is bad.
fn id_list(v: &Value) -> Option<Vec<String>> {
    v.as_array()?
        .iter()
        .map(|i| {
            i.as_str()
                .filter(|s| parse::is_video_id(s))
                .map(str::to_string)
        })
        .collect()
}

/// Extracts ids from an upstream list response; a malformed id is a loud error.
fn ids_from(data: &Value, pick: impl Fn(&Value) -> Option<&str>) -> Result<Vec<String>, ToolError> {
    let mut ids: Vec<String> = Vec::new();
    for item in data["items"].as_array().map(Vec::as_slice).unwrap_or(&[]) {
        let id = pick(item)
            .filter(|i| parse::is_video_id(i))
            .ok_or_else(|| invalid("upstream_malformed"))?;
        if !ids.iter().any(|e| e == id) {
            ids.push(id.to_string());
        }
    }
    Ok(ids)
}

/// Maps a host HTTP failure onto the tool error taxonomy (stable codes).
fn map_http_fail(e: HttpFail) -> ToolError {
    match e {
        HttpFail::Denied(c) if c == "not_granted" => ToolError::NotGranted,
        HttpFail::Denied(_) => invalid("auth_unavailable"),
        HttpFail::Timeout => ToolError::Timeout,
        HttpFail::TooLarge => invalid("upstream_too_large"),
        HttpFail::RateLimited(_) => invalid("rate_limited"),
        HttpFail::Transport => ToolError::TargetUnavailable,
    }
}

/// Maps a Data API response to JSON or a stable error code.
fn map_status(resp: &HttpResp) -> Result<Value, ToolError> {
    match resp.status {
        200 => {
            if resp.truncated {
                return Err(invalid("upstream_truncated"));
            }
            serde_json::from_slice(&resp.body).map_err(|_| invalid("upstream_malformed"))
        }
        400 => Err(invalid("upstream_bad_request")),
        401 => Err(invalid("auth_unavailable")),
        403 => {
            let body = String::from_utf8_lossy(&resp.body);
            if body.contains("quotaExceeded") || body.contains("rateLimitExceeded") {
                Err(invalid("quota_exhausted"))
            } else {
                Err(invalid("upstream_forbidden"))
            }
        }
        404 => Err(invalid("not_found")),
        429 => Err(invalid("rate_limited")),
        s if s >= 500 => Err(ToolError::TargetUnavailable),
        _ => Err(invalid("upstream_error")),
    }
}

/// Known tool names (the only values ever logged as a tool label).
pub const TOOLS: [&str; 4] = [
    "music.youtube.search",
    "music.youtube.track",
    "music.youtube.playlist.get",
    "music.youtube.resolve",
];

/// A loggable label: the known tool name, else `unknown` (never caller text).
pub fn tool_log_label(name: &str) -> &'static str {
    TOOLS
        .iter()
        .find(|t| **t == name)
        .copied()
        .unwrap_or("unknown")
}

/// Stable error codes safe to log; anything else collapses to `invalid_args`.
const OUTCOME_CODES: [&str; 16] = [
    "rate_limited",
    "quota_exceeded",
    "quota_exhausted",
    "restricted",
    "not_found",
    "auth_unavailable",
    "config_invalid",
    "upstream_bad_request",
    "upstream_forbidden",
    "upstream_malformed",
    "upstream_truncated",
    "upstream_too_large",
    "upstream_error",
    "not_a_youtube_url",
    "playlist_url_use_playlist.get",
    "no_video_id",
];

/// Maps an `invalid-args` message to a loggable outcome code (never caller text).
pub fn outcome_code(msg: &str) -> &'static str {
    OUTCOME_CODES
        .iter()
        .find(|c| **c == msg)
        .copied()
        .unwrap_or("invalid_args")
}

/// Builds the tool registry over `svc`.
pub fn build_registry(svc: Arc<Service>) -> ToolRegistry {
    let mut r = ToolRegistry::new();
    let s = svc.clone();
    r.register("music.youtube.search", move |_c: &ToolContext, a| {
        s.search(a)
    });
    let s = svc.clone();
    r.register("music.youtube.track", move |_c: &ToolContext, a| s.track(a));
    let s = svc.clone();
    r.register("music.youtube.playlist.get", move |_c: &ToolContext, a| {
        s.playlist_get(a)
    });
    let s = svc;
    r.register("music.youtube.resolve", move |_c: &ToolContext, a| {
        s.resolve(a)
    });
    r
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Mutex;

    #[derive(Default)]
    struct FakeKv(Mutex<HashMap<String, Vec<u8>>>);
    impl KvHost for FakeKv {
        fn get(&self, k: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(self.0.lock().unwrap().get(k).cloned())
        }
        fn set(&self, k: &str, v: &[u8], _t: u32) -> Result<(), String> {
            self.0.lock().unwrap().insert(k.into(), v.to_vec());
            Ok(())
        }
        fn increment(&self, k: &str, d: i64, _t: u32) -> Result<i64, String> {
            let mut m = self.0.lock().unwrap();
            let cur: i64 = m
                .get(k)
                .and_then(|b| String::from_utf8(b.clone()).ok())
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            m.insert(k.into(), (cur + d).to_string().into_bytes());
            Ok(cur + d)
        }
    }

    struct FixedClock(u64);
    impl Clock for FixedClock {
        fn now_millis(&self) -> u64 {
            self.0
        }
    }

    type Call = (String, Vec<(String, String)>);

    /// Scripted upstream: responses popped in order; records (url, refs).
    struct FakeHttp {
        script: Mutex<Vec<Result<HttpResp, HttpFail>>>,
        calls: Mutex<Vec<Call>>,
    }
    impl FakeHttp {
        fn new(script: Vec<Result<HttpResp, HttpFail>>) -> Arc<Self> {
            Arc::new(Self {
                script: Mutex::new(script.into_iter().rev().collect()),
                calls: Mutex::new(vec![]),
            })
        }
        fn count(&self) -> usize {
            self.calls.lock().unwrap().len()
        }
    }
    impl HttpHost for FakeHttp {
        fn get(&self, url: &str, refs: &[(String, String)]) -> Result<HttpResp, HttpFail> {
            self.calls.lock().unwrap().push((url.into(), refs.to_vec()));
            self.script
                .lock()
                .unwrap()
                .pop()
                .expect("unscripted upstream call")
        }
    }

    fn ok(v: Value) -> Result<HttpResp, HttpFail> {
        Ok(HttpResp {
            status: 200,
            body: v.to_string().into_bytes(),
            truncated: false,
        })
    }

    fn status(s: u16, body: &str) -> Result<HttpResp, HttpFail> {
        Ok(HttpResp {
            status: s,
            body: body.as_bytes().to_vec(),
            truncated: false,
        })
    }

    const A: &str = "aaaaaaaaaaa";
    const B: &str = "bbbbbbbbbbb";

    fn video(id: &str, title: &str) -> Value {
        json!({"id": id, "snippet": {"title": title, "channelTitle": "Ch", "thumbnails": {}},
               "contentDetails": {"duration": "PT3M"}, "status": {"embeddable": true}})
    }

    fn svc(http: Arc<FakeHttp>, kv: Arc<FakeKv>) -> Service {
        Service::new(http, kv, Arc::new(FixedClock(1_700_000_000_000)))
    }

    fn err_msg(e: ToolError) -> String {
        match e {
            ToolError::InvalidArgs(m) => m,
            other => format!("{other:?}"),
        }
    }

    #[test]
    fn track_fetches_then_serves_from_cache() {
        let http = FakeHttp::new(vec![ok(json!({"items": [video(A, "Song")]}))]);
        let s = svc(http.clone(), Arc::new(FakeKv::default()));
        let r = s.track(json!({"id": A})).unwrap();
        assert_eq!(r["track"]["title"], "Song");
        assert_eq!(r["cached"], false);
        assert_eq!(r["capability"]["server_audio"], false);
        let r2 = s.track(json!({"id": A})).unwrap();
        assert_eq!(r2["cached"], true);
        assert_eq!(http.count(), 1, "second call must not hit upstream");
    }

    #[test]
    fn request_carries_only_secret_ref_names_never_values() {
        let http = FakeHttp::new(vec![ok(json!({"items": [video(A, "S")]}))]);
        let s = svc(http.clone(), Arc::new(FakeKv::default()));
        s.track(json!({"id": A})).unwrap();
        let (url, refs) = http.calls.lock().unwrap()[0].clone();
        assert!(url.starts_with("https://www.googleapis.com/youtube/v3/videos?"));
        assert!(
            !url.contains("key="),
            "key must be host-injected, not in guest URL"
        );
        assert_eq!(
            refs,
            vec![("?key".to_string(), "YOUTUBE_API_KEY".to_string())]
        );
    }

    #[test]
    fn oauth_mode_uses_authorization_header_ref() {
        let kv = Arc::new(FakeKv::default());
        kv.set("config", br#"{"auth_mode":"oauth"}"#, 0).unwrap();
        let http = FakeHttp::new(vec![ok(json!({"items": [video(A, "S")]}))]);
        svc(http.clone(), kv).track(json!({"id": A})).unwrap();
        assert_eq!(
            http.calls.lock().unwrap()[0].1,
            vec![(
                "Authorization".to_string(),
                "YOUTUBE_OAUTH_AUTHORIZATION".to_string()
            )]
        );
    }

    #[test]
    fn track_not_found_and_bad_id() {
        let http = FakeHttp::new(vec![ok(json!({"items": []}))]);
        let s = svc(http, Arc::new(FakeKv::default()));
        assert_eq!(err_msg(s.track(json!({"id": A})).unwrap_err()), "not_found");
        assert!(s.track(json!({"id": "bad"})).is_err());
        assert!(s.track(json!({"id": A, "extra": 1})).is_err());
        assert!(s.track(json!({})).is_err());
        assert!(s.track(json!([])).is_err());
    }

    #[test]
    fn search_uses_music_category_caches_and_hydrates() {
        let http = FakeHttp::new(vec![
            ok(
                json!({"items": [{"id": {"videoId": A}}, {"id": {"videoId": B}}, {"id": {"videoId": A}}]}),
            ),
            ok(json!({"items": [video(A, "One"), video(B, "Two")]})),
        ]);
        let s = svc(http.clone(), Arc::new(FakeKv::default()));
        let r = s
            .search(json!({"query": " lofi beats ", "limit": 2}))
            .unwrap();
        assert_eq!(r["tracks"].as_array().unwrap().len(), 2);
        assert_eq!(r["tracks"][0]["title"], "One");
        assert_eq!(r["cached"], false);
        let url = http.calls.lock().unwrap()[0].0.clone();
        assert!(url.contains("videoCategoryId=10") && url.contains("type=video"));
        assert!(url.contains("q=lofi%20beats") && url.contains("safeSearch=moderate"));
        let r2 = s
            .search(json!({"query": "lofi beats", "limit": 2}))
            .unwrap();
        assert_eq!(r2["cached"], true);
        assert_eq!(http.count(), 2);
    }

    #[test]
    fn search_empty_results_is_empty_list_and_validates() {
        let http = FakeHttp::new(vec![ok(json!({"items": []}))]);
        let s = svc(http, Arc::new(FakeKv::default()));
        let r = s.search(json!({"query": "zzzz"})).unwrap();
        assert_eq!(r["tracks"], json!([]));
        for bad in [
            json!({"query": ""}),
            json!({"query": "   "}),
            json!({"query": "a\nb"}),
            json!({"query": "x".repeat(201)}),
            json!({"query": "ok", "limit": 0}),
            json!({"query": "ok", "limit": 26}),
            json!({"query": "ok", "limit": "5"}),
            json!({"query": "ok", "nope": 1}),
            json!({}),
        ] {
            assert!(s.search(bad.clone()).is_err(), "{bad}");
        }
    }

    #[test]
    fn search_skips_unavailable_videos_and_reports_count() {
        let http = FakeHttp::new(vec![
            ok(json!({"items": [{"id": {"videoId": A}}, {"id": {"videoId": B}}]})),
            ok(json!({"items": [video(B, "Two")]})),
        ]);
        let r = svc(http, Arc::new(FakeKv::default()))
            .search(json!({"query": "q"}))
            .unwrap();
        assert_eq!(r["tracks"].as_array().unwrap().len(), 1);
        assert_eq!(r["skipped"], 1);
    }

    #[test]
    fn search_malformed_upstream_id_fails_loud() {
        let http = FakeHttp::new(vec![ok(json!({"items": [{"id": {"videoId": "x"}}]}))]);
        let e = svc(http, Arc::new(FakeKv::default()))
            .search(json!({"query": "q"}))
            .unwrap_err();
        assert_eq!(err_msg(e), "upstream_malformed");
    }

    #[test]
    fn playlist_get_paginates_and_caches() {
        let pid = "PLabcdefghij";
        let http = FakeHttp::new(vec![
            ok(json!({"items": [{"contentDetails": {"videoId": A}}], "nextPageToken": "CAEQAA"})),
            ok(json!({"items": [video(A, "One")]})),
        ]);
        let s = svc(http.clone(), Arc::new(FakeKv::default()));
        let r = s
            .playlist_get(json!({"playlist_id": pid, "limit": 1}))
            .unwrap();
        assert_eq!(r["next_page_token"], "CAEQAA");
        assert_eq!(r["tracks"][0]["id"], A);
        let r2 = s
            .playlist_get(json!({"playlist_id": pid, "limit": 1}))
            .unwrap();
        assert_eq!((r2["cached"].clone(), http.count()), (json!(true), 2));
        assert_eq!(r2["next_page_token"], "CAEQAA");
    }

    #[test]
    fn playlist_validation() {
        let s = svc(FakeHttp::new(vec![]), Arc::new(FakeKv::default()));
        for bad in [
            json!({"playlist_id": "short"}),
            json!({"playlist_id": "PLabcdefghij", "limit": 51}),
            json!({"playlist_id": "PLabcdefghij", "page_token": "a b"}),
            json!({"playlist_id": "PLabcdefghij", "page_token": 5}),
            json!({"playlist_id": "PLabcdefghij", "x": 1}),
            json!({}),
        ] {
            assert!(s.playlist_get(bad.clone()).is_err(), "{bad}");
        }
    }

    #[test]
    fn resolve_maps_url_errors_and_hits_track() {
        let http = FakeHttp::new(vec![ok(json!({"items": [video(A, "S")]}))]);
        let s = svc(http, Arc::new(FakeKv::default()));
        let r = s
            .resolve(json!({"url": format!("https://youtu.be/{A}")}))
            .unwrap();
        assert_eq!(r["track"]["id"], A);
        assert_eq!(
            err_msg(
                s.resolve(json!({"url": "https://evil.com/watch?v=aaaaaaaaaaa"}))
                    .unwrap_err()
            ),
            "not_a_youtube_url"
        );
        assert_eq!(
            err_msg(
                s.resolve(json!({"url": "https://www.youtube.com/playlist?list=PLabcdefghij"}))
                    .unwrap_err()
            ),
            "playlist_url_use_playlist.get"
        );
        assert_eq!(
            err_msg(
                s.resolve(json!({"url": "https://youtube.com/"}))
                    .unwrap_err()
            ),
            "no_video_id"
        );
        assert!(
            s.resolve(json!({"url": "https://youtu.be/a\u{0}b"}))
                .is_err()
        );
        assert!(s.resolve(json!({"url": "x".repeat(600)})).is_err());
    }

    #[test]
    fn rate_limit_blocks_upstream_but_not_cache() {
        let kv = Arc::new(FakeKv::default());
        kv.set("config", br#"{"rate_per_min":1}"#, 0).unwrap();
        let http = FakeHttp::new(vec![ok(json!({"items": [video(A, "S")]}))]);
        let s = svc(http.clone(), kv);
        s.track(json!({"id": A})).unwrap();
        // cache hit does not consume the limiter
        s.track(json!({"id": A})).unwrap();
        let e = s.track(json!({"id": B})).unwrap_err();
        assert_eq!(err_msg(e), "rate_limited");
        assert_eq!(http.count(), 1);
    }

    #[test]
    fn daily_quota_budget_is_enforced_and_refunded() {
        let kv = Arc::new(FakeKv::default());
        kv.set("config", br#"{"daily_quota_units":100}"#, 0)
            .unwrap();
        let http = FakeHttp::new(vec![ok(json!({"items": []}))]);
        let s = svc(http.clone(), kv.clone());
        // search costs 100: first fits exactly, second would exceed.
        s.search(json!({"query": "a"})).unwrap();
        let e = s.search(json!({"query": "b"})).unwrap_err();
        assert_eq!(err_msg(e), "quota_exceeded");
        assert_eq!(http.count(), 1);
        let day = 1_700_000_000_000u64 / DAY_MS;
        let used = kv.get(&format!("quota.d.{day}")).unwrap().unwrap();
        assert_eq!(
            String::from_utf8(used).unwrap(),
            "100",
            "refund keeps budget exact"
        );
    }

    #[test]
    fn upstream_status_mapping() {
        let cases: Vec<(Result<HttpResp, HttpFail>, &str)> = vec![
            (status(400, "{}"), "upstream_bad_request"),
            (status(401, "{}"), "auth_unavailable"),
            (
                status(403, r#"{"error":{"errors":[{"reason":"quotaExceeded"}]}}"#),
                "quota_exhausted",
            ),
            (status(403, "{}"), "upstream_forbidden"),
            (status(404, "{}"), "not_found"),
            (status(429, "{}"), "rate_limited"),
            (status(418, "{}"), "upstream_error"),
            (status(200, "not json"), "upstream_malformed"),
            (
                Ok(HttpResp {
                    status: 200,
                    body: b"{}".to_vec(),
                    truncated: true,
                }),
                "upstream_truncated",
            ),
            (Err(HttpFail::TooLarge), "upstream_too_large"),
            (Err(HttpFail::RateLimited(3)), "rate_limited"),
            (
                Err(HttpFail::Denied("secret_unresolved".into())),
                "auth_unavailable",
            ),
        ];
        for (resp, want) in cases {
            let s = svc(FakeHttp::new(vec![resp]), Arc::new(FakeKv::default()));
            assert_eq!(err_msg(s.track(json!({"id": A})).unwrap_err()), want);
        }
        let s = svc(
            FakeHttp::new(vec![status(503, "")]),
            Arc::new(FakeKv::default()),
        );
        assert_eq!(
            s.track(json!({"id": A})).unwrap_err(),
            ToolError::TargetUnavailable
        );
        let s = svc(
            FakeHttp::new(vec![Err(HttpFail::Timeout)]),
            Arc::new(FakeKv::default()),
        );
        assert_eq!(s.track(json!({"id": A})).unwrap_err(), ToolError::Timeout);
        let s = svc(
            FakeHttp::new(vec![Err(HttpFail::Transport)]),
            Arc::new(FakeKv::default()),
        );
        assert_eq!(
            s.track(json!({"id": A})).unwrap_err(),
            ToolError::TargetUnavailable
        );
        let s = svc(
            FakeHttp::new(vec![Err(HttpFail::Denied("not_granted".into()))]),
            Arc::new(FakeKv::default()),
        );
        assert_eq!(
            s.track(json!({"id": A})).unwrap_err(),
            ToolError::NotGranted
        );
    }

    #[test]
    fn invalid_config_fails_loud() {
        for bad in [
            r#"{"auth_mode":"password"}"#,
            r#"{"rate_per_min":0}"#,
            r#"{"rate_per_min":601}"#,
            r#"{"daily_quota_units":99}"#,
            r#"{"cache_ttl_s":10}"#,
            r#"{"cache_ttl_s":9999999}"#,
            r#"{"safe_search":"off"}"#,
            r#"{"region_code":"us"}"#,
            r#"{"unknown":1}"#,
            "not json",
        ] {
            let kv = Arc::new(FakeKv::default());
            kv.set("config", bad.as_bytes(), 0).unwrap();
            let s = svc(FakeHttp::new(vec![]), kv);
            assert_eq!(
                err_msg(s.track(json!({"id": A})).unwrap_err()),
                "config_invalid",
                "{bad}"
            );
        }
        let kv = Arc::new(FakeKv::default());
        kv.set(
            "config",
            br#"{"region_code":"US","safe_search":"strict"}"#,
            0,
        )
        .unwrap();
        let http = FakeHttp::new(vec![ok(json!({"items": []}))]);
        svc(http.clone(), kv).search(json!({"query": "q"})).unwrap();
        let url = http.calls.lock().unwrap()[0].0.clone();
        assert!(url.contains("regionCode=US") && url.contains("safeSearch=strict"));
    }

    #[test]
    fn corrupt_or_foreign_cache_entry_is_a_miss() {
        let kv = Arc::new(FakeKv::default());
        kv.set(&format!("trk.{A}"), b"garbage", 60).unwrap();
        let http = FakeHttp::new(vec![ok(json!({"items": [video(A, "Fresh")]}))]);
        let r = svc(http, kv).track(json!({"id": A})).unwrap();
        assert_eq!(r["track"]["title"], "Fresh");
    }

    #[test]
    fn registry_routes_and_unknown_tool_fails() {
        let http = FakeHttp::new(vec![ok(json!({"items": [video(A, "S")]}))]);
        let reg = build_registry(Arc::new(svc(http, Arc::new(FakeKv::default()))));
        let out = reg
            .call(
                "music.youtube.track",
                json!({"id": A}).to_string().as_bytes(),
            )
            .unwrap();
        assert!(
            String::from_utf8(out)
                .unwrap()
                .contains("\"provider\":\"youtube\"")
        );
        assert!(reg.call("music.youtube.nope", b"{}").is_err());
        assert_eq!(
            tool_log_label("music.youtube.search"),
            "music.youtube.search"
        );
        assert_eq!(tool_log_label("attacker text"), "unknown");
        for t in TOOLS {
            assert!(
                reg.call(t, b"{}").is_err(),
                "{t} must reject empty args, not default"
            );
        }
    }

    #[test]
    fn kv_failure_is_target_unavailable() {
        struct BadKv;
        impl KvHost for BadKv {
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
        let s = Service::new(
            FakeHttp::new(vec![]),
            Arc::new(BadKv),
            Arc::new(FixedClock(0)),
        );
        assert_eq!(
            s.track(json!({"id": A})).unwrap_err(),
            ToolError::TargetUnavailable
        );
    }

    fn restricted_video(id: &str) -> Value {
        let mut v = video(id, "R");
        v["contentDetails"]["contentRating"] = json!({"ytRating": "ytAgeRestricted"});
        v
    }

    fn rated_video(id: &str, title: &str) -> Value {
        let mut v = video(id, title);
        v["contentDetails"]["contentRating"] = json!({});
        v
    }

    fn with_config(cfg: &str) -> Arc<FakeKv> {
        let kv = Arc::new(FakeKv::default());
        kv.set("config", cfg.as_bytes(), 0).unwrap();
        kv
    }

    fn empty_searches(n: usize) -> Arc<FakeHttp> {
        FakeHttp::new((0..n).map(|_| ok(json!({"items": []}))).collect())
    }

    #[test]
    fn api_key_mode_clamps_community_to_global_fraction() {
        // Asks for 100k/day, but the shared key only allows its fraction (10% of 10k = 1000).
        let http = empty_searches(11);
        let s = svc(
            http.clone(),
            with_config(r#"{"daily_quota_units":100000,"rate_per_min":600}"#),
        );
        for i in 0..10 {
            s.search(json!({"query": format!("q{i}")})).unwrap();
        }
        let e = s.search(json!({"query": "q10"})).unwrap_err();
        assert_eq!(err_msg(e), "quota_exceeded");
        assert_eq!(http.count(), 10, "11th call must not reach upstream");
        assert_eq!(Config::default().effective_daily_units(), 1000);
    }

    #[test]
    fn oauth_mode_spends_own_credential_and_is_not_clamped() {
        let http = empty_searches(11);
        let s = svc(
            http.clone(),
            with_config(r#"{"auth_mode":"oauth","daily_quota_units":1100,"rate_per_min":600}"#),
        );
        for i in 0..11 {
            s.search(json!({"query": format!("q{i}")})).unwrap();
        }
        assert_eq!(http.count(), 11);
        assert_eq!(
            err_msg(s.search(json!({"query": "more"})).unwrap_err()),
            "quota_exceeded"
        );
    }

    #[test]
    fn global_credential_exhaustion_is_explicit_and_fails_fast() {
        let http = FakeHttp::new(vec![status(
            403,
            r#"{"error":{"errors":[{"reason":"quotaExceeded"}]}}"#,
        )]);
        let s = svc(http.clone(), Arc::new(FakeKv::default()));
        assert_eq!(
            err_msg(s.search(json!({"query": "a"})).unwrap_err()),
            "quota_exhausted"
        );
        // Every later tool call that day fails fast without touching upstream.
        for r in [
            s.track(json!({"id": A})),
            s.search(json!({"query": "b"})),
            s.playlist_get(json!({"playlist_id": "PLabcdefghij"})),
        ] {
            assert_eq!(err_msg(r.unwrap_err()), "quota_exhausted");
        }
        assert_eq!(http.count(), 1);
        // Cached data is still served (no upstream needed): not under test here, but the
        // breaker must expire the next UTC day.
        let next_day = Service::new(
            http,
            s.kv.clone(),
            Arc::new(FixedClock(1_700_000_000_000 + DAY_MS)),
        );
        assert!(next_day.config().is_ok());
        assert!(
            next_day
                .kv
                .get(&format!(
                    "quota.x.{}",
                    (1_700_000_000_000 + DAY_MS) / DAY_MS
                ))
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn safe_search_moderate_filters_every_tool() {
        let rid = "rrrrrrrrrrr";
        let http = FakeHttp::new(vec![
            // search: ids + hydrate
            ok(json!({"items": [{"id": {"videoId": rid}}, {"id": {"videoId": B}}]})),
            ok(json!({"items": [restricted_video(rid), rated_video(B, "Ok")]})),
            // playlist ids (hydrate cached)
            ok(
                json!({"items": [{"contentDetails": {"videoId": rid}}, {"contentDetails": {"videoId": B}}]}),
            ),
        ]);
        let s = svc(http, Arc::new(FakeKv::default()));
        let r = s.search(json!({"query": "q"})).unwrap();
        assert_eq!(r["tracks"].as_array().unwrap().len(), 1);
        assert_eq!(r["tracks"][0]["id"], B);
        assert_eq!(
            (r["restricted"].clone(), r["skipped"].clone()),
            (json!(1), json!(1))
        );
        let p = s
            .playlist_get(json!({"playlist_id": "PLabcdefghij"}))
            .unwrap();
        assert_eq!(p["tracks"].as_array().unwrap().len(), 1);
        assert_eq!(p["restricted"], 1);
        assert_eq!(
            err_msg(s.track(json!({"id": rid})).unwrap_err()),
            "restricted"
        );
        assert_eq!(
            err_msg(
                s.resolve(json!({"url": format!("https://youtu.be/{rid}")}))
                    .unwrap_err()
            ),
            "restricted"
        );
        assert!(s.track(json!({"id": B})).is_ok());
    }

    #[test]
    fn safe_search_none_allows_age_restricted() {
        let rid = "rrrrrrrrrrr";
        let http = FakeHttp::new(vec![ok(json!({"items": [restricted_video(rid)]}))]);
        let s = svc(http, with_config(r#"{"safe_search":"none"}"#));
        assert_eq!(
            s.track(json!({"id": rid})).unwrap()["track"]["explicit"],
            true
        );
    }

    #[test]
    fn safe_search_strict_drops_unconfirmed_and_applies_to_cached_rows() {
        // Unrated-in-payload (no contentRating key) cannot be confirmed -> dropped in strict.
        let http = FakeHttp::new(vec![ok(json!({"items": [video(A, "Unconfirmed")]}))]);
        let kv = with_config(r#"{"safe_search":"strict"}"#);
        let s = svc(http.clone(), kv.clone());
        assert_eq!(
            err_msg(s.track(json!({"id": A})).unwrap_err()),
            "restricted"
        );
        // Row was cached unfiltered: relaxing the policy serves it without a new upstream call.
        kv.set("config", br#"{"safe_search":"moderate"}"#, 0)
            .unwrap();
        assert_eq!(s.track(json!({"id": A})).unwrap()["cached"], true);
        assert_eq!(http.count(), 1);
        // And tightening again re-applies strict to the cached row.
        kv.set("config", br#"{"safe_search":"strict"}"#, 0).unwrap();
        assert_eq!(
            err_msg(s.track(json!({"id": A})).unwrap_err()),
            "restricted"
        );
        // A confirmed-rated, non-restricted video passes strict.
        let http2 = FakeHttp::new(vec![ok(json!({"items": [rated_video(B, "Fine")]}))]);
        let s2 = svc(http2, with_config(r#"{"safe_search":"strict"}"#));
        assert!(s2.track(json!({"id": B})).is_ok());
    }

    const CONTRACT: &str = include_str!("../../../contracts/music-tool-contract.v1.json");

    /// Minimal JSON-schema subset check (type/required/properties/items/enum/minimum/$ref).
    fn conforms(v: &Value, schema: &Value, defs: &Value, path: &str) -> Result<(), String> {
        if let Some(r) = schema["$ref"].as_str() {
            let name = r.rsplit('/').next().unwrap();
            return conforms(v, &defs[name], defs, path);
        }
        if let Some(t) = schema.get("type") {
            let types: Vec<&str> = match t {
                Value::String(s) => vec![s.as_str()],
                Value::Array(a) => a.iter().filter_map(Value::as_str).collect(),
                _ => vec![],
            };
            let ok = types.iter().any(|t| match *t {
                "object" => v.is_object(),
                "array" => v.is_array(),
                "string" => v.is_string(),
                "integer" => v.is_i64() || v.is_u64(),
                "boolean" => v.is_boolean(),
                "null" => v.is_null(),
                _ => false,
            });
            if !ok {
                return Err(format!("{path}: expected {types:?}, got {v}"));
            }
        }
        if let Some(e) = schema["enum"].as_array()
            && !e.contains(v)
        {
            return Err(format!("{path}: not in enum"));
        }
        if let (Some(min), Some(n)) = (schema["minimum"].as_i64(), v.as_i64())
            && n < min
        {
            return Err(format!("{path}: below minimum"));
        }
        if let Some(obj) = v.as_object() {
            for r in schema["required"].as_array().into_iter().flatten() {
                let k = r.as_str().unwrap();
                if !obj.contains_key(k) {
                    return Err(format!("{path}: missing required {k}"));
                }
            }
            for (k, sub) in schema["properties"].as_object().into_iter().flatten() {
                if let Some(val) = obj.get(k) {
                    conforms(val, sub, defs, &format!("{path}.{k}"))?;
                }
            }
        }
        if let (Some(arr), Some(items)) = (v.as_array(), schema.get("items")) {
            for (i, it) in arr.iter().enumerate() {
                conforms(it, items, defs, &format!("{path}[{i}]"))?;
            }
        }
        Ok(())
    }

    /// Contract fixture: every tool's real output (and a representative input) conforms to
    /// the shared music-tool-contract schemas the Spotify/SoundCloud bundles also implement.
    #[test]
    fn outputs_conform_to_shared_music_contract() {
        let c: Value = serde_json::from_str(CONTRACT).unwrap();
        let defs = &c["definitions"];
        let tools = c["tools"].as_object().unwrap();
        let mut have: Vec<String> = tools.keys().map(|k| format!("music.youtube.{k}")).collect();
        have.sort();
        let mut want: Vec<String> = TOOLS.iter().map(|t| t.to_string()).collect();
        want.sort();
        assert_eq!(have, want, "tool names must equal the contract's");

        let http = FakeHttp::new(vec![
            ok(json!({"items": [{"id": {"videoId": A}}]})),
            ok(json!({"items": [video(A, "One")]})),
            ok(json!({"items": [{"contentDetails": {"videoId": A}}]})),
        ]);
        let s = svc(http, Arc::new(FakeKv::default()));
        let url = format!("https://youtu.be/{A}");
        let cases = [
            (
                "search",
                json!({"query": "q", "limit": 1}),
                s.search(json!({"query": "q", "limit": 1})),
            ),
            (
                "playlist.get",
                json!({"playlist_id": "PLabcdefghij"}),
                s.playlist_get(json!({"playlist_id": "PLabcdefghij"})),
            ),
            ("track", json!({"id": A}), s.track(json!({"id": A}))),
            (
                "resolve",
                json!({"url": url}),
                s.resolve(json!({"url": url})),
            ),
        ];
        for (name, input, out) in cases {
            let out = match out {
                Ok(v) => v,
                Err(e) => panic!("{name}: {e:?}"),
            };
            let t = &tools[name];
            conforms(&input, &t["input_schema"], defs, "in").unwrap();
            conforms(&out, &t["output_schema"], defs, name).unwrap();
        }
        // The validator itself must be able to fail.
        assert!(
            conforms(
                &json!({"tracks": []}),
                &tools["search"]["output_schema"],
                defs,
                "x"
            )
            .is_err()
        );
    }
}
