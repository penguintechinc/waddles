//! Log-safe renderings of secrets.
//!
//! RTMP stream keys, SRT stream ids, WHIP tokens, and relay-target URLs are
//! bearer credentials: whoever reads one can publish to (or push as) the
//! stream. They must never reach **any** log level, span attribute, error
//! string, or `Debug` rendering (`rules/critical-rules.md` Token & Secret
//! Hygiene -- "Log masked", and Observability -- sanitization applies at
//! every level, DEBUG included). Everything that has to *mention* one goes
//! through this module:
//!
//! - [`fingerprint`] -- a stable, non-reversible correlation id, so an
//!   operator can still tie a rejected publish to the accepted one that
//!   preceded it without the credential itself being in the log;
//! - [`redact_url`] -- keeps a push URL's scheme + host, drops the part that
//!   carries the key;
//! - [`scrub_diagnostic`] -- sanitises a free-form line (ffmpeg stderr)
//!   whose content we do not control.

use std::borrow::Cow;
use std::hash::{Hash, Hasher};

/// Query-parameter / option names whose value is a credential.
const SENSITIVE_PARAMS: [&str; 7] = [
    "streamid",
    "passphrase",
    "token",
    "key",
    "password",
    "secret",
    "signature",
];

/// A short, stable, non-reversible correlation id for `secret`, safe to log
/// in place of it (16 lowercase hex digits).
///
/// Deterministic within a build so the same key correlates across log lines
/// and across protocols (an RTMP/SRT key, a WHIP token), but it is a hash:
/// the raw value cannot be read back out of a log. Not a security boundary
/// for a *guessable* secret -- never use it to store or compare credentials.
pub fn fingerprint(secret: &str) -> String {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    secret.hash(&mut hasher);
    format!("{:016x}", hasher.finish())
}

/// Redacts the sensitive suffix of a push URL for safe logging, dropping any
/// `user:password@` userinfo. Content-driven, not protocol-driven, so a
/// malformed or mismatched-scheme URL still redacts fully:
///
/// - A query string present (SRT-style `?streamid=...`) is always dropped
///   wholesale (`scheme://host[:port]?****`) -- it commonly carries the
///   equivalent of a stream key.
/// - Otherwise, a path present (RTMP-style `/app/<streamkey>`) keeps every
///   segment but the last, which is replaced (`scheme://host/app/****`).
/// - Neither present: `scheme://host/****`.
/// - No `scheme://` at all: redacts wholesale as `****`.
pub fn redact_url(raw: &str) -> String {
    let Some((scheme, rest)) = raw.split_once("://") else {
        return "****".to_string();
    };
    let (before_query, has_query) = match rest.split_once('?') {
        Some((before, _)) => (before, true),
        None => (rest, false),
    };
    let (authority, path) = match before_query.split_once('/') {
        Some((authority, path)) => (authority, Some(path)),
        None => (before_query, None),
    };
    let authority = strip_userinfo(authority);
    if has_query {
        return format!("{scheme}://{authority}?****");
    }
    match path {
        Some(path) if !path.is_empty() => {
            let mut segments: Vec<&str> = path.split('/').collect();
            if let Some(last) = segments.last_mut() {
                *last = "****";
            }
            format!("{scheme}://{authority}/{}", segments.join("/"))
        }
        _ => format!("{scheme}://{authority}/****"),
    }
}

/// Sanitises one line of free-form diagnostic output (ffmpeg's stderr) whose
/// content we do not control and which routinely echoes the resolved secret
/// URLs it was handed (`Error opening output rtmp://host/app/<key>`):
///
/// - any `scheme://...` token collapses to `scheme://host/****` (strict:
///   host only, no path -- a diagnostic has no business carrying one);
/// - `name=value` where `name` is a credential-bearing option
///   (`streamid`, `passphrase`, `token`, `key`, ...) becomes `name=****`;
/// - a `whip-<token>.sdp` file name (the WHIP transcode bridge names its SDP
///   after the session token) becomes `whip-****.sdp`.
///
/// Everything else -- the actual diagnosis -- is left untouched.
pub fn scrub_diagnostic(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut word_start: Option<usize> = None;
    for (idx, ch) in line.char_indices() {
        if is_word_delimiter(ch) {
            if let Some(start) = word_start.take() {
                out.push_str(&scrub_word(&line[start..idx]));
            }
            out.push(ch);
        } else if word_start.is_none() {
            word_start = Some(idx);
        }
    }
    if let Some(start) = word_start {
        out.push_str(&scrub_word(&line[start..]));
    }
    out
}

/// Characters that end a "word" for [`scrub_diagnostic`]: whitespace, quote
/// and bracket characters ffmpeg wraps URLs in, and the `|` that separates
/// tee-muxer slaves (`[f=flv]rtmp://a/b|[f=mpegts]srt://c?streamid=d`).
fn is_word_delimiter(ch: char) -> bool {
    ch.is_whitespace()
        || matches!(
            ch,
            '\'' | '"' | '(' | ')' | '[' | ']' | '<' | '>' | '{' | '}' | '|' | ','
        )
}

/// Scrubs one word (see [`is_word_delimiter`]), preserving any trailing
/// sentence punctuation (`rtmp://host/app/key: Input/output error`).
fn scrub_word(word: &str) -> Cow<'_, str> {
    let core_len = word.trim_end_matches([':', ';', '.', '!']).len();
    let (core, tail) = word.split_at(core_len);

    if core.contains("://") {
        return Cow::Owned(format!("{}{tail}", redact_url_host_only(core)));
    }
    if let Some((name, _value)) = core.split_once('=') {
        if SENSITIVE_PARAMS
            .iter()
            .any(|sensitive| name.eq_ignore_ascii_case(sensitive))
        {
            return Cow::Owned(format!("{name}=****{tail}"));
        }
    }
    if let Some(masked) = mask_whip_sdp_name(core) {
        return Cow::Owned(format!("{masked}{tail}"));
    }
    Cow::Borrowed(word)
}

/// `scheme://host[:port]/****` -- the strictest URL redaction: authority
/// only, userinfo dropped, path and query gone.
fn redact_url_host_only(raw: &str) -> String {
    let Some((scheme, rest)) = raw.split_once("://") else {
        return "****".to_string();
    };
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let authority = strip_userinfo(&rest[..end]);
    format!("{scheme}://{authority}/****")
}

/// Drops a leading `user[:password]@` from a URL authority.
fn strip_userinfo(authority: &str) -> &str {
    authority
        .rsplit_once('@')
        .map_or(authority, |(_, host)| host)
}

/// `whip-<token>.sdp` (optionally behind a directory) -> `whip-****.sdp`.
fn mask_whip_sdp_name(path: &str) -> Option<String> {
    let (dir, file) = path.split_at(path.rfind('/').map_or(0, |i| i + 1));
    let named_after_a_token =
        file.len() > "whip-.sdp".len() && file.starts_with("whip-") && file.ends_with(".sdp");
    named_after_a_token.then(|| format!("{dir}whip-****.sdp"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_never_contains_the_raw_secret_and_is_stable() {
        let raw = "sk_live_super_secret_key";
        let hashed = fingerprint(raw);
        assert_eq!(hashed.len(), 16, "16 hex digits");
        assert!(hashed.chars().all(|c| c.is_ascii_hexdigit()));
        assert!(!hashed.contains(raw));
        assert_eq!(hashed, fingerprint(raw), "deterministic");
        assert_ne!(hashed, fingerprint("a_different_key"));
    }

    #[test]
    fn redact_url_drops_the_key_segment_of_an_rtmp_url() {
        assert_eq!(
            redact_url("rtmp://ingest.example.com/app/sk_supersecret"),
            "rtmp://ingest.example.com/app/****"
        );
    }

    #[test]
    fn redact_url_drops_the_whole_srt_query() {
        assert_eq!(
            redact_url("srt://ingest.example.com:9000?streamid=sk_supersecret&latency=120"),
            "srt://ingest.example.com:9000?****"
        );
    }

    #[test]
    fn redact_url_without_a_scheme_redacts_wholesale() {
        assert_eq!(redact_url("not-a-url"), "****");
    }

    #[test]
    fn redact_url_drops_userinfo() {
        let redacted = redact_url("rtmp://pusher:hunter2@ingest.example.com/app/sk_supersecret");
        assert_eq!(redacted, "rtmp://ingest.example.com/app/****");
        assert!(!redacted.contains("hunter2") && !redacted.contains("pusher"));
    }

    #[test]
    fn scrub_diagnostic_collapses_quoted_urls_to_the_host() {
        let line = "[error] Error opening output 'rtmp://live.example.com/app/sk_live_SECRET': Connection refused";
        let scrubbed = scrub_diagnostic(line);
        assert_eq!(
            scrubbed,
            "[error] Error opening output 'rtmp://live.example.com/****': Connection refused"
        );
    }

    #[test]
    fn scrub_diagnostic_handles_every_tee_slave() {
        let line = "[error] tee: [f=flv:onfail=ignore]rtmp://a.example.com/app/KEY_ONE|[f=mpegts]srt://b.example.com:9000?streamid=KEY_TWO&passphrase=PW failed";
        let scrubbed = scrub_diagnostic(line);
        for secret in ["KEY_ONE", "KEY_TWO", "PW"] {
            assert!(!scrubbed.contains(secret), "{secret} leaked: {scrubbed}");
        }
        assert!(scrubbed.contains("rtmp://a.example.com/****"), "{scrubbed}");
        assert!(
            scrubbed.contains("srt://b.example.com:9000/****"),
            "{scrubbed}"
        );
        assert!(scrubbed.ends_with(" failed"), "{scrubbed}");
    }

    #[test]
    fn scrub_diagnostic_keeps_trailing_punctuation_and_drops_userinfo() {
        let scrubbed = scrub_diagnostic(
            "[error] rtmps://user:p4ss@host.example.com:443/app/SECRET: I/O error",
        );
        assert_eq!(
            scrubbed,
            "[error] rtmps://host.example.com:443/****: I/O error"
        );
    }

    #[test]
    fn scrub_diagnostic_masks_credential_options_outside_urls() {
        let scrubbed = scrub_diagnostic("[error] bad option streamid=SECRET_ID passphrase=PW");
        assert_eq!(scrubbed, "[error] bad option streamid=**** passphrase=****");
    }

    #[test]
    fn scrub_diagnostic_masks_the_token_named_whip_sdp_file() {
        let scrubbed = scrub_diagnostic(
            "[error] /data/stream/whip-sk_live_WHIP_TOKEN.sdp: Invalid data found when processing input",
        );
        assert_eq!(
            scrubbed,
            "[error] /data/stream/whip-****.sdp: Invalid data found when processing input"
        );
    }

    #[test]
    fn scrub_diagnostic_leaves_an_ordinary_diagnosis_untouched() {
        for line in [
            "[error] Conversion failed!",
            "[warning] Non-monotonous DTS in output stream 0:0; previous: 100, current: 90",
            "frame=  100 fps= 30 q=-1.0 size=    1024kB time=00:00:03.33 bitrate=2500.0kbits/s speed=1.00x",
            "",
        ] {
            assert_eq!(scrub_diagnostic(line), line);
        }
    }
}
