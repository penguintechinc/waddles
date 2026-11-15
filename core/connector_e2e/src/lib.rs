//! Real-path harness for the connector-bundle host capability (issue #840).
//!
//! [`WireRig`] stands up the PRODUCTION path end to end inside one process:
//! a real bundle [`Executor`] with a real compiled WASM connector pinned,
//! speaking the host-API wire (`penguin-bundle-host` 0.3.0
//! `ExportKind::Connector*`) over a duplex stream to svc_ingest's real
//! [`ExecutorLink`], with svc_ingest's production [`WireConnectorInvoker`] on
//! top. Nothing between the driver and wasmtime is mocked: the codec, the
//! invoker, the executor's `run_connection` read loop, `ConnectorRouter`,
//! `ConnectorHost` and the guest are all shipped code.
//!
//! Only the duplex pipe stands in for the mTLS socket the two processes use
//! in a deployment (the TLS accept/dial layers are separate, already-tested
//! seams). Reconnects are real: [`WireRig::reconnect`] drops the connection and
//! dials a fresh one against the SAME executor, exactly what its dial loop does.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::connector::ConnectorLimits;
use bundle_executor::error::ExecutorError;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use bundle_executor::wire::run_connection;
use clap::Parser;
use sha2::{Digest, Sha256};
use svc_ingest::connector::{
    ConnectorRegistry, ExecutorLink, PinnedConnector, WireConnectorInvoker,
};
use tokio::io::duplex;
use tokio::task::JoinHandle;

/// Bytes the in-memory pipe buffers each way: far above one frame so a test
/// never blocks on pipe back-pressure it does not mean to exercise.
const PIPE_BYTES: usize = 4 << 20;

/// How long [`WireRig::reconnect`] lets a replaced executor connection wind
/// down on its own before aborting it.
const OLD_CONNECTION_GRACE: Duration = Duration::from_secs(5);

/// A [`ComponentSource`] that never serves anything: connectors are pinned
/// directly (never fetched over the wire), so a stage `load` reaching this
/// would be a harness bug and fails typed rather than returning fake bytes.
pub struct NoSource;

impl ComponentSource for NoSource {
    async fn fetch(
        &self,
        component_key: &str,
        _sidecar_key: &str,
    ) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
        Err(ExecutorError::Config(format!(
            "connector-e2e serves no stage components (asked for {component_key:?})"
        )))
    }
}

/// `sha256:<hex>` of `bytes`, the digest form the executor and registry use.
#[must_use]
pub fn digest_of(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("sha256:{:x}", hasher.finalize())
}

/// One connector to pin: its platform slug, core `app_id` and component bytes.
pub struct RigConnector {
    /// Platform slug the driver resolves it by.
    pub platform: String,
    /// Core-namespaced `app_id` (the connector world is core-only).
    pub app_id: String,
    /// The compiled component.
    pub wasm: &'static [u8],
    /// The WIT world it was built for (`Connector` or `ConnectorTools`).
    pub world: WitWorld,
}

/// A live executor + svc_ingest wire link with the connectors pinned.
pub struct WireRig {
    /// svc_ingest's side of the executor connection.
    pub link: Arc<ExecutorLink>,
    /// The production invoker, ready to hand to a `ConnectorDriver`.
    pub invoker: Arc<WireConnectorInvoker>,
    /// The registry the invoker and a driver should share.
    pub registry: Arc<ConnectorRegistry>,
    /// The real executor (its pinned registry survives reconnects).
    pub executor: Arc<Executor<NoSource>>,
    /// Keeps the flag-answering stage of the connectors' host-call bridge alive.
    _stage_link: Option<Arc<ExecutorLink>>,
    connection: Option<JoinHandle<Result<(), ExecutorError>>>,
}

impl WireRig {
    /// Builds the executor, pins every connector (after `tune`ing the
    /// default limits), registers them for the host side and attaches the
    /// first connection.
    ///
    /// # Panics
    /// Test harness: any setup failure is a loud panic naming the cause.
    pub async fn start(
        connectors: Vec<RigConnector>,
        max_frame_bytes: usize,
        tune: impl Fn(&mut ConnectorLimits),
    ) -> Self {
        Self::start_with_permissions(connectors, &[], &[], max_frame_bytes, tune).await
    }

    /// As [`WireRig::start`], granting every pinned connector the manifest
    /// `permissions` (e.g. `connector.pii.read`, which the real platform
    /// connectors import `identity` under) and answering `flags.enabled`
    /// host-calls `true` for `enabled_flags` (the boot path's connector cutover
    /// flags, `svc_ingest::outbound_boot::CONNECTOR_FLAGS`).
    ///
    /// # Panics
    /// Test harness: any setup failure is a loud panic naming the cause.
    #[allow(clippy::unwrap_used, clippy::expect_used)]
    pub async fn start_with_permissions(
        connectors: Vec<RigConnector>,
        permissions: &[&str],
        enabled_flags: &[&str],
        max_frame_bytes: usize,
        tune: impl Fn(&mut ConnectorLimits),
    ) -> Self {
        let cfg = CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-ingest:8301",
        ])
        .expect("static executor args parse");
        let executor = Arc::new(Executor::new(&cfg, NoSource).expect("executor builds"));
        // With flags to answer, connectors get a REAL host-call bridge whose
        // peer is a REAL `ExecutorLink` (a second instance: attaching on the
        // main link would replace the main session) -- so `flags.enabled` is
        // answered by svc_ingest's own link code, not a script.
        let (bridge, stage_link) = if enabled_flags.is_empty() {
            (None, None)
        } else {
            let link =
                Arc::new(ExecutorLink::new().with_enabled_flags(enabled_flags.iter().copied()));
            let (stage_io, exec_io) = duplex(PIPE_BYTES);
            let attach_link = Arc::clone(&link);
            let (attached, bridge) = tokio::join!(
                async move { attach_link.attach(stage_io).await },
                bundle_executor::stage_bridge::bridge_to_stage(exec_io)
            );
            attached.expect("flag stage attaches");
            (Some(bridge.expect("stage bridge handshake")), Some(link))
        };
        let mut registry = ConnectorRegistry::new();
        for c in connectors {
            let digest = digest_of(c.wasm);
            let manifest = VerifiedManifest::new(
                c.app_id.clone(),
                digest.clone(),
                c.world,
                permissions
                    .iter()
                    .map(|p| (*p).to_string())
                    .collect::<HashSet<_>>(),
            );
            let mut limits = ConnectorLimits::from_config(&cfg);
            tune(&mut limits);
            executor
                .pin_connector(&manifest, c.wasm.to_vec(), limits, bridge.clone())
                .await
                .expect("connector pins");
            registry
                .register(PinnedConnector::new(
                    c.platform,
                    c.app_id,
                    digest,
                    max_frame_bytes,
                ))
                .expect("connector registers");
        }
        let registry = Arc::new(registry);
        let link = Arc::new(ExecutorLink::new());
        let invoker = Arc::new(WireConnectorInvoker::new(
            Arc::clone(&link),
            Arc::clone(&registry),
        ));
        let mut rig = Self {
            link,
            invoker,
            registry,
            executor,
            _stage_link: stage_link,
            connection: None,
        };
        rig.reconnect().await;
        rig
    }

    /// Ends the current executor connection (if any) and dials a fresh one
    /// against the same executor -- what its dial loop does after a drop.
    ///
    /// # Panics
    /// Test harness: a failed handshake is a loud panic.
    #[allow(clippy::expect_used)]
    pub async fn reconnect(&mut self) {
        // An executor replica that reconnects does so because its old connection
        // ended; the link no longer replaces a session on attach (several
        // replicas stay attached side by side), so end the old connection first,
        // exactly as the real dial loop's previous connection would have ended.
        if let Some(old) = self.connection.take() {
            let abort = old.abort_handle();
            abort.abort();
            let _ = tokio::time::timeout(OLD_CONNECTION_GRACE, old).await;
            for _ in 0..500 {
                if !self.link.is_attached() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }
        let (stage_io, exec_io) = duplex(PIPE_BYTES);
        let executor = Arc::clone(&self.executor);
        let hello = executor.hello(false, "runc");
        self.connection = Some(tokio::spawn(async move {
            run_connection(
                exec_io,
                hello,
                executor,
                "connector-e2e",
                Heartbeat::disabled(),
            )
            .await
        }));
        self.link
            .attach(stage_io)
            .await
            .expect("host-api handshake completes");
    }

    /// Attaches a SECOND executor replica to the same link: a brand-new
    /// [`Executor`] with `connectors` pinned (the same digests every replica
    /// pins from the chart's `CONNECTOR_PIN_DIGEST_*`), dialing the link over its
    /// own host-API connection -- what each pod of the scaled-out
    /// `svc-bundles-executor` Deployment does.
    ///
    /// # Panics
    /// Test harness: any setup failure is a loud panic naming the cause.
    #[allow(clippy::expect_used)]
    pub async fn add_replica(&self, connectors: &[RigConnector]) -> Replica {
        let cfg = CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-ingest:8301",
        ])
        .expect("static executor args parse");
        let executor = Arc::new(Executor::new(&cfg, NoSource).expect("replica builds"));
        for c in connectors {
            let manifest =
                VerifiedManifest::new(c.app_id.clone(), digest_of(c.wasm), c.world, HashSet::new());
            executor
                .pin_connector(
                    &manifest,
                    c.wasm.to_vec(),
                    ConnectorLimits::from_config(&cfg),
                    None,
                )
                .await
                .expect("replica pins the connector");
        }
        let (stage_io, exec_io) = duplex(PIPE_BYTES);
        let hello = executor.hello(false, "runc");
        let running = Arc::clone(&executor);
        let connection = tokio::spawn(async move {
            run_connection(
                exec_io,
                hello,
                running,
                "connector-e2e-replica",
                Heartbeat::disabled(),
            )
            .await
        });
        self.link
            .attach(stage_io)
            .await
            .expect("replica handshake completes");
        Replica {
            executor,
            connection: Some(connection),
        }
    }

    /// Drops the executor connection without a replacement (a crashed
    /// executor): the link must report it, never keep a dead session.
    pub fn sever(&mut self) {
        if let Some(old) = self.connection.take() {
            old.abort();
        }
    }
}

/// A second executor replica attached to a [`WireRig`]'s link.
pub struct Replica {
    /// The replica's own executor (own pinned registry).
    pub executor: Arc<Executor<NoSource>>,
    connection: Option<JoinHandle<Result<(), ExecutorError>>>,
}

impl Replica {
    /// Ends this replica's host-API connection (the pod went away).
    pub fn sever(&mut self) {
        if let Some(c) = self.connection.take() {
            c.abort();
        }
    }
}

impl Drop for Replica {
    fn drop(&mut self) {
        self.sever();
    }
}

impl Drop for WireRig {
    fn drop(&mut self) {
        self.sever();
    }
}
