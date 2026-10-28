//! Error taxonomy of the resolver. Variants carry only fixed codes -- never a
//! hub response body, so a secret-looking payload cannot reach a log line.

/// Why hub-api refused to hand out a connection (409 `not_usable` `reason`).
///
/// Non-exhaustive: hub-api may add codes; unknown codes arrive as `Other`.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[non_exhaustive]
pub enum NotUsableReason {
    PendingApproval,
    RevokedGrant,
    Disabled,
    ConnectionExpired,
    ConnectionRevoked,
    MetadataIncomplete,
    TokenExpired,
    RefreshRejected,
    RefreshNotConfigured,
    Other,
}

impl NotUsableReason {
    /// Map hub-api's fixed wire code; anything unrecognised is `Other`.
    pub fn from_wire(code: &str) -> Self {
        match code {
            "pending_approval" => Self::PendingApproval,
            "revoked_grant" => Self::RevokedGrant,
            "disabled" => Self::Disabled,
            "connection_expired" => Self::ConnectionExpired,
            "connection_revoked" => Self::ConnectionRevoked,
            "metadata_incomplete" => Self::MetadataIncomplete,
            "token_expired" => Self::TokenExpired,
            "refresh_rejected" => Self::RefreshRejected,
            "refresh_not_configured" => Self::RefreshNotConfigured,
            _ => Self::Other,
        }
    }
}

/// Every way a resolve/list call can fail.
#[derive(thiserror::Error, Clone, PartialEq, Eq, Debug)]
pub enum ResolveError {
    #[error("connection not found")]
    NotFound,
    #[error("connection not usable: {0:?}")]
    NotUsable(NotUsableReason),
    #[error("hub-api rejected the service credentials")]
    Auth,
    #[error("transient failure: {0}")]
    Transient(&'static str),
    #[error("circuit breaker open")]
    CircuitOpen,
    #[error("malformed response: {0}")]
    Malformed(&'static str),
}

impl ResolveError {
    /// True only for failures worth retrying (`Transient`, `CircuitOpen`);
    /// Auth/NotFound/NotUsable/Malformed are deterministic and never retried.
    pub fn is_transient(&self) -> bool {
        matches!(self, Self::Transient(_) | Self::CircuitOpen)
    }

    /// Fixed, bounded label for metrics/spans.
    pub fn outcome(&self) -> &'static str {
        match self {
            Self::NotFound => "not_found",
            Self::NotUsable(_) => "not_usable",
            Self::Auth => "auth",
            Self::Transient(_) => "transient",
            Self::CircuitOpen => "circuit_open",
            Self::Malformed(_) => "malformed",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reason_mapping_covers_all_nine_codes_and_unknown() {
        let table = [
            ("pending_approval", NotUsableReason::PendingApproval),
            ("revoked_grant", NotUsableReason::RevokedGrant),
            ("disabled", NotUsableReason::Disabled),
            ("connection_expired", NotUsableReason::ConnectionExpired),
            ("connection_revoked", NotUsableReason::ConnectionRevoked),
            ("metadata_incomplete", NotUsableReason::MetadataIncomplete),
            ("token_expired", NotUsableReason::TokenExpired),
            ("refresh_rejected", NotUsableReason::RefreshRejected),
            (
                "refresh_not_configured",
                NotUsableReason::RefreshNotConfigured,
            ),
            ("other", NotUsableReason::Other),
            ("something_new", NotUsableReason::Other),
            ("", NotUsableReason::Other),
        ];
        for (code, want) in table {
            assert_eq!(NotUsableReason::from_wire(code), want, "{code}");
        }
    }

    #[test]
    fn only_transient_and_circuit_open_are_transient() {
        assert!(ResolveError::Transient("x").is_transient());
        assert!(ResolveError::CircuitOpen.is_transient());
        for e in [
            ResolveError::NotFound,
            ResolveError::Auth,
            ResolveError::NotUsable(NotUsableReason::Other),
            ResolveError::Malformed("x"),
        ] {
            assert!(!e.is_transient(), "{e:?}");
        }
    }

    #[test]
    fn outcome_labels_are_fixed() {
        assert_eq!(ResolveError::NotFound.outcome(), "not_found");
        assert_eq!(
            ResolveError::NotUsable(NotUsableReason::Other).outcome(),
            "not_usable"
        );
        assert_eq!(ResolveError::Auth.outcome(), "auth");
        assert_eq!(ResolveError::Transient("x").outcome(), "transient");
        assert_eq!(ResolveError::CircuitOpen.outcome(), "circuit_open");
        assert_eq!(ResolveError::Malformed("x").outcome(), "malformed");
    }
}
