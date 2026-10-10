//! URL redaction for error text and log lines on the bundle `http` egress
//! path.
//!
//! `reqwest::Error`'s `Display`/`Debug` embed the full request URL, and
//! many platform APIs carry a credential in the URL itself -- most
//! importantly a Discord webhook (`/api/webhooks/{id}/{token}`), where the
//! token is a path segment, not a query parameter. Dropping only the
//! query/fragment therefore still leaks that token into every error
//! message, DLQ detail, and bundle log that renders the error. This module
//! reduces any URL to `scheme://host[:port]` plus a **default-deny** path:
//! only a small closed set of fixed API-structure words survives, every
//! other segment becomes [`REDACTED_SEGMENT`], and everything after a
//! `webhooks` segment is masked outright (id, token, and any suffix).

use reqwest::Url;

/// Placeholder substituted for every redacted path segment.
pub const REDACTED_SEGMENT: &str = "***";

/// Path segments that are fixed API-structure words, never credentials.
/// Default-deny: any segment not on this list (ids, tokens, bot
/// credentials glued to a prefix, arbitrary resource names) is masked, so
/// an unfamiliar URL shape fails closed instead of leaking.
const STRUCTURAL_SEGMENTS: &[&str] = &[
    "api",
    "applications",
    "channels",
    "commands",
    "gateway",
    "guilds",
    "interactions",
    "messages",
    "oauth2",
    "users",
    "webhooks",
];

/// A segment after which every remaining segment is masked, structural
/// word or not -- Discord's `/webhooks/{id}/{token}[/slack|/github|...]`
/// puts the bearer token two segments past this marker.
const MASK_REST_AFTER: &str = "webhooks";

/// Reports whether `segment` is an API version marker like `v10`.
fn is_version_segment(segment: &str) -> bool {
    segment.strip_prefix('v').is_some_and(|digits| {
        (1..=3).contains(&digits.len()) && digits.bytes().all(|b| b.is_ascii_digit())
    })
}

/// Redacts a URL path (which starts with `/` for any http(s) URL),
/// keeping only allowlisted structural segments and masking the rest.
fn redact_path(path: &str) -> String {
    let rest = path.strip_prefix('/').unwrap_or(path);
    let mut mask_rest = false;
    let mut out = String::with_capacity(path.len());
    for segment in rest.split('/') {
        out.push('/');
        if segment.is_empty() {
            continue;
        }
        let keep = !mask_rest
            && (is_version_segment(segment)
                || STRUCTURAL_SEGMENTS
                    .iter()
                    .any(|word| segment.eq_ignore_ascii_case(word)));
        if keep {
            out.push_str(segment);
            if segment.eq_ignore_ascii_case(MASK_REST_AFTER) {
                mask_rest = true;
            }
        } else {
            out.push_str(REDACTED_SEGMENT);
        }
    }
    out
}

/// Returns a copy of `url` that is safe to log: userinfo, query and
/// fragment dropped, path redacted per [`redact_path`]. `None` for a URL
/// with no host or an opaque (cannot-be-a-base) path -- there is no
/// segment structure to redact, so the caller must drop the URL entirely
/// rather than echo it.
pub fn redact_url(url: &Url) -> Option<Url> {
    if url.cannot_be_a_base() || !url.has_host() {
        return None;
    }
    let mut redacted = url.clone();
    redacted.set_query(None);
    redacted.set_fragment(None);
    // `set_username`/`set_password` only fail for host-less or opaque
    // URLs, both rejected above; a failure here still drops the URL.
    redacted.set_username("").ok()?;
    redacted.set_password(None).ok()?;
    let path = redact_path(redacted.path());
    redacted.set_path(&path);
    Some(redacted)
}

/// Renders a raw URL string for a log line or error message: host plus
/// redacted path (see [`redact_url`]). An unparseable or host-less input
/// renders as a fixed placeholder -- the raw text is never echoed, since a
/// string that fails to parse may still contain a credential.
pub fn redact_url_for_log(raw: &str) -> String {
    match Url::parse(raw).ok().as_ref().and_then(redact_url) {
        Some(url) => url.to_string(),
        None => "<redacted url>".to_string(),
    }
}

/// Rewrites the URL embedded in a `reqwest::Error` to its redacted form
/// (or strips it when it cannot be redacted), so neither `Display` nor
/// `Debug` of the returned error carries a path-borne credential.
pub fn scrub_error_url(err: reqwest::Error) -> reqwest::Error {
    match err.url().map(redact_url) {
        None => err,
        Some(Some(redacted)) => err.with_url(redacted),
        Some(None) => err.without_url(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::egress::{HttpTransport, ReqwestTransport, TransportRequest};
    use std::net::SocketAddr;
    use std::time::Duration;

    const WEBHOOK_ID: &str = "1234567890123456789";
    const WEBHOOK_TOKEN: &str = "Zk3Xw9_tOkEn-s3cr3t-DoNotLeak-Zk3Xw9";

    #[test]
    fn discord_webhook_id_and_token_are_masked() {
        let raw = format!("https://discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}");
        assert_eq!(
            redact_url_for_log(&raw),
            "https://discord.com/api/webhooks/***/***"
        );
    }

    #[test]
    fn webhook_variants_mask_everything_after_the_marker() {
        for (raw, want) in [
            (
                format!("https://discord.com/api/v10/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}/slack"),
                "https://discord.com/api/v10/webhooks/***/***/***",
            ),
            (
                format!(
                    "https://discordapp.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}/messages/55"
                ),
                "https://discordapp.com/api/webhooks/***/***/***/***",
            ),
            (
                format!("https://discord.com/api/webhooks/{WEBHOOK_ID}"),
                "https://discord.com/api/webhooks/***",
            ),
        ] {
            let got = redact_url_for_log(&raw);
            assert_eq!(got, want, "for {raw}");
            assert!(!got.contains(WEBHOOK_TOKEN));
        }
    }

    #[test]
    fn query_fragment_and_userinfo_are_dropped() {
        let raw = format!(
            "https://user:hunter2@discord.com:8443/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}?wait=true&key=sekret#frag"
        );
        let got = redact_url_for_log(&raw);
        assert_eq!(got, "https://discord.com:8443/api/webhooks/***/***");
        for needle in ["user", "hunter2", "wait", "sekret", "frag", WEBHOOK_TOKEN] {
            assert!(!got.contains(needle), "{needle} leaked in {got}");
        }
    }

    #[test]
    fn unfamiliar_path_shapes_fail_closed() {
        for (raw, want) in [
            ("https://example.com/secrettoken", "https://example.com/***"),
            (
                "https://api.telegram.org/bot123456:AAH-secret/sendMessage",
                "https://api.telegram.org/***/***",
            ),
            (
                "https://hooks.slack.com/services/T000/B000/XXXXsecretXXXX",
                "https://hooks.slack.com/***/***/***/***",
            ),
            (
                "https://discord.com/api/v10/channels/123456789012345678/messages",
                "https://discord.com/api/v10/channels/***/messages",
            ),
            ("https://example.com/", "https://example.com/"),
            ("https://example.com", "https://example.com/"),
        ] {
            assert_eq!(redact_url_for_log(raw), want, "for {raw}");
        }
    }

    #[test]
    fn unparseable_or_hostless_input_is_never_echoed() {
        for raw in [
            "not a url with a token abc123",
            "mailto:someone@example.com",
            "data:text/plain,secret",
            "file:///etc/passwd",
        ] {
            assert_eq!(redact_url_for_log(raw), "<redacted url>", "for {raw}");
        }
    }

    /// regression: a Discord webhook token, carried in the URL *path*, must
    /// never appear in the `HostResultError` a real transport failure
    /// produces -- the pre-fix transport rendered `reqwest::Error`'s full
    /// URL, query-stripped or not. Exercises the real `ReqwestTransport`
    /// against a refused local port (no mocked transport).
    #[tokio::test]
    async fn transport_error_never_carries_the_webhook_token() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr: SocketAddr = listener.local_addr().unwrap();
        drop(listener);

        let err = ReqwestTransport::new()
            .send(
                TransportRequest {
                    method: "POST".to_string(),
                    url: format!(
                        "http://localhost:{}/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}?wait=true",
                        addr.port()
                    ),
                    pinned_addr: addr,
                    headers: vec![],
                    body: None,
                },
                Duration::from_secs(5),
                1024,
            )
            .await
            .expect_err("nothing is listening, so the send must fail");

        assert_eq!(err.code, "transport");
        for needle in [WEBHOOK_TOKEN, WEBHOOK_ID, "wait=true"] {
            assert!(
                !err.message.contains(needle),
                "{needle} leaked in {:?}",
                err.message
            );
        }
        assert!(
            err.message.contains("/api/webhooks/***/***"),
            "expected host + redacted path, got {:?}",
            err.message
        );
    }

    /// The `Debug` rendering (what `{:?}`/`tracing` field capture would
    /// print) must be scrubbed too, not just `Display`.
    #[tokio::test]
    async fn scrubbed_reqwest_error_debug_and_display_carry_no_token() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let raw = reqwest::Client::new()
            .post(format!(
                "http://{addr}/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}?wait=true#frag"
            ))
            .send()
            .await
            .unwrap_err();
        // Premise: an unscrubbed reqwest error DOES leak the path token.
        assert!(raw.to_string().contains(WEBHOOK_TOKEN));

        let scrubbed = scrub_error_url(raw);
        for text in [scrubbed.to_string(), format!("{scrubbed:?}")] {
            for needle in [WEBHOOK_TOKEN, WEBHOOK_ID, "wait=true", "frag"] {
                assert!(!text.contains(needle), "{needle} leaked in {text}");
            }
        }
    }

    /// A URL-less error stays URL-less, and an error carrying a URL that
    /// cannot be redacted (host-less / opaque) has it stripped outright
    /// rather than echoed.
    #[test]
    fn scrub_error_url_handles_url_less_and_unredactable_urls() {
        let url_less = reqwest::Proxy::all("").unwrap_err();
        assert!(url_less.url().is_none());
        assert!(scrub_error_url(url_less).url().is_none());

        let opaque = reqwest::Proxy::all("")
            .unwrap_err()
            .with_url(Url::parse("mailto:someone@example.com").unwrap());
        assert!(opaque.to_string().contains("someone@example.com"));
        let scrubbed = scrub_error_url(opaque);
        assert!(scrubbed.url().is_none());
        assert!(!scrubbed.to_string().contains("someone@example.com"));
    }
}
