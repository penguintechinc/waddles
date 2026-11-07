//! Pure YouTube parsing helpers: URL -> video id, ISO-8601 durations, thumbnail
//! choice, percent-encoding, cache-key hashing and `videos.list` -> track mapping.
//!
//! Nothing here touches the host; every function is deterministic and unit-tested.
//! Anything malformed fails LOUD with a stable code (never a guessed default).

use serde_json::{Value, json};

/// Hosts a resolvable URL may point at (exact match; no wildcard, no userinfo, no port).
const ALLOWED_HOSTS: [&str; 5] = [
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
];

/// Highest quality first; `snippet.thumbnails` only guarantees `default`.
const THUMBNAIL_PRIORITY: [&str; 5] = ["maxres", "standard", "high", "medium", "default"];

/// Why a URL could not be turned into a video id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UrlError {
    /// Not an http(s) URL on an allowed YouTube host.
    NotYoutube,
    /// A playlist URL with no video id; the caller should use `playlist.get`.
    PlaylistOnly,
    /// A YouTube URL that carries no (valid) video id.
    NoVideoId,
}

/// `true` for an 11-char YouTube video id (`[A-Za-z0-9_-]`).
pub fn is_video_id(s: &str) -> bool {
    s.len() == 11 && s.bytes().all(is_id_byte)
}

/// `true` for a playlist id (`[A-Za-z0-9_-]`, 10..=64 chars).
pub fn is_playlist_id(s: &str) -> bool {
    (10..=64).contains(&s.len()) && s.bytes().all(is_id_byte)
}

/// `true` for an opaque page token (`[A-Za-z0-9_-]`, 1..=200 chars).
pub fn is_page_token(s: &str) -> bool {
    (1..=200).contains(&s.len()) && s.bytes().all(is_id_byte)
}

fn is_id_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_' || b == b'-'
}

/// Extracts the video id from any supported youtube.com / music.youtube.com / youtu.be URL.
pub fn extract_video_id(url: &str) -> Result<String, UrlError> {
    let rest = strip_scheme(url).ok_or(UrlError::NotYoutube)?;
    let auth_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let host = rest[..auth_end].to_ascii_lowercase();
    if !ALLOWED_HOSTS.contains(&host.as_str()) {
        return Err(UrlError::NotYoutube);
    }
    let after = &rest[auth_end..];
    let after = after.split('#').next().unwrap_or("");
    let (path, query) = match after.split_once('?') {
        Some((p, q)) => (p, q),
        None => (after, ""),
    };

    let candidate: Option<&str> = if host == "youtu.be" {
        path.trim_start_matches('/').split('/').next()
    } else if let Some(v) = query_param(query, "v") {
        Some(v)
    } else {
        ["/embed/", "/shorts/", "/live/", "/v/"]
            .iter()
            .find_map(|p| path.strip_prefix(p))
            .and_then(|r| r.split('/').next())
    };

    match candidate {
        Some(id) if is_video_id(id) => Ok(id.to_string()),
        Some(_) => Err(UrlError::NoVideoId),
        None if query_param(query, "list").is_some() => Err(UrlError::PlaylistOnly),
        None => Err(UrlError::NoVideoId),
    }
}

fn strip_scheme(url: &str) -> Option<&str> {
    let lower_ok = |p: &str| url.len() >= p.len() && url[..p.len()].eq_ignore_ascii_case(p);
    if lower_ok("https://") {
        Some(&url[8..])
    } else if lower_ok("http://") {
        Some(&url[7..])
    } else {
        None
    }
}

fn query_param<'a>(query: &'a str, key: &str) -> Option<&'a str> {
    query
        .split('&')
        .filter_map(|kv| kv.split_once('='))
        .find(|(k, _)| *k == key)
        .map(|(_, v)| v)
}

/// Parses `contentDetails.duration` (`PT4M13S`, `P1DT2H`, `P0D`) into milliseconds.
/// `None` on any malformed or overflowing value.
pub fn parse_iso8601_duration_ms(d: &str) -> Option<u64> {
    let rest = d.strip_prefix('P')?;
    let (date_part, time_part) = match rest.split_once('T') {
        Some((a, b)) => (a, Some(b)),
        None => (rest, None),
    };
    let mut secs: u64 = 0;
    if !date_part.is_empty() {
        let days = date_part.strip_suffix('D')?;
        secs = secs.checked_add(parse_digits(days)?.checked_mul(86_400)?)?;
    }
    if let Some(t) = time_part {
        if t.is_empty() {
            return None;
        }
        let mut num = String::new();
        let mut last_rank = 0u8;
        for c in t.chars() {
            if c.is_ascii_digit() {
                num.push(c);
                continue;
            }
            let (rank, mult) = match c {
                'H' => (1, 3_600),
                'M' => (2, 60),
                'S' => (3, 1),
                _ => return None,
            };
            if rank <= last_rank {
                return None;
            }
            last_rank = rank;
            secs = secs.checked_add(parse_digits(&num)?.checked_mul(mult)?)?;
            num.clear();
        }
        if !num.is_empty() {
            return None;
        }
    }
    secs.checked_mul(1000)
}

fn parse_digits(s: &str) -> Option<u64> {
    if s.is_empty() || s.len() > 12 || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    s.parse().ok()
}

/// Highest-resolution thumbnail URL, `None` when the map is empty.
pub fn best_thumbnail(thumbnails: &Value) -> Option<String> {
    THUMBNAIL_PRIORITY.iter().find_map(|k| {
        thumbnails
            .get(*k)
            .and_then(|t| t.get("url"))
            .and_then(Value::as_str)
            .filter(|u| u.starts_with("https://"))
            .map(str::to_string)
    })
}

/// Percent-encodes `s` for a query value (RFC 3986 unreserved pass through).
pub fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// FNV-1a 64 as 16 hex chars -- a cache-key shortener only (entries store the
/// original query and verify it on read, so a collision can never serve a wrong row).
pub fn fnv_hex(s: &str) -> String {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in s.bytes() {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{h:016x}")
}

/// Standard (assignable) `videoCategories` ids -> lowercase name (legacy parity).
fn category_name(id: &str) -> Option<&'static str> {
    Some(match id {
        "1" => "film & animation",
        "2" => "autos & vehicles",
        "10" => "music",
        "15" => "pets & animals",
        "17" => "sports",
        "18" => "short movies",
        "19" => "travel & events",
        "20" => "gaming",
        "21" => "videoblogging",
        "22" => "people & blogs",
        "23" | "34" => "comedy",
        "24" => "entertainment",
        "25" => "news & politics",
        "26" => "howto & style",
        "27" => "education",
        "28" => "science & technology",
        "29" => "nonprofits & activism",
        "30" => "movies",
        "31" => "anime/animation",
        "32" => "action/adventure",
        "33" => "classics",
        "35" => "documentary",
        "36" => "drama",
        "37" => "family",
        "38" => "foreign",
        "39" => "horror",
        "40" => "sci-fi/fantasy",
        "41" => "thriller",
        "42" => "shorts",
        "43" => "shows",
        "44" => "trailers",
        _ => return None,
    })
}

/// Lowercase de-duplicated labels: category + tags + topic leaf names (legacy parity).
fn labels(item: &Value) -> Vec<String> {
    let snippet = &item["snippet"];
    let mut raw: Vec<String> = Vec::new();
    if let Some(cid) = snippet["categoryId"].as_str().filter(|c| !c.is_empty()) {
        raw.push(
            category_name(cid)
                .map(str::to_string)
                .unwrap_or_else(|| format!("category:{cid}")),
        );
    }
    if let Some(tags) = snippet["tags"].as_array() {
        raw.extend(tags.iter().filter_map(Value::as_str).map(str::to_string));
    }
    if let Some(topics) = item["topicDetails"]["topicCategories"].as_array() {
        for t in topics.iter().filter_map(Value::as_str) {
            if let Some(leaf) = t.rsplit('/').next().filter(|l| !l.is_empty()) {
                raw.push(leaf.replace('_', " "));
            }
        }
    }
    let mut seen: Vec<String> = Vec::new();
    for l in raw {
        let l = l.trim().to_lowercase();
        if !l.is_empty() && !seen.contains(&l) && seen.len() < 40 {
            seen.push(l);
        }
    }
    seen
}

/// Maps one `videos.list` item to the common track shape. `Err` is a stable code.
///
/// `explicit` is `true` only when YouTube flags the video age-restricted
/// (`contentRating.ytRating == "ytAgeRestricted"`); YouTube exposes no explicit
/// lyrics flag, so everything else is `null` (unknown) -- never a guessed `false`.
pub fn track_from_item(item: &Value) -> Result<Value, &'static str> {
    let id = item["id"]
        .as_str()
        .filter(|i| is_video_id(i))
        .ok_or("upstream_malformed")?;
    let snippet = &item["snippet"];
    let title = snippet["title"].as_str().ok_or("upstream_malformed")?;
    let channel = snippet["channelTitle"].as_str().unwrap_or("");
    let artist = channel.strip_suffix(" - Topic").unwrap_or(channel);
    let dur = item["contentDetails"]["duration"].as_str().unwrap_or("");
    let duration_ms = if dur.is_empty() {
        0
    } else {
        parse_iso8601_duration_ms(dur).ok_or("upstream_malformed")?
    };
    let live = snippet["liveBroadcastContent"]
        .as_str()
        .is_some_and(|s| s != "none");
    let explicit = (item["contentDetails"]["contentRating"]["ytRating"].as_str()
        == Some("ytAgeRestricted"))
    .then_some(true);
    Ok(json!({
        "provider": "youtube",
        "id": id,
        "title": title,
        "artist": artist,
        "duration_ms": duration_ms,
        "thumbnail": best_thumbnail(&snippet["thumbnails"]),
        "explicit": explicit,
        "source_url": format!("https://www.youtube.com/watch?v={id}"),
        "channel_id": snippet["channelId"].as_str(),
        "embeddable": item["status"]["embeddable"].as_bool(),
        "live": live,
        "labels": labels(item),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_every_supported_url_shape() {
        let id = "dQw4w9WgXcQ";
        for u in [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "http://youtube.com/watch?feature=x&v=dQw4w9WgXcQ&t=3",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabcdefghij",
            "https://youtu.be/dQw4w9WgXcQ?si=zz",
            "HTTPS://M.YOUTUBE.COM/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ/",
            "https://www.youtube.com/live/dQw4w9WgXcQ#frag",
        ] {
            assert_eq!(extract_video_id(u).as_deref(), Ok(id), "{u}");
        }
    }

    #[test]
    fn rejects_foreign_hosts_and_tricks() {
        for u in [
            "https://evil.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
            "https://user@youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com:8443/watch?v=dQw4w9WgXcQ",
            "https://sub.youtube.com/watch?v=dQw4w9WgXcQ",
            "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
            "dQw4w9WgXcQ",
            "",
        ] {
            assert_eq!(extract_video_id(u), Err(UrlError::NotYoutube), "{u}");
        }
    }

    #[test]
    fn invalid_ids_and_playlist_only_fail_loud() {
        assert_eq!(
            extract_video_id("https://youtube.com/watch?v=short"),
            Err(UrlError::NoVideoId)
        );
        assert_eq!(
            extract_video_id("https://youtube.com/watch?v=dQw4w9WgXc%51"),
            Err(UrlError::NoVideoId)
        );
        assert_eq!(
            extract_video_id("https://www.youtube.com/playlist?list=PLabcdefghij"),
            Err(UrlError::PlaylistOnly)
        );
        assert_eq!(
            extract_video_id("https://www.youtube.com/"),
            Err(UrlError::NoVideoId)
        );
    }

    #[test]
    fn durations() {
        assert_eq!(parse_iso8601_duration_ms("PT4M13S"), Some(253_000));
        assert_eq!(parse_iso8601_duration_ms("PT1H"), Some(3_600_000));
        assert_eq!(parse_iso8601_duration_ms("P1DT2H3M4S"), Some(93_784_000));
        assert_eq!(parse_iso8601_duration_ms("P0D"), Some(0));
        assert_eq!(parse_iso8601_duration_ms("PT0S"), Some(0));
        for bad in [
            "",
            "PT",
            "4M",
            "PT4X",
            "PT1S2M",
            "PT99999999999999S",
            "PTM",
            "P1H",
        ] {
            assert_eq!(parse_iso8601_duration_ms(bad), None, "{bad}");
        }
    }

    #[test]
    fn encode_and_hash() {
        assert_eq!(percent_encode("a b&c=é"), "a%20b%26c%3D%C3%A9");
        assert_eq!(percent_encode("AZaz09-_.~"), "AZaz09-_.~");
        assert_eq!(fnv_hex("a"), fnv_hex("a"));
        assert_ne!(fnv_hex("a"), fnv_hex("b"));
        assert_eq!(fnv_hex("").len(), 16);
    }

    #[test]
    fn id_validators() {
        assert!(is_video_id("dQw4w9WgXcQ"));
        assert!(!is_video_id("dQw4w9WgXc"));
        assert!(!is_video_id("dQw4w9WgXc!"));
        assert!(is_playlist_id("PLabcdefghij"));
        assert!(!is_playlist_id("short"));
        assert!(is_page_token("CAoQAA"));
        assert!(!is_page_token(""));
        assert!(!is_page_token("a b"));
    }

    fn item() -> Value {
        json!({
            "id": "dQw4w9WgXcQ",
            "snippet": {
                "title": "Song",
                "channelTitle": "Artist - Topic",
                "channelId": "UC123",
                "categoryId": "10",
                "tags": ["Pop", " pop ", "Dance"],
                "liveBroadcastContent": "none",
                "thumbnails": {"default": {"url": "https://i.ytimg.com/d.jpg"},
                               "high": {"url": "https://i.ytimg.com/h.jpg"}}
            },
            "contentDetails": {"duration": "PT3M5S", "contentRating": {"ytRating": "ytAgeRestricted"}},
            "status": {"embeddable": true},
            "topicDetails": {"topicCategories": ["https://en.wikipedia.org/wiki/Pop_music"]}
        })
    }

    #[test]
    fn maps_item_to_common_track() {
        let t = track_from_item(&item()).unwrap();
        assert_eq!(t["artist"], "Artist");
        assert_eq!(t["duration_ms"], 185_000);
        assert_eq!(t["thumbnail"], "https://i.ytimg.com/h.jpg");
        assert_eq!(t["explicit"], true);
        assert_eq!(t["embeddable"], true);
        assert_eq!(t["live"], false);
        assert_eq!(
            t["source_url"],
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        );
        assert_eq!(t["labels"], json!(["music", "pop", "dance", "pop music"]));
    }

    #[test]
    fn unknown_explicit_is_null_and_malformed_fails() {
        let mut i = item();
        i["contentDetails"]["contentRating"] = json!({});
        assert!(track_from_item(&i).unwrap()["explicit"].is_null());
        let mut bad = item();
        bad["contentDetails"]["duration"] = json!("garbage");
        assert_eq!(track_from_item(&bad), Err("upstream_malformed"));
        let mut noid = item();
        noid["id"] = json!("x");
        assert_eq!(track_from_item(&noid), Err("upstream_malformed"));
        let mut notitle = item();
        notitle["snippet"].as_object_mut().unwrap().remove("title");
        assert_eq!(track_from_item(&notitle), Err("upstream_malformed"));
    }

    #[test]
    fn non_https_thumbnail_ignored() {
        let t = json!({"maxres": {"url": "http://x/y.jpg"}, "default": {"url": "https://x/d.jpg"}});
        assert_eq!(best_thumbnail(&t).as_deref(), Some("https://x/d.jpg"));
        assert_eq!(best_thumbnail(&json!({})), None);
    }
}
