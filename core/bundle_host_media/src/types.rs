//! Shared value types for the `media` capability: the invocation [`Scope`], the
//! [`MediaKind`], opaque [`Handle`]s, the [`JobState`] and the closed typed
//! [`MediaError`] vocabulary every layer (host, wire, WIT) maps onto.

use serde::{Deserialize, Serialize};

/// The only provider in v3.0.
pub const PROVIDER_GEMINI: &str = "gemini";

/// Wire/tier detail carried by `tier-required` when the license state could not
/// be established (fail closed -- never treated as allowed).
pub const LICENSE_UNKNOWN: &str = "license_unknown";

/// What an invocation is scoped to. Always host-derived (verified invoke scope),
/// never a guest value. Numeric ids only (never slug/name).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Scope {
    /// Numeric tenant id.
    pub tenant_id: i64,
    /// Numeric community id.
    pub community_id: i32,
    /// The requesting bundle's app id: the `app-bundle:<id>` tag owner.
    pub app_id: String,
}

/// What to generate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MediaKind {
    /// Still image (synchronous).
    Image,
    /// Music clip (job).
    Music,
    /// Video clip (job).
    Video,
}

impl MediaKind {
    /// Stable lowercase name (metric label, key segment).
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Image => "image",
            Self::Music => "music",
            Self::Video => "video",
        }
    }
}

/// Subscription tier, ordered `Free < Professional < Enterprise`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Tier {
    /// Unmetered baseline.
    Free,
    /// First paid tier.
    Professional,
    /// Highest tier.
    Enterprise,
}

impl Tier {
    /// Parses the hub-api tier slug; anything else is `None` (caller fails closed).
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "free" => Some(Self::Free),
            "professional" => Some(Self::Professional),
            "enterprise" => Some(Self::Enterprise),
            _ => None,
        }
    }
}

/// The closed set of media failures. Never carries provider text, prompt text or
/// key material.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum MediaError {
    /// Effective tier below Professional (`"professional"`) or unknown license
    /// state ([`LICENSE_UNKNOWN`]).
    #[error("tier required: {0}")]
    TierRequired(String),
    /// No BYOK key for this provider.
    #[error("byok key missing for provider {0}")]
    ByokMissing(String),
    /// Provider safety/policy refusal or no output (closed vocabulary).
    #[error("provider refused: {0}")]
    ProviderRefused(String),
    /// Rate / in-flight / provider quota (`minute`, `inflight`, `provider`).
    #[error("quota exceeded: {0}")]
    Quota(String),
    /// Deadline exceeded.
    #[error("timed out")]
    Timeout,
    /// Malformed request.
    #[error("invalid: {0}")]
    Invalid(String),
    /// Dependency down / not provisioned.
    #[error("unavailable: {0}")]
    Unavailable(String),
    /// Unknown job/handle or one belonging to another bundle/community.
    #[error("not found")]
    NotFound,
    /// Unexpected backend failure.
    #[error("backend: {0}")]
    Backend(String),
}

impl MediaError {
    /// Stable wire code (`HostResultError.code`, persisted job failures).
    pub fn code(&self) -> &'static str {
        match self {
            Self::TierRequired(_) => "tier_required",
            Self::ByokMissing(_) => "byok_missing",
            Self::ProviderRefused(_) => "provider_refused",
            Self::Quota(_) => "quota",
            Self::Timeout => "timeout",
            Self::Invalid(_) => "invalid_args",
            Self::Unavailable(_) => "unavailable",
            Self::NotFound => "not_found",
            Self::Backend(_) => "backend",
        }
    }

    /// The wire message (the variant payload, empty when none).
    pub fn detail(&self) -> String {
        match self {
            Self::TierRequired(s)
            | Self::ByokMissing(s)
            | Self::ProviderRefused(s)
            | Self::Quota(s)
            | Self::Invalid(s)
            | Self::Unavailable(s)
            | Self::Backend(s) => s.clone(),
            Self::Timeout | Self::NotFound => String::new(),
        }
    }

    /// Inverse of [`Self::code`]/[`Self::detail`]; an unknown code is `None`.
    pub fn from_code(code: &str, detail: String) -> Option<Self> {
        Some(match code {
            "tier_required" => Self::TierRequired(detail),
            "byok_missing" => Self::ByokMissing(detail),
            "provider_refused" => Self::ProviderRefused(detail),
            "quota" => Self::Quota(detail),
            "timeout" => Self::Timeout,
            "invalid_args" => Self::Invalid(detail),
            "unavailable" => Self::Unavailable(detail),
            "not_found" => Self::NotFound,
            "backend" => Self::Backend(detail),
            _ => return None,
        })
    }
}

/// Where a job stands, as the guest sees it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JobState {
    /// Still running.
    Pending,
    /// Finished; the object [`Handle`] string.
    Done(String),
    /// Failed with a typed error.
    Failed(MediaError),
}

/// Parsed object handle `media://{tenant}/{community}/{job}.{ext}`: opaque to the
/// guest, resolvable only by the owning bundle.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Handle {
    /// Numeric tenant id.
    pub tenant_id: i64,
    /// Numeric community id.
    pub community_id: i32,
    /// 32-hex job id.
    pub job_id: String,
    /// File extension from the sniffed content type.
    pub ext: String,
}

const HANDLE_SCHEME: &str = "media://";

impl Handle {
    /// Renders the handle string handed to the guest.
    pub fn render(&self) -> String {
        format!(
            "{HANDLE_SCHEME}{}/{}/{}.{}",
            self.tenant_id, self.community_id, self.job_id, self.ext
        )
    }

    /// Strictly parses a handle; any deviation is `None`.
    pub fn parse(s: &str) -> Option<Self> {
        let rest = s.strip_prefix(HANDLE_SCHEME)?;
        let mut parts = rest.split('/');
        let tenant_id = parts.next()?.parse::<i64>().ok()?;
        let community_id = parts.next()?.parse::<i32>().ok()?;
        let file = parts.next()?;
        if parts.next().is_some() {
            return None;
        }
        let (job_id, ext) = file.split_once('.')?;
        if !is_job_id(job_id)
            || !matches!(
                ext,
                "png" | "jpg" | "webp" | "mp3" | "wav" | "ogg" | "mp4" | "webm"
            )
        {
            return None;
        }
        let h = Self {
            tenant_id,
            community_id,
            job_id: job_id.to_string(),
            ext: ext.to_string(),
        };
        // Canonical form only: no `+1`, leading zeros, etc.
        (h.render() == s).then_some(h)
    }
}

/// `true` for a 32-char lowercase-hex job id.
pub fn is_job_id(s: &str) -> bool {
    s.len() == 32
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn handle_round_trips_and_rejects_noncanonical_forms() {
        let h = Handle {
            tenant_id: 7,
            community_id: 42,
            job_id: "a".repeat(32),
            ext: "png".into(),
        };
        assert_eq!(Handle::parse(&h.render()), Some(h.clone()));
        for bad in [
            "",
            "media://",
            "media://7/42",
            "media://7/42/x.png",
            "media://+7/42/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
            "media://7/42/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png/x",
            "media://7/42/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.png",
            "media://7/42/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png.png",
        ] {
            assert_eq!(Handle::parse(bad), None, "{bad}");
        }
    }

    #[test]
    fn error_codes_round_trip() {
        for e in [
            MediaError::TierRequired("professional".into()),
            MediaError::ByokMissing("gemini".into()),
            MediaError::ProviderRefused("safety".into()),
            MediaError::Quota("minute".into()),
            MediaError::Timeout,
            MediaError::Invalid("x".into()),
            MediaError::Unavailable("y".into()),
            MediaError::NotFound,
            MediaError::Backend("z".into()),
        ] {
            assert_eq!(MediaError::from_code(e.code(), e.detail()), Some(e.clone()));
            assert!(!e.to_string().is_empty());
        }
        assert_eq!(MediaError::from_code("nope", String::new()), None);
    }

    #[test]
    fn tier_ordering_and_parse() {
        assert!(Tier::Free < Tier::Professional && Tier::Professional < Tier::Enterprise);
        assert_eq!(Tier::parse("professional"), Some(Tier::Professional));
        assert_eq!(Tier::parse("Professional"), None);
        assert_eq!(MediaKind::Video.as_str(), "video");
        assert_eq!(MediaKind::Music.as_str(), "music");
        assert_eq!(MediaKind::Image.as_str(), "image");
    }
}
