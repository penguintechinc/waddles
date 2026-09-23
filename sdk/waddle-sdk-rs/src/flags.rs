//! Idiomatic wrapper over the WIT `%flags` interface (PostHog flag +
//! license entitlement, two-gate, cached, fail-open to the supplied
//! default; always granted; `wit/waddle-bundle/stage.wit` `interface
//! %flags`, wire name `waddle:bundle/flags@1.0.0`).

use std::fmt;
use std::str::FromStr;

/// License tier, per `rules/critical-rules.md` Feature Flags & License
/// Tiers. Parsed from the WIT `tier()` import's raw string.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tier {
    Free,
    Professional,
    Enterprise,
}

impl fmt::Display for Tier {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Tier::Free => "free",
            Tier::Professional => "professional",
            Tier::Enterprise => "enterprise",
        })
    }
}

/// An unrecognized tier string. Parsing fails closed (an error, not a
/// silent default) so a caller notices a host/SDK version skew rather than
/// mis-gating a feature; [`tier()`] itself still fails open to `Free`
/// (see its doc comment) because that is the WIT contract's own default.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("unrecognized license tier: {0}")]
pub struct UnknownTier(pub String);

impl FromStr for Tier {
    type Err = UnknownTier;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "free" => Ok(Tier::Free),
            "professional" => Ok(Tier::Professional),
            "enterprise" => Ok(Tier::Enterprise),
            other => Err(UnknownTier(other.to_string())),
        }
    }
}

/// Resolves the PostHog flag `key`, falling back to `default_value` if the
/// flag/license server is unreachable or the flag has never been seen
/// (fail-open, per `rules/critical-rules.md`).
///
/// Only compiles for `wasm32` targets -- see `crate::bindings_glue`'s
/// module doc comment for the resulting host coverage carve-out.
#[cfg(target_arch = "wasm32")]
pub fn enabled(key: &str, default_value: bool) -> bool {
    crate::bindings_glue::flags_enabled(key, default_value)
}

/// The tenant's current license tier. Falls open to [`Tier::Free`] if the
/// host reports a string this SDK version does not recognize, so a bundle
/// never panics on a host/SDK skew.
#[cfg(target_arch = "wasm32")]
pub fn tier() -> Tier {
    crate::bindings_glue::flags_tier()
        .parse()
        .unwrap_or(Tier::Free)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_every_known_tier() {
        assert_eq!("free".parse::<Tier>(), Ok(Tier::Free));
        assert_eq!("professional".parse::<Tier>(), Ok(Tier::Professional));
        assert_eq!("enterprise".parse::<Tier>(), Ok(Tier::Enterprise));
    }

    #[test]
    fn rejects_unknown_tier_string() {
        let err = "gold".parse::<Tier>().unwrap_err();
        assert_eq!(err, UnknownTier("gold".to_string()));
    }

    #[test]
    fn display_round_trips_through_from_str() {
        for tier in [Tier::Free, Tier::Professional, Tier::Enterprise] {
            let s = tier.to_string();
            assert_eq!(s.parse::<Tier>(), Ok(tier));
        }
    }
}
