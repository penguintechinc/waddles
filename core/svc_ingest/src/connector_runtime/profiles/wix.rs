//! The Wix [`ConnectorProfile`]: a verified-webhook (inbound-only) connection.
//!
//! One hub-api Wix binding (the app PUBLIC key as the connection secret plus the
//! site's `instance_id` in metadata) becomes one [`TransportPlan::Webhook`]. The
//! route key is the host derivation (`webhook_boot`), the scheme is
//! [`Scheme::WixJwt`] bound to that instance id, and the secret is resolved per
//! request by `secret_ref` -- never from the request. Nothing from the binding
//! except the validated instance id reaches the plan, and a hostile value is
//! rejected with a fixed-text [`PlanError`], never echoed.

use std::collections::{hash_map::DefaultHasher, BTreeMap};
use std::hash::{Hash, Hasher};
use std::time::Duration;

use connection_resolver::{BindingSummary, ConnectionKind, ConnectionRef};

use super::super::feed::{ConnKey, DesiredConnection, TransportPlan, WebhookPlan};
use super::super::plan::{ConnectorProfile, PlanError, RuntimeEnv};
use super::super::webhook_boot::{RouteKeyDeriver, REPLAY_WINDOW};
use crate::connector::webhook::{RateLimit, WebhookRoute, HARD_MAX_BODY_BYTES};
use crate::connector::webhook_verify::{Scheme, WixSpec};

/// Platform slug (also the `ConnKey` platform and the route owner).
pub const PLATFORM: &str = "wix";
/// Core connector bundle id.
pub const APP_ID: &str = "waddles.core.service.wix";
/// Provider name in hub-api's binding list.
pub const PROVIDER: &str = "wix";
/// Capability the binding list is scoped to.
pub const CAPABILITY: &str = "webhook_ingest";
/// Binding metadata key holding the Wix app instance (site) id.
pub const META_INSTANCE_ID: &str = "instance_id";

/// Wix instance ids are UUIDs; accept a conservative superset.
const INSTANCE_LEN: std::ops::RangeInclusive<usize> = 8..=64;

/// The Wix webhook connector profile.
#[derive(Debug, Clone)]
pub struct WixProfile {
    deriver: Option<RouteKeyDeriver>,
}

impl WixProfile {
    /// Builds the profile over the route-key deriver (`None` when no webhook
    /// platform is configured; every plan then fails loud).
    #[must_use]
    pub fn new(deriver: Option<RouteKeyDeriver>) -> Self {
        Self { deriver }
    }
}

/// True for `[A-Za-z0-9-]{8,64}`: no whitespace, CR/LF or separators.
fn is_instance_id(s: &str) -> bool {
    INSTANCE_LEN.contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

fn instance_id(m: &BTreeMap<String, String>) -> Result<&str, PlanError> {
    let raw = m
        .get(META_INSTANCE_ID)
        .ok_or(PlanError::MissingKey(META_INSTANCE_ID))?;
    if is_instance_id(raw) {
        Ok(raw.as_str())
    } else {
        Err(PlanError::InvalidValue(META_INSTANCE_ID))
    }
}

impl ConnectorProfile for WixProfile {
    fn platform(&self) -> &'static str {
        PLATFORM
    }

    fn app_id(&self) -> &'static str {
        APP_ID
    }

    fn provider(&self) -> &'static str {
        PROVIDER
    }

    fn capability(&self) -> &'static str {
        CAPABILITY
    }

    fn plan(
        &self,
        b: &BindingSummary,
        _env: &RuntimeEnv,
    ) -> Result<Vec<DesiredConnection>, PlanError> {
        if b.provider != PROVIDER {
            return Err(PlanError::WrongProvider);
        }
        if b.kind != ConnectionKind::PlatformOauth {
            return Err(PlanError::WrongKind);
        }
        let tenant = Some(b.tenant_id)
            .filter(|t| *t >= 1)
            .ok_or(PlanError::Config("binding tenant is not positive"))?;
        let version = Some(b.secret_version)
            .filter(|v| *v >= 1)
            .ok_or(PlanError::Config("binding secret version is not positive"))?;
        let instance = instance_id(&b.metadata)?;
        let deriver = self.deriver.as_ref().ok_or(PlanError::Config(
            "wix webhook route keys are not configured",
        ))?;
        let route_key = deriver
            .derive(PLATFORM, b.connection_id)
            .map_err(|_| PlanError::Config("wix route key derivation failed"))?;
        let secret_ref = ConnectionRef {
            community_id: b.community_id,
            connection_id: b.connection_id,
        };
        let route = WebhookRoute {
            route_key,
            owner_bundle: PLATFORM.to_owned(),
            scheme: Scheme::WixJwt(WixSpec {
                instance_id: instance.to_owned(),
            }),
            secret_ref,
            replay_window: REPLAY_WINDOW,
            rate_limit: RateLimit {
                max: 300,
                per: Duration::from_secs(60),
            },
            body_limit: HARD_MAX_BODY_BYTES,
        };
        let mut h = DefaultHasher::new();
        route.route_key.hash(&mut h);
        instance.hash(&mut h);
        tenant.hash(&mut h);
        version.hash(&mut h);
        let fingerprint = h.finish();
        Ok(vec![DesiredConnection {
            key: ConnKey {
                platform: PLATFORM,
                community_id: b.community_id,
                connection_id: b.connection_id,
                lane: Default::default(),
            },
            tenant_id: tenant,
            plan: TransportPlan::Webhook(Box::new(WebhookPlan {
                route,
                authorized_community: b.community_id,
            })),
            fingerprint,
        }])
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;
    use connection_resolver::SecretString;

    const INST: &str = "0b9e1a52-4c3d-4e6f-8a7b-1234567890ab";

    fn env() -> RuntimeEnv {
        RuntimeEnv {
            environment: "alpha".into(),
            max_frame_bytes: 65_536,
            egress_allow: vec![],
            heartbeat_enabled: false,
        }
    }

    fn deriver() -> RouteKeyDeriver {
        RouteKeyDeriver::new(SecretString::new("k".repeat(40))).unwrap()
    }

    fn binding(instance: Option<&str>) -> BindingSummary {
        let mut metadata = BTreeMap::new();
        if let Some(i) = instance {
            metadata.insert(META_INSTANCE_ID.to_string(), i.to_string());
        }
        BindingSummary {
            tenant_id: 1,
            secret_version: 1,
            community_id: 7,
            connection_id: 11,
            provider: "wix".into(),
            kind: ConnectionKind::PlatformOauth,
            metadata,
        }
    }

    fn profile() -> WixProfile {
        WixProfile::new(Some(deriver()))
    }

    #[test]
    fn plans_one_webhook_route_bound_to_the_instance() {
        let mut v = profile().plan(&binding(Some(INST)), &env()).unwrap();
        assert_eq!(v.len(), 1);
        let d = v.remove(0);
        assert_eq!(
            (d.key.platform, d.key.connection_id, d.tenant_id),
            ("wix", 11, 1)
        );
        let TransportPlan::Webhook(w) = d.plan else {
            panic!("wix must plan a webhook transport");
        };
        assert_eq!(w.authorized_community, 7);
        assert_eq!(w.route.owner_bundle, "wix");
        assert_eq!(w.route.route_key, deriver().derive("wix", 11).unwrap());
        assert_eq!(w.route.secret_ref.connection_id, 11);
        assert!(matches!(&w.route.scheme, Scheme::WixJwt(s) if s.instance_id == INST));
        w.route.validate().unwrap();
    }

    #[test]
    fn fingerprint_tracks_instance_tenant_and_rotation() {
        let fp = |b: &BindingSummary| profile().plan(b, &env()).unwrap()[0].fingerprint;
        let base = fp(&binding(Some(INST)));
        assert_eq!(base, fp(&binding(Some(INST))));
        assert_ne!(base, fp(&binding(Some("another-instance-id"))));
        let mut rotated = binding(Some(INST));
        rotated.secret_version = 2;
        assert_ne!(base, fp(&rotated));
        let mut moved = binding(Some(INST));
        moved.tenant_id = 2;
        assert_ne!(base, fp(&moved));
    }

    #[test]
    fn hostile_or_missing_bindings_are_rejected_with_fixed_errors() {
        let p = profile();
        assert_eq!(
            p.plan(&binding(None), &env()).err(),
            Some(PlanError::MissingKey(META_INSTANCE_ID))
        );
        for bad in [
            "",
            "short",
            "has space in it",
            "id\n12345678",
            "id/../12345678",
            &"a".repeat(65),
        ] {
            assert_eq!(
                p.plan(&binding(Some(bad)), &env()).err(),
                Some(PlanError::InvalidValue(META_INSTANCE_ID)),
                "{bad:?}"
            );
        }
        let mut b = binding(Some(INST));
        b.provider = "discord".into();
        assert_eq!(p.plan(&b, &env()).err(), Some(PlanError::WrongProvider));
        let mut b = binding(Some(INST));
        b.kind = ConnectionKind::ExternalHttp;
        assert_eq!(p.plan(&b, &env()).err(), Some(PlanError::WrongKind));
        let mut b = binding(Some(INST));
        b.tenant_id = 0;
        assert!(matches!(p.plan(&b, &env()), Err(PlanError::Config(_))));
        let mut b = binding(Some(INST));
        b.secret_version = 0;
        assert!(matches!(p.plan(&b, &env()), Err(PlanError::Config(_))));
    }

    #[test]
    fn without_a_deriver_every_plan_fails_loud() {
        assert!(matches!(
            WixProfile::new(None).plan(&binding(Some(INST)), &env()),
            Err(PlanError::Config(_))
        ));
    }

    #[test]
    fn constants_match_the_registered_connector() {
        let p = profile();
        assert_eq!(
            (p.platform(), p.app_id(), p.provider(), p.capability()),
            ("wix", "waddles.core.service.wix", "wix", "webhook_ingest")
        );
    }
}
