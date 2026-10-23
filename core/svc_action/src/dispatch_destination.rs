//! State-ownership router for the action dispatcher (edge-reshape Chunk 1a).
//!
//! Decides *where* an action goes; never sends anything. Chunk 1a has one
//! destination (the stateful platform reply); later chunks add more.

use std::fmt;

use crate::capabilities::{relay_op_supported, RelayOp};

/// Where a routed action is delivered.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Destination {
    /// Stateful: goes back to the platform edge that owns the interaction.
    StatefulPlatformReply { provider: String, op: RelayOp },
}

/// Routing failure. Carries provider + op only -- never bundle/user text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DispatchError {
    /// No destination exists for this `(provider, op)` pair.
    NoDestination { provider: String, op: RelayOp },
}

impl fmt::Display for DispatchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoDestination { provider, op } => {
                write!(
                    f,
                    "no destination for provider={provider} op={}",
                    op.as_str()
                )
            }
        }
    }
}

impl std::error::Error for DispatchError {}

/// Routes `(provider, op)` to a [`Destination`], delegating the allow
/// decision to `relay_op_supported` so the two can never drift. Pure.
pub fn route_destination(provider: &str, op: RelayOp) -> Result<Destination, DispatchError> {
    if relay_op_supported(provider, op) {
        Ok(Destination::StatefulPlatformReply {
            provider: provider.to_string(),
            op,
        })
    } else {
        Err(DispatchError::NoDestination {
            provider: provider.to_string(),
            op,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capabilities::RELAY_PROVIDERS;

    const ALL_OPS: [RelayOp; 3] = [RelayOp::ChatSend, RelayOp::ChatDelete, RelayOp::DmSend];

    #[test]
    fn routes_supported_relay_ops_to_stateful_reply() {
        let cases = [
            ("twitch", RelayOp::ChatSend),
            ("discord", RelayOp::ChatSend),
            ("discord", RelayOp::ChatDelete),
            ("discord", RelayOp::DmSend),
        ];
        for (provider, op) in cases {
            assert_eq!(
                route_destination(provider, op),
                Ok(Destination::StatefulPlatformReply {
                    provider: provider.to_string(),
                    op
                })
            );
        }
    }

    #[test]
    fn unsupported_pair_is_no_destination_error() {
        for (provider, op) in [("twitch", RelayOp::DmSend), ("slack", RelayOp::ChatSend)] {
            assert_eq!(
                route_destination(provider, op),
                Err(DispatchError::NoDestination {
                    provider: provider.to_string(),
                    op
                })
            );
        }
    }

    #[test]
    fn route_matches_relay_op_supported_allowlist() {
        for provider in RELAY_PROVIDERS {
            for op in ALL_OPS {
                assert_eq!(
                    route_destination(provider, op).is_ok(),
                    relay_op_supported(provider, op),
                    "{provider} {}",
                    op.as_str()
                );
            }
        }
    }

    #[test]
    fn no_destination_display_has_no_payload() {
        let e = route_destination("twitch", RelayOp::DmSend).unwrap_err();
        assert_eq!(
            e.to_string(),
            "no destination for provider=twitch op=dm.send"
        );
    }
}
