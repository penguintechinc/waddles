//! `gameserver_net.*` host calls (`wit/waddle-bundle/tool.wit` `interface gameserver-net`),
//! carried as `capability = db`. Fail-loud order: (1) op parsing; (2) FIRST-PARTY CORE only
//! (defense in depth on top of the global-admin grant); (3) the activation must have a
//! community; (4) for an open/exchange the gate authorizes `gameserver.net`; (5) wiring + flag;
//! (6) `bundle_host_gameserver_net`, which resolves the connection for exactly this
//! (tenant, community), runs the SSRF guard on the resolved IP, performs the RCON login and
//! enforces every limit. Tenant/community come from this invoke's host-derived scope, NEVER from
//! `call.args`; the password and address never appear in a result or an error.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use bundle_capability_gate::{AppScopedResource, PermissionId, ResourceRef};
use bundle_host_gameserver_net::{GameNet, NetError, Scheme, Scope, Session};
use penguin_bundle_host::wire::{HostCallBody, HostResultError};
use tokio::sync::{Mutex, OwnedSemaphorePermit, Semaphore};

use bundle_host_kv::KvBackend;

use super::{denied, denied_from_gate, FeatureGate, StageCapabilities};

/// Plain opt-in flag (default OFF): unseen/OFF/license-server-unreachable denies every call
/// `feature_disabled`.
pub const BUNDLE_GAMESERVER_NET_CAPABILITY_FLAG: &str = "waddles.bundle-gameserver-net";

/// Most sessions one invocation may hold open at once.
const MAX_SESSIONS_PER_INVOCATION: usize = 2;
/// Largest base64 field the stage accepts from the executor (the host caps are lower still).
const MAX_B64_LEN: usize = 16 * 1024;
/// First-party namespace: the only apps that may use this capability.
const CORE_NAMESPACE: &str = "waddles.core.";

/// Live wiring for the `gameserver-net` capability. `None` on the handler means every
/// `gameserver_net.*` call denies `not_implemented`.
#[derive(Clone)]
pub struct GameServerNetWiring {
    /// The guarded network capability (directory, limits, audit inside).
    pub net: Arc<GameNet>,
    /// [`BUNDLE_GAMESERVER_NET_CAPABILITY_FLAG`] gate -- OFF denies before any socket work.
    pub flag: Arc<dyn FeatureGate>,
}

/// One invocation's open sessions, keyed by an opaque handle. Dropped (closing every socket)
/// when the invocation's [`StageCapabilities`] is dropped.
///
/// The per-invocation cap is a semaphore: a permit is taken BEFORE a session is opened and stored
/// alongside it, so it is held for the session's whole lifetime -- including while the session is
/// temporarily out of the map for I/O (a `map.len()` check would undercount exactly then).
pub(super) struct SessionTable {
    next: AtomicU64,
    slots: Arc<Semaphore>,
    map: Mutex<HashMap<u64, Slot>>,
}

/// An open session plus the cap permit that pays for it.
pub(super) struct Slot {
    session: Session,
    _permit: OwnedSemaphorePermit,
}

impl Default for SessionTable {
    fn default() -> Self {
        Self {
            next: AtomicU64::new(0),
            slots: Arc::new(Semaphore::new(MAX_SESSIONS_PER_INVOCATION)),
            map: Mutex::new(HashMap::new()),
        }
    }
}

fn net_error_to_host(err: NetError) -> HostResultError {
    match err {
        NetError::Denied(code) => denied("denied", code),
        NetError::Unreachable => denied("unreachable", "unreachable"),
        NetError::Timeout => denied("timeout", "timeout"),
        NetError::AuthFailed => denied("auth_failed", "auth_failed"),
        NetError::TooLarge => denied("too_large", "too_large"),
        NetError::Backend => denied("backend", "gameserver-net backend error"),
    }
}

fn str_arg<'a>(args: &'a serde_json::Value, k: &str) -> Result<&'a str, HostResultError> {
    args.get(k)
        .and_then(|v| v.as_str())
        .ok_or_else(|| denied("invalid_args", format!("{k} must be a string")))
}

fn handle_arg(args: &serde_json::Value) -> Result<u64, HostResultError> {
    args.get("handle")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| denied("invalid_args", "handle must be an integer"))
}

fn bytes_arg(args: &serde_json::Value, k: &str) -> Result<Vec<u8>, HostResultError> {
    let s = str_arg(args, k)?;
    if s.len() > MAX_B64_LEN {
        return Err(denied("too_large", "too_large"));
    }
    B64.decode(s)
        .map_err(|_| denied("invalid_args", format!("{k} must be base64")))
}

impl<K: KvBackend + 'static> StageCapabilities<K> {
    /// Attaches the `gameserver-net` capability's live wiring -- once per invoke, with a clone of
    /// the process-wide [`GameServerNetWiring`].
    pub fn with_gameserver_net(mut self, wiring: GameServerNetWiring) -> Self {
        self.gameserver_net = Some(wiring);
        self
    }

    /// Dispatches one `gameserver_net.*` call (see the module doc for the order).
    pub(super) async fn handle_gameserver_net(
        &self,
        call: &HostCallBody,
    ) -> Result<serde_json::Value, HostResultError> {
        let op = call
            .op
            .strip_prefix("gameserver_net.")
            .ok_or_else(|| denied("unknown_op", "not a gameserver_net op"))?;
        if !matches!(
            op,
            "rcon_open" | "tcp_open" | "udp_exchange" | "tcp_write" | "tcp_read" | "tcp_close"
        ) {
            return Err(denied(
                "unknown_op",
                format!("gameserver_net op {op:?} not supported"),
            ));
        }
        if !self.app_id.starts_with(CORE_NAMESPACE) {
            return Err(denied(
                "not_granted",
                "gameserver-net is available to first-party core bundles only",
            ));
        }
        if self.community_id == 0 || self.community.is_none() {
            return Err(denied(
                "invalid_args",
                "gameserver-net access is community-scoped; this activation has no community",
            ));
        }
        let opens = matches!(op, "rcon_open" | "tcp_open" | "udp_exchange");
        if opens {
            self.gate
                .authorize(
                    &self.gate_scope(),
                    PermissionId::GameserverNet,
                    ResourceRef::AppScoped(AppScopedResource::None),
                )
                .map_err(denied_from_gate)?;
        }
        let Some(wiring) = &self.gameserver_net else {
            return Err(denied(
                "not_implemented",
                "gameserver-net capability is not provisioned in this deployment",
            ));
        };
        if !wiring.flag.enabled().await {
            return Err(denied(
                "feature_disabled",
                "gameserver-net capability is disabled (waddles.bundle-gameserver-net is OFF)",
            ));
        }
        let scope = Scope {
            tenant_id: i64::from(self.tenant_id),
            community_id: i64::from(self.community_id),
        };
        let args = &call.args;
        match op {
            "rcon_open" => {
                let scheme = match str_arg(args, "scheme")? {
                    "source-rcon" => Scheme::SourceRcon,
                    "minecraft-rcon" => Scheme::MinecraftRcon,
                    _ => return Err(denied("invalid_args", "unknown scheme")),
                };
                let permit = self.reserve_slot()?;
                let r = wiring
                    .net
                    .rcon_open(&scope, str_arg(args, "server")?, scheme)
                    .await;
                self.store_session(r, permit).await
            }
            "tcp_open" => {
                let permit = self.reserve_slot()?;
                let r = wiring.net.tcp_open(&scope, str_arg(args, "server")?).await;
                self.store_session(r, permit).await
            }
            "udp_exchange" => {
                let payload = bytes_arg(args, "payload_b64")?;
                let max = args
                    .get("max_response")
                    .and_then(|v| v.as_u64())
                    .and_then(|v| u32::try_from(v).ok())
                    .ok_or_else(|| denied("invalid_args", "max_response must be a u32"))?;
                let out = wiring
                    .net
                    .udp_exchange(&scope, str_arg(args, "server")?, &payload, max)
                    .await
                    .map_err(net_error_to_host)?;
                Ok(serde_json::json!({ "bytes_b64": B64.encode(out) }))
            }
            "tcp_write" => {
                let handle = handle_arg(args)?;
                let bytes = bytes_arg(args, "bytes_b64")?;
                let mut slot = self.take_session(handle).await?;
                let r = slot.session.write_all(&bytes).await;
                self.put_session(handle, slot).await;
                r.map_err(net_error_to_host)?;
                Ok(serde_json::json!({}))
            }
            "tcp_read" => {
                let handle = handle_arg(args)?;
                let max = args
                    .get("max")
                    .and_then(|v| v.as_u64())
                    .and_then(|v| u32::try_from(v).ok())
                    .ok_or_else(|| denied("invalid_args", "max must be a u32"))?;
                let mut slot = self.take_session(handle).await?;
                let r = slot.session.read(max).await;
                self.put_session(handle, slot).await;
                let bytes = r.map_err(net_error_to_host)?;
                Ok(serde_json::json!({ "bytes_b64": B64.encode(bytes) }))
            }
            _ => {
                let handle = handle_arg(args)?;
                self.gs_sessions.map.lock().await.remove(&handle);
                Ok(serde_json::json!({}))
            }
        }
    }

    /// Takes one of this invocation's session permits or fails closed. The permit lives as long as
    /// the session it pays for (dropped on close, failed open, or invocation end).
    fn reserve_slot(&self) -> Result<OwnedSemaphorePermit, HostResultError> {
        Arc::clone(&self.gs_sessions.slots)
            .try_acquire_owned()
            .map_err(|_| denied("denied", "too_many_connections"))
    }

    async fn store_session(
        &self,
        opened: Result<Session, NetError>,
        permit: OwnedSemaphorePermit,
    ) -> Result<serde_json::Value, HostResultError> {
        let session = opened.map_err(net_error_to_host)?;
        let handle = self.gs_sessions.next.fetch_add(1, Ordering::Relaxed) + 1;
        self.gs_sessions.map.lock().await.insert(
            handle,
            Slot {
                session,
                _permit: permit,
            },
        );
        Ok(serde_json::json!({ "handle": handle }))
    }

    /// Takes a session (and its permit) out of the table for the duration of one I/O (no lock held
    /// across it). The permit travels with it, so the cap still counts the session meanwhile.
    async fn take_session(&self, handle: u64) -> Result<Slot, HostResultError> {
        self.gs_sessions
            .map
            .lock()
            .await
            .remove(&handle)
            .ok_or_else(|| denied("invalid_args", "unknown or busy session handle"))
    }

    async fn put_session(&self, handle: u64, slot: Slot) {
        self.gs_sessions.map.lock().await.insert(handle, slot);
    }
}

#[cfg(test)]
#[path = "../capabilities_gameserver_net_tests.rs"]
mod tests;
