//! Production wiring for the bundle `media` host capability
//! (`wit/waddle-bundle/stage.wit` `interface media`, spec
//! `docs/superpowers/specs/2026-11-06-harness-media-generation.md`).
//!
//! [`build_media_wiring`] assembles `bundle_host_media::MediaHost` from the
//! stage's own Valkey connection (job store), the bundle bucket
//! (`MEDIA_BUCKET_*`, SeaweedFS S3), hub-api (effective tier + BYOK, both via a
//! machine JWT) and Gemini (pinned host, BYOK key per job). Every missing
//! dependency leaves the capability UNWIRED with a loud log -- every `media.*`
//! host-call then denies `not_implemented`; nothing falls back to a stub. The
//! whole capability additionally sits behind the default-OFF flag
//! `waddles.harness-media-generation` ([`crate::license::HarnessMediaGenerationGate`]).

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use bundle_host_media::auth::BearerSource;
use bundle_host_media::credentials::HubCredentialResolver;
use bundle_host_media::gemini::{GeminiModels, GeminiProvider};
use bundle_host_media::host::{MediaConfig, MediaHost, SystemClock};
use bundle_host_media::jobs::ValkeyJobStore;
use bundle_host_media::store::ObjectStoreMediaStore;
use bundle_host_media::tier::HubTierResolver;

use crate::config::CliConfig;
use crate::license::{FeatureGate, HarnessMediaGenerationGate};

/// The per-process `media` wiring, cloned into every per-invoke
/// `StageCapabilities`. `None` on the capabilities means `not_implemented`.
#[derive(Clone)]
pub struct MediaWiring {
    /// The orchestration host.
    pub host: Arc<MediaHost>,
    /// `waddles.harness-media-generation` gate -- OFF denies `feature_disabled`
    /// before any provider, key or storage is touched.
    pub flag: Arc<dyn FeatureGate>,
}

/// [`BearerSource`] over the stage's machine-JWT client.
struct MachineBearer(service_auth::MachineJwtClient);

#[async_trait]
impl BearerSource for MachineBearer {
    async fn bearer(&self) -> Result<Arc<str>, String> {
        self.0.get_token().await.map_err(|e| e.to_string())
    }
}

/// Reads a required env var; an unset or empty value is reported by name.
fn required_env(name: &'static str) -> Result<String, &'static str> {
    match std::env::var(name) {
        Ok(v) if !v.trim().is_empty() => Ok(v),
        _ => Err(name),
    }
}

/// Builds the `media` wiring, or `None` (loud error log naming what is missing).
pub fn build_media_wiring(
    cli: &CliConfig,
    license_client: &Arc<penguin_licensing::LicenseClient>,
    valkey: redis::aio::MultiplexedConnection,
) -> Option<MediaWiring> {
    if cli.service_jwt_token_endpoint.is_empty() {
        tracing::error!(
            "media capability: SERVICE_JWT_TOKEN_ENDPOINT unset (hub-api tier + BYOK need a \
             machine JWT) -- capability unavailable (media.* reports not_implemented)"
        );
        return None;
    }
    let (access_key, secret_key) = match (
        required_env("MEDIA_BUCKET_ACCESS_KEY_ID"),
        required_env("MEDIA_BUCKET_SECRET_ACCESS_KEY"),
    ) {
        (Ok(a), Ok(s)) => (a, s),
        (a, s) => {
            tracing::error!(
                missing = ?[a.err(), s.err()],
                "media capability: bundle bucket credentials unset -- capability unavailable \
                 (media.* reports not_implemented)"
            );
            return None;
        }
    };
    let store = match ObjectStoreMediaStore::new(
        &cli.media_bucket_endpoint,
        &cli.media_bucket_name,
        &cli.media_bucket_region,
        &access_key,
        &secret_key,
    ) {
        Ok(s) => s,
        Err(e) => {
            tracing::error!(error = %e, "media capability: bundle bucket misconfigured -- capability unavailable");
            return None;
        }
    };
    let http = match reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(error = %e, "media capability: hub-api HTTP client build failed -- capability unavailable");
            return None;
        }
    };
    let bearer = |scope: &str| -> Arc<dyn BearerSource> {
        Arc::new(MachineBearer(service_auth::MachineJwtClient::new(
            cli.service_jwt_token_endpoint.clone(),
            cli.service_jwt_sa_token_path.clone(),
            scope.to_string(),
        )))
    };
    let tier = HubTierResolver::new(
        http.clone(),
        cli.hub_api_url.clone(),
        bearer("media:tier:read"),
        Duration::from_secs(60),
        Duration::from_secs(900),
    );
    let creds =
        HubCredentialResolver::new(http, cli.hub_api_url.clone(), bearer("media:byok:resolve"));
    let provider = GeminiProvider::production(
        GeminiModels {
            image: cli.media_image_model.clone(),
            music: cli.media_music_model.clone(),
            video: cli.media_video_model.clone(),
        },
        Duration::from_secs(120),
    );
    let host = MediaHost::new(
        Arc::new(tier),
        Arc::new(creds),
        Arc::new(provider),
        Arc::new(store),
        Arc::new(ValkeyJobStore::new(valkey)),
        Arc::new(SystemClock),
        MediaConfig::default(),
    );
    tracing::info!(
        bucket = %cli.media_bucket_name,
        "media capability: configured (flag-gated, default OFF; Professional+ and BYOK enforced host-side)"
    );
    Some(MediaWiring {
        host: Arc::new(host),
        flag: Arc::new(HarnessMediaGenerationGate::new(Arc::clone(license_client))),
    })
}
