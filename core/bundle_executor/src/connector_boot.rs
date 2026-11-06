//! Startup pinning of the platform connector service bundles and the request handler
//! for the dedicated connector link (connector runtime spec
//! `docs/superpowers/specs/2026-11-05-connector-runtime-inbound-manager.md`
//! S2.1-S2.4).
//!
//! # Boot pinning
//!
//! For each platform whose `CONNECTOR_PIN_DIGEST_<P>=sha256:<64hex>` is set,
//! [`pin_configured`] fetches `connectors/<platform>/<digest>.wasm` and its
//! signed sidecar `connectors/<platform>/<digest>.json` through the
//! executor's own [`crate::invoke::ComponentSource`] (the signed bucket),
//! verifies the sidecar's Ed25519 signature against
//! `BUNDLE_SIGNING_PUBLIC_KEYS` (an empty key set is REFUSED here -- there is
//! no "verification off" for a connector), checks `sha256(bytes) == digest`,
//! and pins the component under the host-constant `app_id`
//! (`waddles.core.service.<p>`), the `connector-tools` world and a grant set
//! that is a subset of the connector's own `hub-manifest.yaml` permissions
//! (never broader -- see [`ConnectorPlatform::grants`]).
//!
//! ANY failure aborts startup loudly, naming the platform; never a skipped or
//! degraded pin. Error text carries the platform and failure class only --
//! never a credential or a URL.
//!
//! # The connector link
//!
//! [`ConnectorLinkHandler`] is the `RequestHandler` for the second dial loop:
//! it serves connector exports only (a `load`/`unload`/stage export is
//! refused) and routes each connector call's host-calls back over the link
//! with the host-derived `_scope` stamp. Its `on_disconnect` is a no-op: a
//! dropped connector link must never wipe the stage link's bundle registry.

use std::collections::HashSet;
use std::sync::Arc;

use penguin_bundle_host::wire::{
    ErrorBody, ErrorCode, InvokeBody, LoadBody, LoadedBody, ResultBody, ShutdownBody, UnloadBody,
    UnloadedBody,
};
use tracing::{info, warn};

use crate::config::{parse_pin_digest, CliConfig};
use crate::connector::ConnectorLimits;
use crate::error::ExecutorError;
use crate::invoke::{ComponentSource, Executor};
use crate::manifest::{VerifiedManifest, WitWorld, PERM_CONNECTOR_PII_READ};
use crate::signing::SignedSidecar;
use crate::wire::{Connection, RequestHandler};

/// One platform connector the executor can pin: its host-constant identity and
/// the exact grants it is pinned with.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ConnectorPlatform {
    /// `twitch` | `discord` | `youtube` -- the bucket path segment.
    pub platform: &'static str,
    /// The host-constant core `app_id` (`waddles.core.service.<p>`); never
    /// read from a bucket object or a wire frame.
    pub app_id: &'static str,
    /// The permissions this connector is pinned with: ONLY those its own
    /// `hub-manifest.yaml` declares AND the executor acts on. A test pins this
    /// to be a subset of the manifest, so it can never grow past it.
    pub grants: &'static [&'static str],
}

/// Every connector this executor can pin.
pub const CONNECTOR_PLATFORMS: [ConnectorPlatform; 3] = [
    ConnectorPlatform {
        platform: "twitch",
        app_id: "waddles.core.service.twitch",
        grants: &[PERM_CONNECTOR_PII_READ, "flags.read", "platform.log"],
    },
    ConnectorPlatform {
        platform: "discord",
        app_id: "waddles.core.service.discord",
        grants: &[PERM_CONNECTOR_PII_READ, "platform.log"],
    },
    ConnectorPlatform {
        platform: "youtube",
        app_id: "waddles.core.service.youtube",
        grants: &[PERM_CONNECTOR_PII_READ],
    },
];

/// A platform to pin and the digest it must hash to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PinTarget {
    /// The connector.
    pub platform: ConnectorPlatform,
    /// `sha256:<64 lowercase hex>`.
    pub digest: String,
}

/// Bucket key of the connector component.
#[must_use]
pub fn component_key(platform: &str, digest: &str) -> String {
    format!("connectors/{platform}/{digest}.wasm")
}

/// Bucket key of the connector's signed sidecar.
#[must_use]
pub fn sidecar_key(platform: &str, digest: &str) -> String {
    format!("connectors/{platform}/{digest}.json")
}

/// The platforms `cfg` asks to pin, in a fixed order. A malformed digest is a
/// typed config error naming the variable.
pub fn pin_targets(cfg: &CliConfig) -> Result<Vec<PinTarget>, ExecutorError> {
    let mut targets = Vec::new();
    for (platform, (var, value)) in CONNECTOR_PLATFORMS
        .iter()
        .zip(cfg.connector_pin_digest_vars())
    {
        if let Some(digest) = parse_pin_digest(var, value)? {
            targets.push(PinTarget {
                platform: *platform,
                digest,
            });
        }
    }
    Ok(targets)
}

/// Pins every configured connector. Returns the number pinned. Fails on the
/// FIRST problem, naming the platform; the caller aborts startup.
pub async fn pin_configured<S: ComponentSource>(
    executor: &Executor<S>,
    cfg: &CliConfig,
) -> Result<usize, ExecutorError> {
    let targets = pin_targets(cfg)?;
    let limits = ConnectorLimits::from_config(cfg);
    for target in &targets {
        pin_one(executor, target, limits).await.map_err(|e| {
            ExecutorError::ConnectorLoad(format!(
                "startup pin of the {} connector failed: {e}",
                target.platform.platform
            ))
        })?;
        info!(
            platform = target.platform.platform,
            digest = %target.digest,
            "connector service bundle pinned"
        );
    }
    Ok(targets.len())
}

/// Fetch, verify and pin one connector.
async fn pin_one<S: ComponentSource>(
    executor: &Executor<S>,
    target: &PinTarget,
    limits: ConnectorLimits,
) -> Result<(), ExecutorError> {
    let keys = executor.signing_keys();
    if keys.is_empty() {
        return Err(ExecutorError::Config(
            "BUNDLE_SIGNING_PUBLIC_KEYS is empty: a connector is never pinned unverified"
                .to_string(),
        ));
    }
    let platform = target.platform;
    let (bytes, sidecar_bytes) = executor
        .source()
        .fetch(
            &component_key(platform.platform, &target.digest),
            &sidecar_key(platform.platform, &target.digest),
        )
        .await?;
    // The sidecar's `version` is a label bound by the signature (which also
    // covers app_id and digest, both pinned host-side); parse it so the one
    // shared verifier does the cross-checks and the Ed25519 verification.
    let sidecar: SignedSidecar = serde_json::from_slice(&sidecar_bytes)
        .map_err(|e| ExecutorError::SignatureInvalid(format!("sidecar is not valid JSON: {e}")))?;
    crate::signing::verify_artifact_signature(
        &sidecar_bytes,
        keys,
        platform.app_id,
        &sidecar.version,
        &target.digest,
    )?;
    let manifest = VerifiedManifest::new(
        platform.app_id,
        target.digest.clone(),
        WitWorld::ConnectorTools,
        platform
            .grants
            .iter()
            .map(|g| (*g).to_string())
            .collect::<HashSet<_>>(),
    );
    // `pin_connector` re-checks sha256(bytes) == digest before compiling.
    executor.pin_connector(&manifest, bytes, limits, None).await
}

/// The `RequestHandler` for the connector link (see the module doc).
pub struct ConnectorLinkHandler<S: ComponentSource> {
    executor: Arc<Executor<S>>,
}

impl<S: ComponentSource> ConnectorLinkHandler<S> {
    /// Wraps the shared executor.
    #[must_use]
    pub fn new(executor: Arc<Executor<S>>) -> Self {
        Self { executor }
    }
}

fn refuse(what: &str) -> ErrorBody {
    ErrorBody {
        code: ErrorCode::ExportMissing,
        message: format!("{what} is not served on the connector link"),
        detail: None,
    }
}

impl<S: ComponentSource> RequestHandler for ConnectorLinkHandler<S> {
    async fn on_load(&self, _body: LoadBody) -> Result<LoadedBody, ErrorBody> {
        Err(refuse("load"))
    }

    async fn on_unload(&self, _body: UnloadBody) -> Result<UnloadedBody, ErrorBody> {
        Err(refuse("unload"))
    }

    async fn on_invoke(
        &self,
        body: InvokeBody,
        invoke_id: u64,
        connection: Arc<Connection>,
    ) -> Result<ResultBody, ErrorBody> {
        self.executor
            .on_connector_invoke(body, invoke_id, connection)
            .await
    }

    async fn on_shutdown(&self, body: ShutdownBody) {
        warn!(
            grace_ms = body.grace_ms,
            "connector listener requested shutdown"
        );
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use clap::Parser;

    const D1: &str = "sha256:1111111111111111111111111111111111111111111111111111111111111111";
    const D2: &str = "sha256:2222222222222222222222222222222222222222222222222222222222222222";

    fn cfg(extra: &[&str]) -> CliConfig {
        let mut args = vec!["bundle-executor", "--stage-host-api-addr", "svc-x:8301"];
        args.extend_from_slice(extra);
        CliConfig::try_parse_from(args).unwrap()
    }

    #[test]
    fn keys_are_connector_scoped_paths() {
        assert_eq!(
            component_key("twitch", D1),
            format!("connectors/twitch/{D1}.wasm")
        );
        assert_eq!(
            sidecar_key("twitch", D1),
            format!("connectors/twitch/{D1}.json")
        );
    }

    #[test]
    fn targets_follow_the_set_variables_in_platform_order() {
        let c = cfg(&[
            "--connector-pin-digest-youtube",
            D2,
            "--connector-pin-digest-twitch",
            D1,
            "--connector-pin-digest-discord",
            "",
        ]);
        let t = pin_targets(&c).unwrap();
        let got: Vec<_> = t
            .iter()
            .map(|x| (x.platform.platform, x.digest.as_str()))
            .collect();
        assert_eq!(got, vec![("twitch", D1), ("youtube", D2)]);
        assert!(pin_targets(&cfg(&[])).unwrap().is_empty());
    }

    #[test]
    fn a_malformed_digest_is_refused_naming_the_variable_not_the_value() {
        for bad in ["abc", "sha256:XYZ", &D1.to_uppercase(), &format!("{D1}0")] {
            let c = cfg(&["--connector-pin-digest-discord", bad]);
            let err = pin_targets(&c).unwrap_err().to_string();
            assert!(err.contains("CONNECTOR_PIN_DIGEST_DISCORD"), "{err}");
            assert!(!err.contains(bad), "{err}");
        }
    }

    #[tokio::test]
    async fn the_connector_link_refuses_load_and_unload() {
        use crate::invoke::UnimplementedBucketSource;
        let exec = Arc::new(Executor::new(&cfg(&[]), UnimplementedBucketSource).unwrap());
        let h = ConnectorLinkHandler::new(exec);
        let load = LoadBody {
            tenant_id: 1,
            community_id: 0,
            app_id: "waddles.test.app".to_string(),
            version: "1".to_string(),
            digest: D1.to_string(),
            component_key: "k".to_string(),
            sidecar_key: "s".to_string(),
            capabilities: vec![],
            limits: penguin_bundle_host::wire::LoadLimits {
                timeout_ms: 2000,
                memory_mb: 64,
            },
        };
        assert_eq!(
            h.on_load(load).await.unwrap_err().code,
            ErrorCode::ExportMissing
        );
        let unload = UnloadBody {
            tenant_id: 1,
            community_id: 0,
            app_id: "waddles.test.app".to_string(),
            digest: D1.to_string(),
        };
        assert_eq!(
            h.on_unload(unload).await.unwrap_err().code,
            ErrorCode::ExportMissing
        );
        h.on_shutdown(ShutdownBody { grace_ms: 1 }).await;
    }

    #[test]
    fn app_ids_are_host_constants_in_the_core_service_namespace() {
        for p in CONNECTOR_PLATFORMS {
            assert_eq!(p.app_id, format!("waddles.core.service.{}", p.platform));
            assert!(p.grants.contains(&PERM_CONNECTOR_PII_READ));
        }
    }

    /// "NEVER broader": every grant a platform is pinned with is declared in
    /// that connector's own hub manifest.
    #[test]
    fn grants_are_a_subset_of_each_connector_manifest() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../bundles/rust");
        for p in CONNECTOR_PLATFORMS {
            let path = root.join(p.platform).join("hub-manifest.yaml");
            let text = std::fs::read_to_string(&path).unwrap();
            let declared: HashSet<&str> = text
                .lines()
                .filter_map(|l| l.trim().strip_prefix("- id:"))
                .map(str::trim)
                .collect();
            assert!(!declared.is_empty(), "no permissions parsed from {path:?}");
            for g in p.grants {
                assert!(
                    declared.contains(g),
                    "{} grant {g} not in its manifest",
                    p.platform
                );
            }
        }
    }
}
