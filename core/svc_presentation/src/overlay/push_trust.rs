//! Concrete [`overlay_auth::PushTrustSource`] backed directly by
//! `service_auth::JwksTrustBundle` -- the trust source
//! `overlay_auth::require_push_credential` needs to verify an inbound
//! PUSH credential (a hub-api-issued machine JWT). No new crypto or JWKS
//! fetch/cache logic here: `service_auth` already owns both, this is only
//! the thin `PushTrustSource` adapter `overlay_auth`'s module doc
//! describes ("typically backed by `service_auth::JwksTrustBundle`
//! pointed at hub-api's JWKS endpoint").

use overlay_auth::PushTrustSource;
use service_auth::{JwksTrustBundle, TrustBundle};

use crate::config::Config;

/// Wraps a [`JwksTrustBundle`] plus the expected audience/issuer this
/// service requires on every PUSH credential.
pub struct AppPushTrustSource {
    trust_bundle: JwksTrustBundle,
    audience: String,
    trusted_issuer: String,
}

impl AppPushTrustSource {
    /// Builds the trust source from this service's loaded [`Config`] --
    /// `PUSH_JWKS_ENDPOINT`/`PUSH_AUDIENCE`/`PUSH_TRUSTED_ISSUER`.
    ///
    /// Honours `SERVICE_JWT_ACCEPT_LEGACY_EDDSA` (default off = ES256 only), parsed
    /// strictly by clap in [`Config`].
    pub fn from_config(config: &Config) -> Self {
        Self {
            trust_bundle: JwksTrustBundle::new(config.cli.push_jwks_endpoint.clone())
                .with_legacy_eddsa(config.cli.service_jwt_accept_legacy_eddsa),
            audience: config.cli.push_audience.clone(),
            trusted_issuer: config.cli.push_trusted_issuer.clone(),
        }
    }
}

impl PushTrustSource for AppPushTrustSource {
    fn trust_bundle(&self) -> &dyn TrustBundle {
        &self.trust_bundle
    }

    fn expected_audience(&self) -> &str {
        &self.audience
    }

    fn trusted_issuers(&self) -> Vec<&str> {
        vec![self.trusted_issuer.as_str()]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Secret};
    use clap::Parser;

    fn test_config() -> Config {
        let cli = CliConfig::parse_from([
            "svc-presentation",
            "--push-jwks-endpoint",
            "https://hub-api.internal:50204",
            "--push-audience",
            "waddlebot-internal",
            "--push-trusted-issuer",
            "hub-api",
        ]);
        Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
            image_bucket_access_key_id: None,
            image_bucket_secret_access_key: None,
        }
    }

    #[test]
    fn built_from_config_exposes_the_configured_audience_and_issuer() {
        let source = AppPushTrustSource::from_config(&test_config());
        assert_eq!(source.expected_audience(), "waddlebot-internal");
        assert_eq!(source.trusted_issuers(), vec!["hub-api"]);
        assert!(
            !source.trust_bundle().accept_legacy_eddsa(),
            "ES256-only by default"
        );
    }

    #[test]
    fn legacy_eddsa_flag_is_wired_and_garbage_is_refused() {
        let mut config = test_config();
        config.cli.service_jwt_accept_legacy_eddsa = true;
        assert!(AppPushTrustSource::from_config(&config)
            .trust_bundle()
            .accept_legacy_eddsa());
        let bad = CliConfig::try_parse_from([
            "svc-presentation",
            "--service-jwt-accept-legacy-eddsa",
            "maybe",
        ]);
        assert!(bad.is_err(), "a non-boolean flag value must fail startup");
    }
}
