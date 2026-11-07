//! Typed client for the `music-<provider>` service bundles' tools.
//!
//! Contract (catalog spec section 3/4; fixtures in `tests/fixtures/`):
//! - `music.<p>.search`        `{query, limit}`        -> `{tracks: [Track]}`
//! - `music.<p>.track`         `{ref}` (id or URL)     -> `{track: Track}`
//! - `music.<p>.playlist.get`  `{ref, limit}`          -> `{tracks: [Track], title?}`
//! - `music.<p>.playback`      `{action:"play", ref}`  -> `{capability: {...}}` (youtube/spotify)
//!
//! Every returned track is untrusted third-party data: it is sanitized and
//! provider-checked before it can enter state, and never interpreted as an
//! instruction.

use serde_json::{Value, json};

use crate::host::{Host, Level, ToolErr};
use crate::model::{PROVIDERS, Track};

/// Service-bundle app id for a provider (`waddles.core.service.music-<p>`).
pub fn target(provider: &str) -> String {
    format!("waddles.core.service.music-{provider}")
}

/// Tool name for a provider operation (`music.<p>.<op>`).
pub fn tool(provider: &str, op: &str) -> String {
    format!("music.{provider}.{op}")
}

/// Failure talking to a music provider bundle.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum MusicError {
    #[error("music provider {0} is not available here")]
    NotGranted(String),
    #[error("music provider {0} is temporarily unavailable")]
    Unavailable(String),
    #[error("music provider {0} returned an invalid response")]
    BadResponse(String),
    #[error("unknown music provider")]
    UnknownProvider,
}

fn map_err(provider: &str, e: ToolErr) -> MusicError {
    match e {
        ToolErr::NotGranted | ToolErr::Forbidden => MusicError::NotGranted(provider.to_string()),
        ToolErr::InvalidArgs(_) => MusicError::BadResponse(provider.to_string()),
        _ => MusicError::Unavailable(provider.to_string()),
    }
}

fn call(host: &dyn Host, provider: &str, op: &str, args: Value) -> Result<Value, MusicError> {
    if !PROVIDERS.contains(&provider) {
        return Err(MusicError::UnknownProvider);
    }
    host.invoke(&target(provider), &tool(provider, op), &args, None)
        .map_err(|e| {
            host.log(
                Level::Warn,
                "music tool failed",
                json!({"provider": provider, "op": op, "error": e.to_string()}),
            );
            map_err(provider, e)
        })
}

fn parse_tracks(host: &dyn Host, provider: &str, v: &Value) -> Result<Vec<Track>, MusicError> {
    let arr = v
        .get("tracks")
        .and_then(Value::as_array)
        .ok_or_else(|| MusicError::BadResponse(provider.to_string()))?;
    let total = arr.len();
    let tracks: Vec<Track> = arr
        .iter()
        .filter_map(|t| serde_json::from_value::<Track>(t.clone()).ok())
        .filter_map(|t| t.sanitized(provider))
        .collect();
    if tracks.len() != total {
        host.log(
            Level::Debug,
            "dropped invalid provider tracks",
            json!({"provider": provider, "dropped": total - tracks.len()}),
        );
    }
    Ok(tracks)
}

/// Searches `provider` for `query` (already cleaned/capped by the caller).
pub fn search(
    host: &dyn Host,
    provider: &str,
    query: &str,
    limit: u32,
) -> Result<Vec<Track>, MusicError> {
    let v = call(
        host,
        provider,
        "search",
        json!({"query": query, "limit": limit}),
    )?;
    parse_tracks(host, provider, &v)
}

/// Resolves one track by provider id or URL.
pub fn resolve(
    host: &dyn Host,
    provider: &str,
    reference: &str,
) -> Result<Option<Track>, MusicError> {
    let v = call(host, provider, "track", json!({"ref": reference}))?;
    let t = v
        .get("track")
        .ok_or_else(|| MusicError::BadResponse(provider.to_string()))?;
    if t.is_null() {
        return Ok(None);
    }
    let track: Track = serde_json::from_value(t.clone())
        .map_err(|_| MusicError::BadResponse(provider.to_string()))?;
    Ok(track.sanitized(provider))
}

/// Fetches the tracks of a provider playlist.
pub fn playlist(
    host: &dyn Host,
    provider: &str,
    reference: &str,
    limit: u32,
) -> Result<Vec<Track>, MusicError> {
    let v = call(
        host,
        provider,
        "playlist.get",
        json!({"ref": reference, "limit": limit}),
    )?;
    parse_tracks(host, provider, &v)
}

/// Best-effort playback handoff (overlay player / Spotify Connect). Providers
/// without a playback tool are skipped; failures are returned so the caller
/// can degrade loudly.
pub fn playback_play(host: &dyn Host, track: &Track) -> Result<(), MusicError> {
    if !matches!(track.provider.as_str(), "youtube" | "spotify") {
        return Ok(());
    }
    call(
        host,
        &track.provider,
        "playback",
        json!({"action": "play", "ref": track.id}),
    )
    .map(|_| ())
}

/// Maps a request URL to `(provider, url)` when the host is a known music site.
/// Rejects userinfo (`user@host`) and non-http(s) schemes.
pub fn provider_for_url(raw: &str) -> Option<&'static str> {
    let rest = raw
        .strip_prefix("https://")
        .or_else(|| raw.strip_prefix("http://"))?;
    let authority = rest.split(['/', '?', '#']).next()?;
    if authority.contains('@') || authority.is_empty() {
        return None;
    }
    let host = authority.split(':').next()?.to_ascii_lowercase();
    let is = |d: &str| host == d || host.ends_with(&format!(".{d}"));
    if is("youtube.com") || host == "youtu.be" {
        Some("youtube")
    } else if host == "open.spotify.com" || host == "spotify.link" {
        Some("spotify")
    } else if is("soundcloud.com") {
        Some("soundcloud")
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_hosts_map_to_providers_and_reject_lookalikes() {
        assert_eq!(
            provider_for_url("https://www.youtube.com/watch?v=abc"),
            Some("youtube")
        );
        assert_eq!(
            provider_for_url("https://music.youtube.com/watch?v=abc"),
            Some("youtube")
        );
        assert_eq!(provider_for_url("https://youtu.be/abc"), Some("youtube"));
        assert_eq!(
            provider_for_url("https://open.spotify.com/track/1"),
            Some("spotify")
        );
        assert_eq!(
            provider_for_url("https://soundcloud.com/a/b"),
            Some("soundcloud")
        );
        assert_eq!(provider_for_url("https://evil-youtube.com/x"), None);
        assert_eq!(provider_for_url("https://youtube.com.evil.io/x"), None);
        assert_eq!(provider_for_url("https://youtube.com@evil.io/x"), None);
        assert_eq!(provider_for_url("ftp://youtube.com/x"), None);
        assert_eq!(provider_for_url("some song name"), None);
    }

    #[test]
    fn targets_and_tools_follow_the_catalog_naming() {
        assert_eq!(target("youtube"), "waddles.core.service.music-youtube");
        assert_eq!(
            tool("spotify", "playlist.get"),
            "music.spotify.playlist.get"
        );
    }
}
