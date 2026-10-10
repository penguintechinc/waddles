//! A single relay (RTMP/SRT push) destination: the resolved-secret URL and
//! the redacted form that is safe to log or attach to a Prometheus label.
//!
//! The resolved push URL (`rtmp://host/app/<streamkey>` or
//! `srt://host:port?streamid=...&latency=...`) is exactly the kind of
//! value `rules/client.md` Secrets & Credentials forbids logging -- every
//! type here carries the raw value only behind [`crate::config::Secret`]
//! (whose own `Debug` is already redacted) or a method whose name says
//! `_unredacted`, never a bare field a `{:?}`/`{}` could leak.

use std::fmt;

use crate::config::Secret;
use crate::pipeline::model::OutputSpec;
use crate::store::SecretRef;

use super::error::RelayError;

/// Which push protocol a relay target speaks -- derived from the owning
/// [`OutputSpec::RtmpPush`]/[`OutputSpec::SrtPush`] variant.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RelayTargetKind {
    Rtmp,
    Srt,
}

impl RelayTargetKind {
    /// The ffmpeg muxer name for this push protocol's `-f <name>` output
    /// fragment.
    pub fn ffmpeg_format(&self) -> &'static str {
        match self {
            RelayTargetKind::Rtmp => "flv",
            RelayTargetKind::Srt => "mpegts",
        }
    }

    /// URL schemes this protocol's push URL may use. RTMP additionally
    /// accepts `rtmps://` (RTMP over TLS) -- ffmpeg's `flv` muxer pushes to
    /// either through the same `rtmp` protocol handler, and several
    /// destinations (Facebook Live, many CDNs) only accept TLS ingest.
    fn accepted_schemes(&self) -> &'static [&'static str] {
        match self {
            RelayTargetKind::Rtmp => &["rtmp", "rtmps"],
            RelayTargetKind::Srt => &["srt"],
        }
    }
}

/// One configured relay destination before secret resolution: which
/// protocol plus a pointer at where the real URL lives. Never carries a
/// raw secret.
#[derive(Debug, Clone)]
pub struct RelayTargetSpec {
    pub kind: RelayTargetKind,
    pub url_secret_ref: SecretRef,
}

impl RelayTargetSpec {
    /// Builds a [`RelayTargetSpec`] from an [`OutputSpec`], or `None` if
    /// `spec` is not an [`OutputSpec::RtmpPush`]/[`OutputSpec::SrtPush`]
    /// variant (every other variant belongs to a different egress sink).
    pub fn from_output_spec(spec: &OutputSpec) -> Option<Self> {
        match spec {
            OutputSpec::RtmpPush { url_secret_ref, .. } => Some(Self {
                kind: RelayTargetKind::Rtmp,
                url_secret_ref: url_secret_ref.clone(),
            }),
            OutputSpec::SrtPush { url_secret_ref, .. } => Some(Self {
                kind: RelayTargetKind::Srt,
                url_secret_ref: url_secret_ref.clone(),
            }),
            _ => None,
        }
    }
}

/// A relay destination after its `url_secret_ref` has been resolved and
/// validated. `Debug` only ever prints `url_redacted` -- `url` is a
/// [`Secret`], whose own redacted `Debug` guards it even if a future
/// `#[derive(Debug)]` container forgets to special-case this field.
#[derive(Clone)]
pub struct ResolvedRelayTarget {
    pub kind: RelayTargetKind,
    pub url_redacted: String,
    url: Secret,
}

impl ResolvedRelayTarget {
    /// Validates `raw` against `kind`'s expected scheme/host shape and
    /// wraps it as a [`ResolvedRelayTarget`]. Errors carry only the
    /// redacted form of `raw`, never the raw value itself.
    pub fn from_raw(kind: RelayTargetKind, raw: Secret) -> Result<Self, RelayError> {
        let value = raw.expose();
        let redacted = redact(value);
        if let Err(reason) = validate(kind, value) {
            return Err(RelayError::InvalidUrl { redacted, reason });
        }
        Ok(Self {
            kind,
            url_redacted: redacted,
            url: raw,
        })
    }

    /// Returns the ffmpeg `-f <mux> <url>` output-argument fragments for
    /// this single target. Contains the resolved secret URL -- callers
    /// MUST NOT log the returned strings; only feed them directly to the
    /// ffmpeg command line.
    pub fn ffmpeg_output_args_unredacted(&self) -> Vec<String> {
        vec![
            "-f".to_string(),
            self.kind.ffmpeg_format().to_string(),
            self.url.expose().to_string(),
        ]
    }

    /// The raw resolved URL. Named loudly -- callers MUST NOT log this;
    /// use [`Self::url_redacted`] for any tracing/log/metric-label output.
    pub fn raw_url_unredacted(&self) -> &str {
        self.url.expose()
    }
}

impl fmt::Debug for ResolvedRelayTarget {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ResolvedRelayTarget")
            .field("kind", &self.kind)
            .field("url_redacted", &self.url_redacted)
            .finish()
    }
}

/// One slave entry of an ffmpeg `-f tee` output list, grouping multiple
/// relay targets sharing a single transcode profile
/// (`[f=<mux>:onfail=ignore]<url>|...`, spec §4). `Display`/`Debug` are
/// redacted; [`Self::tee_fragment_unredacted`] is the only accessor that
/// returns the real fragment, and its name says so.
#[derive(Clone)]
pub struct TeeSlave {
    pub format: &'static str,
    pub url_redacted: String,
    url: Secret,
}

impl TeeSlave {
    fn from_target(target: &ResolvedRelayTarget) -> Self {
        Self {
            format: target.kind.ffmpeg_format(),
            url_redacted: target.url_redacted.clone(),
            url: Secret::new(target.raw_url_unredacted().to_string()),
        }
    }

    /// The literal `-f tee` slave fragment (`[f=<mux>:onfail=ignore]<url>`)
    /// used verbatim when assembling the real ffmpeg command line. Contains
    /// the resolved secret URL -- callers MUST NOT log this; use
    /// `Display`/`Debug` (both redacted) for any tracing/log output.
    pub fn tee_fragment_unredacted(&self) -> String {
        format!("[f={}:onfail=ignore]{}", self.format, self.url.expose())
    }
}

impl fmt::Debug for TeeSlave {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("TeeSlave")
            .field("format", &self.format)
            .field("url_redacted", &self.url_redacted)
            .finish()
    }
}

impl fmt::Display for TeeSlave {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[f={}:onfail=ignore]{}", self.format, self.url_redacted)
    }
}

/// Builds the tee-slave list for a set of resolved targets sharing one
/// profile, in the same order the targets were started -- the order
/// `observe_stderr_line`'s `Output #<N>` index parsing relies on.
pub fn tee_slaves(targets: &[ResolvedRelayTarget]) -> Vec<TeeSlave> {
    targets.iter().map(TeeSlave::from_target).collect()
}

/// Redacts the sensitive suffix of a relay push URL for safe logging.
/// Content-driven, not `kind`-driven, so a malformed/mismatched-scheme URL
/// (e.g. an SRT-shaped value passed where an RTMP url was expected -- see
/// [`validate`]'s scheme check) still redacts fully instead of falling
/// through a kind-specific branch that doesn't match its actual shape:
///
/// - A query string present (SRT-style `?streamid=...`) is always dropped
///   wholesale (`scheme://host[:port]?****`) -- it commonly carries the
///   equivalent of a stream key.
/// - Otherwise, a path present (RTMP-style `/app/<streamkey>`) keeps every
///   segment but the last, which is replaced (`scheme://host/app/****`).
/// - Neither present: `scheme://host/****`.
/// - No `scheme://` at all: redacts wholesale as `****`.
fn redact(raw: &str) -> String {
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

/// Validates `raw` has the expected `scheme://host[:port]` shape for
/// `kind`. Returns a human-readable reason on failure; callers must pair
/// this with [`redact`] before surfacing the value in an error.
fn validate(kind: RelayTargetKind, raw: &str) -> Result<(), String> {
    let Some((scheme, rest)) = raw.split_once("://") else {
        return Err("missing scheme (expected \"scheme://host...\")".to_string());
    };
    let accepted = kind.accepted_schemes();
    if !accepted.iter().any(|s| scheme.eq_ignore_ascii_case(s)) {
        let expected = accepted
            .iter()
            .map(|s| format!("{s}://"))
            .collect::<Vec<_>>()
            .join(" or ");
        return Err(format!("expected {expected} scheme, got {scheme}://"));
    }
    let authority = rest.split(['/', '?']).next().unwrap_or("");
    let host = authority.split(':').next().unwrap_or("");
    if host.is_empty() {
        return Err("missing host".to_string());
    }
    if kind == RelayTargetKind::Srt {
        let port_ok = authority
            .rsplit_once(':')
            .map(|(_, port)| !port.is_empty() && port.chars().all(|c| c.is_ascii_digit()))
            .unwrap_or(false);
        if !port_ok {
            return Err("srt target must include a numeric port (host:port)".to_string());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn from_output_spec_maps_rtmp_push() {
        let spec = OutputSpec::RtmpPush {
            url_secret_ref: SecretRef::Env {
                var: "RELAY_URL".into(),
            },
            profile: None,
        };
        let target = RelayTargetSpec::from_output_spec(&spec).expect("rtmp push maps");
        assert_eq!(target.kind, RelayTargetKind::Rtmp);
    }

    #[test]
    fn from_output_spec_maps_srt_push() {
        let spec = OutputSpec::SrtPush {
            url_secret_ref: SecretRef::Env {
                var: "RELAY_URL".into(),
            },
            profile: None,
        };
        let target = RelayTargetSpec::from_output_spec(&spec).expect("srt push maps");
        assert_eq!(target.kind, RelayTargetKind::Srt);
    }

    #[test]
    fn from_output_spec_rejects_non_relay_variants() {
        let spec = OutputSpec::Whep {
            profile: "1080p60".into(),
        };
        assert!(RelayTargetSpec::from_output_spec(&spec).is_none());
    }

    #[test]
    fn rtmp_url_redacts_the_stream_key_segment() {
        let redacted = redact("rtmp://ingest.example.com/app/sk_supersecret");
        assert_eq!(redacted, "rtmp://ingest.example.com/app/****");
    }

    #[test]
    fn srt_url_redacts_the_whole_query_string() {
        let redacted = redact("srt://ingest.example.com:9000?streamid=sk_supersecret&latency=120");
        assert_eq!(redacted, "srt://ingest.example.com:9000?****");
    }

    #[test]
    fn malformed_url_redacts_wholesale() {
        assert_eq!(redact("not-a-url"), "****");
    }

    #[test]
    fn scheme_mismatched_url_still_fully_redacts_its_query_string() {
        // A value shaped like an SRT url but redacted while validating
        // against the RTMP scheme (the `resolved_target_rejects_wrong_scheme`
        // integration case below) must not leak through a kind-specific
        // branch that assumes RTMP's path shape.
        let redacted = redact("srt://ingest.example.com:9000?streamid=sk_supersecret");
        assert_eq!(redacted, "srt://ingest.example.com:9000?****");
    }

    #[test]
    fn resolved_target_debug_never_contains_the_raw_secret() {
        let target = ResolvedRelayTarget::from_raw(
            RelayTargetKind::Rtmp,
            Secret::new("rtmp://ingest.example.com/app/sk_supersecret".to_string()),
        )
        .expect("valid rtmp url");
        let rendered = format!("{target:?}");
        assert!(!rendered.contains("sk_supersecret"));
        assert!(rendered.contains("****"));
    }

    #[test]
    fn resolved_target_rejects_wrong_scheme() {
        let err = ResolvedRelayTarget::from_raw(
            RelayTargetKind::Rtmp,
            Secret::new("srt://ingest.example.com:9000?streamid=x".to_string()),
        )
        .unwrap_err();
        let rendered = err.to_string();
        assert!(rendered.contains("expected rtmp:// or rtmps:// scheme"));
        assert!(!rendered.contains("streamid=x"));
    }

    #[test]
    fn rtmp_kind_accepts_rtmps_urls_case_insensitively() {
        for raw in [
            "rtmps://live-api-s.facebook.com:443/rtmp/FB-key",
            "RTMPS://ingest.example.com/app/sk_abc",
            "rtmp://ingest.example.com/app/sk_abc",
        ] {
            let target =
                ResolvedRelayTarget::from_raw(RelayTargetKind::Rtmp, Secret::new(raw.to_string()))
                    .unwrap_or_else(|err| panic!("{raw} should be accepted: {err}"));
            assert_eq!(target.kind, RelayTargetKind::Rtmp);
            // Whatever the scheme, the stream key never reaches the redacted form.
            assert!(!target.url_redacted.contains("sk_abc"));
            assert!(!target.url_redacted.contains("FB-key"));
        }
    }

    #[test]
    fn rtmps_redacts_the_stream_key_like_rtmp() {
        assert_eq!(
            redact("rtmps://live-api-s.facebook.com:443/rtmp/FB-key"),
            "rtmps://live-api-s.facebook.com:443/rtmp/****"
        );
    }

    #[test]
    fn rtmps_is_not_accepted_for_srt_and_srt_is_not_accepted_for_rtmp() {
        let err = ResolvedRelayTarget::from_raw(
            RelayTargetKind::Srt,
            Secret::new("rtmps://ingest.example.com/app/k".to_string()),
        )
        .unwrap_err();
        assert!(err.to_string().contains("expected srt:// scheme"));
        assert!(ResolvedRelayTarget::from_raw(
            RelayTargetKind::Rtmp,
            Secret::new("srt://ingest.example.com:9000?streamid=x".to_string()),
        )
        .is_err());
        assert!(ResolvedRelayTarget::from_raw(
            RelayTargetKind::Rtmp,
            Secret::new("http://ingest.example.com/app/k".to_string()),
        )
        .is_err());
    }

    #[test]
    fn rtmps_target_feeds_ffmpeg_the_flv_muxer() {
        let target = ResolvedRelayTarget::from_raw(
            RelayTargetKind::Rtmp,
            Secret::new("rtmps://ingest.example.com/app/sk_abc".to_string()),
        )
        .expect("valid rtmps url");
        assert_eq!(
            target.ffmpeg_output_args_unredacted(),
            vec![
                "-f".to_string(),
                "flv".to_string(),
                "rtmps://ingest.example.com/app/sk_abc".to_string(),
            ]
        );
    }

    #[test]
    fn resolved_target_rejects_srt_without_port() {
        let err = ResolvedRelayTarget::from_raw(
            RelayTargetKind::Srt,
            Secret::new("srt://ingest.example.com?streamid=x".to_string()),
        )
        .unwrap_err();
        assert!(err.to_string().contains("numeric port"));
    }

    #[test]
    fn resolved_target_accepts_valid_srt_url() {
        let target = ResolvedRelayTarget::from_raw(
            RelayTargetKind::Srt,
            Secret::new("srt://ingest.example.com:9000?streamid=sk_abc&latency=120".to_string()),
        )
        .expect("valid srt url");
        assert_eq!(target.url_redacted, "srt://ingest.example.com:9000?****");
    }

    #[test]
    fn ffmpeg_output_args_unredacted_carries_the_real_url() {
        let target = ResolvedRelayTarget::from_raw(
            RelayTargetKind::Rtmp,
            Secret::new("rtmp://ingest.example.com/app/sk_abc".to_string()),
        )
        .expect("valid rtmp url");
        let args = target.ffmpeg_output_args_unredacted();
        assert_eq!(
            args,
            vec![
                "-f".to_string(),
                "flv".to_string(),
                "rtmp://ingest.example.com/app/sk_abc".to_string(),
            ]
        );
    }

    #[test]
    fn tee_slave_display_and_debug_are_redacted() {
        let target = ResolvedRelayTarget::from_raw(
            RelayTargetKind::Srt,
            Secret::new("srt://ingest.example.com:9000?streamid=sk_abc".to_string()),
        )
        .expect("valid srt url");
        let slaves = tee_slaves(std::slice::from_ref(&target));
        let slave = &slaves[0];
        let displayed = format!("{slave}");
        let debugged = format!("{slave:?}");
        assert!(!displayed.contains("sk_abc"));
        assert!(!debugged.contains("sk_abc"));
        assert_eq!(
            displayed,
            "[f=mpegts:onfail=ignore]srt://ingest.example.com:9000?****"
        );
    }

    #[test]
    fn tee_fragment_unredacted_carries_the_real_url() {
        let target = ResolvedRelayTarget::from_raw(
            RelayTargetKind::Rtmp,
            Secret::new("rtmp://ingest.example.com/app/sk_abc".to_string()),
        )
        .expect("valid rtmp url");
        let slaves = tee_slaves(std::slice::from_ref(&target));
        let fragment = slaves[0].tee_fragment_unredacted();
        assert_eq!(
            fragment,
            "[f=flv:onfail=ignore]rtmp://ingest.example.com/app/sk_abc"
        );
    }

    #[test]
    fn tee_slaves_preserves_target_order() {
        let targets = vec![
            ResolvedRelayTarget::from_raw(
                RelayTargetKind::Rtmp,
                Secret::new("rtmp://a.example.com/app/k1".to_string()),
            )
            .unwrap(),
            ResolvedRelayTarget::from_raw(
                RelayTargetKind::Srt,
                Secret::new("srt://b.example.com:9000?streamid=k2".to_string()),
            )
            .unwrap(),
        ];
        let slaves = tee_slaves(&targets);
        assert_eq!(slaves[0].format, "flv");
        assert_eq!(slaves[1].format, "mpegts");
    }
}
