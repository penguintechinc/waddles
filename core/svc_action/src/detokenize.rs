//! Wires `egress-detokenizer` (spec S10.4/S10.6, Gemini condition 5) into
//! this stage's chat egress -- see [`capabilities::StageCapabilities::
//! handle_relay`]/`handle_discord_relay`'s detokenize-then-sanitize call
//! sites for where [`ChatDetokenizer::render`] actually runs.

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use egress_detokenizer::{CacheConfig, Detokenizer, NameResolver, ResolveError};
use uuid::Uuid;

/// Type-erased so [`crate::capabilities::StageCapabilities`] doesn't need a
/// second generic type parameter just to hold this -- any resolver
/// (production or test double) is boxed behind `Arc<dyn NameResolver>`,
/// which itself satisfies `NameResolver` via `egress_detokenizer`'s blanket
/// `impl<T: NameResolver + ?Sized> NameResolver for Arc<T>`.
pub type SharedResolver = Arc<dyn NameResolver>;

/// This stage's chat-egress detokenizer: one per process, shared across
/// every host-API connection's [`crate::capabilities::StageCapabilities`]
/// (a per-tenant name cache is only useful if it's actually shared across
/// invokes, spec S10.4's "one batched lookup, not one query per mention").
pub type ChatDetokenizer = Detokenizer<SharedResolver>;

/// **TODO(M3+ seam, matching this crate's existing `db`/`kv`/`flags`
/// pattern** -- `capabilities.rs`'s module doc): `svc_action` has no
/// existing client for hub-api's `hub_users` mapping (spec S10.1/S10.4's
/// PII-boundary identity store) as of this landing; wiring one is a
/// separate piece of work (a new hub-api batched-lookup endpoint plus a
/// SPIFFE/JWT-authenticated client here, per `rules/security.md`
/// Service-to-Service Auth) rather than something to improvise inline with
/// this detokenizer landing.
///
/// Deliberately **fail-safe-empty**, never a fabricated name: every lookup
/// resolves to "unknown", so every render always chooses the neutral label
/// ([`egress_detokenizer::NEUTRAL_LABEL`]) until the real client lands --
/// the same posture `handle`'s `db`/`kv`/`flags` match arms already take
/// for their own not-yet-wired capabilities. This is intentionally NOT a
/// silent PII leak risk: rendering "a former viewer" for every mention is
/// safe-by-construction, whereas a resolver that guessed or echoed
/// anything back would not be.
pub struct HubUsersResolver;

#[async_trait]
impl NameResolver for HubUsersResolver {
    async fn resolve_batch(
        &self,
        _tenant: &str,
        _users: &[Uuid],
    ) -> Result<HashMap<Uuid, String>, ResolveError> {
        Ok(HashMap::new())
    }
}

/// Builds the production [`ChatDetokenizer`], backed by [`HubUsersResolver`]
/// and the spec's default 5-minute TTL ([`CacheConfig::default`]).
pub fn build_production_detokenizer() -> Arc<ChatDetokenizer> {
    Arc::new(Detokenizer::new(
        Arc::new(HubUsersResolver) as SharedResolver,
        CacheConfig::default(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn hub_users_resolver_always_resolves_empty() {
        let resolver = HubUsersResolver;
        let out = resolver
            .resolve_batch(
                "t1",
                &[Uuid::parse_str("11111111-1111-4111-8111-111111111111").unwrap()],
            )
            .await
            .unwrap();
        assert!(out.is_empty());
    }

    #[tokio::test]
    async fn production_detokenizer_renders_the_neutral_label_for_every_mention() {
        let detokenizer = build_production_detokenizer();
        let user = "11111111-1111-4111-8111-111111111111";
        let out = detokenizer
            .render(
                "t1",
                egress_detokenizer::Sink::ChatTwitch,
                &format!("hi {{user:{user}}}"),
            )
            .await;
        assert_eq!(out, format!("hi {}", egress_detokenizer::NEUTRAL_LABEL));
    }
}
