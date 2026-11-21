//! Action-stage emission of an automated reputation floor ban.
//!
//! The process stage (`svc_process::capabilities::floor_ban`) records a verified floor-ban intent
//! in the outbox (`bundle_reputation_floor_bans`); [`crate::floor_ban_dispatch`] leases each row
//! and calls [`StageCapabilities::emit_floor_ban`] here. This is the EXISTING outbound moderation
//! path -- the same producer pipeline a platform service bundle's `*.moderation.ban` host tool
//! uses (`host_tools`), minus the tool planner (there is no calling bundle or human actor, and
//! the process stage already authorised the caller's per-community grant):
//!
//! ```text
//!  outbox row ──► service grant (moderation.<platform>) ► drain-ready ► throttle
//!       ► detokenize user (hub-api, host-side) ► protected-target check
//!       ► signed `mod.ban` envelope (pin_sig, `guild:-` = the community's bound guild)
//!       ──LPUSH──► svc_ingest sender_exec ──ack {ok|code}──► confirmed / typed failure
//! ```
//!
//! The destination is never caller-named: the guild is pinned from the community's binding by the
//! sender's duty hook (`guild:-` asserts no guild), the target is the tokenized user UUID
//! resolved by the sender (`{user:<uuid>}` placeholder; the raw platform id never rides the wire
//! or a log), and only the sender's CONFIRMED ack is success.

use bundle_capability_gate::resource::{AppScopedResource, ResourceRef};
use bundle_host_kv::KvBackend;
use uuid::Uuid;

use super::host_tools::{NO_LINKED_ACCOUNT, PROTECTED_TARGET};
use super::{
    now_epoch_ms, outbound_relay_queue_key, stamp_handshake, InvokeScope, RelayOp, RelayQueue,
    StageCapabilities, OUTBOUND_SCHEMA_VERSION,
};

/// Core moderation bundles whose confirmed platform `mod.unban` lifts the matching Waddles ban
/// (explicit list, in addition to the `penguintechinc.waddles.` core-namespace check). Any other
/// bundle needs the `reputation.floor_ban.lift` permission.
pub(crate) const FLOOR_LIFT_CORE_ALLOWLIST: &[&str] = &["penguintechinc.waddles.chatguard"];

/// Platforms whose floor ban this stage can deliver (`svc_process` refuses the rest up front).
pub(crate) const FLOOR_BAN_PLATFORMS: &[&str] = &["discord"];

/// One leased outbox row to deliver.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FloorBanCommand {
    /// The outbox row id: the correlation id of the whole delivery.
    pub id: i64,
    /// Numeric tenant (host-derived from the row, never a guest value).
    pub tenant_id: i32,
    /// Numeric community the ban is executed in.
    pub community_id: i32,
    /// The banned user (UUID token only, never a platform id).
    pub user: Uuid,
    /// The platform whose `mod.ban` op to emit.
    pub platform: String,
    /// Which attempt this is (1-based); part of the sender op id.
    pub attempt: i32,
    /// Deliver `mod.unban` instead of `mod.ban` (a Waddles lift of a delivered ban).
    pub unban: bool,
}

/// Why a delivery did not complete, with a stable machine code (`^[a-z_]{1,64}$`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FloorSendError {
    /// Transient or unconfirmed: retried with backoff (a duplicate ban is idempotent).
    Retry(String),
    /// Retrying cannot help (protected target, no linked account, unsupported platform): the row
    /// is closed as refused with this code.
    Permanent(String),
}

impl FloorSendError {
    /// The stable machine code stored on the row and used as a metric label.
    #[must_use]
    pub fn code(&self) -> &str {
        match self {
            Self::Retry(c) | Self::Permanent(c) => c,
        }
    }
}

/// A failed delivery plus the sender op id when one was already minted (for correlation).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FloorSendFailure {
    /// What went wrong.
    pub error: FloorSendError,
    /// The outbound op id, once the command was built.
    pub op_id: Option<String>,
}

impl FloorSendFailure {
    fn retry(code: &str, op_id: Option<String>) -> Self {
        Self {
            error: FloorSendError::Retry(code.to_string()),
            op_id,
        }
    }
    fn permanent(code: &str) -> Self {
        Self {
            error: FloorSendError::Permanent(code.to_string()),
            op_id: None,
        }
    }
}

/// Maps a host refusal of the detokenize / protected-target steps onto a delivery failure: the
/// two outcomes retrying cannot fix are permanent, everything else is retried.
fn failure_of(r: &bundle_tool_call::ToolRefusal) -> FloorSendFailure {
    let msg = r.message();
    if msg.starts_with(PROTECTED_TARGET) {
        FloorSendFailure::permanent("protected_target")
    } else if msg.starts_with(NO_LINKED_ACCOUNT) {
        FloorSendFailure::permanent("no_linked_account")
    } else {
        FloorSendFailure::retry(r.code(), None)
    }
}

/// The sender op id: carries the outbox row id and attempt (the correlation) plus a nonce, in the
/// sender's safe alphabet and length (`[A-Za-z0-9_-]{1,64}`).
fn op_id_of(cmd: &FloorBanCommand) -> String {
    let prefix = if cmd.unban { "fu" } else { "fb" };
    format!(
        "{prefix}-{}-{}-{}",
        cmd.id,
        cmd.attempt,
        Uuid::new_v4().simple()
    )
}

impl<Q: RelayQueue, K: KvBackend> StageCapabilities<Q, K> {
    /// Installs the platform -> Waddles unban sync (once; a second call is ignored).
    pub fn set_unban_sync(&self, sync: std::sync::Arc<dyn crate::floor_ban_dispatch::UnbanSync>) {
        let _ = self.unban_sync.set(sync);
    }

    /// Whether `scope`'s calling bundle may turn its confirmed platform `mod.unban` into a lift of
    /// the Waddles floor/soft ban: only a core moderation bundle on the explicit
    /// [`FLOOR_LIFT_CORE_ALLOWLIST`], or any bundle holding the dedicated per-action permission
    /// `reputation.floor_ban.lift` in this community. A vendor bundle's unban still reaches the
    /// platform but lifts NOTHING in Waddles (a bundle must not be able to lift bans).
    pub(crate) fn may_sync_lift(&self, scope: &InvokeScope) -> bool {
        if bundle_capability_gate::is_core_namespace(&scope.app_id)
            && FLOOR_LIFT_CORE_ALLOWLIST.contains(&scope.app_id.as_str())
        {
            return true;
        }
        self.gate
            .authorize(
                &scope.gate_scope(),
                bundle_capability_gate::PermissionId::ReputationFloorBanLift,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .is_ok()
    }

    /// After the sender CONFIRMED a platform `mod.unban` (chatguard `!unban`, a granted bundle),
    /// lifts the matching Waddles floor ban and soft ban in THIS tenant + community (taken from
    /// the invoke scope, never the event), but only for a trusted caller
    /// ([`Self::may_sync_lift`]). The calling app id is recorded. No unban is enqueued back (no
    /// echo), a sync failure is an ERROR log + metric but never fails the already-confirmed
    /// platform unban, and nothing but the numeric tenant/community, app id and counts is logged.
    pub(crate) async fn sync_confirmed_unban(
        &self,
        scope: &InvokeScope,
        platform: &str,
        user_uuid: &str,
    ) {
        let (tenant_id, community_id) = (scope.tenant_id, scope.community_id);
        let Some(sync) = self.unban_sync.get() else {
            return;
        };
        if !self.may_sync_lift(scope) {
            self.stage_metrics
                .record_floor_ban_dispatch("platform_lift_untrusted");
            tracing::info!(
                tenant_id, community_id, platform, app_id = %scope.app_id,
                "platform unban confirmed; caller is not trusted to lift Waddles bans: nothing lifted"
            );
            return;
        }
        let Ok(user) = user_uuid.parse::<Uuid>() else {
            return;
        };
        match sync
            .lift_from_platform(tenant_id, community_id, platform, user, &scope.app_id)
            .await
        {
            Ok((lifted, soft)) => {
                self.stage_metrics
                    .record_floor_ban_dispatch(if lifted + soft > 0 {
                        "platform_lift"
                    } else {
                        "platform_lift_none"
                    });
                tracing::info!(
                    tenant_id,
                    community_id,
                    platform,
                    lifted,
                    soft_cleared = soft,
                    "platform unban confirmed; matching Waddles ban lifted (no echo)"
                );
            }
            Err(e) => {
                self.stage_metrics
                    .record_floor_ban_dispatch("platform_lift_error");
                tracing::error!(
                    tenant_id, community_id, platform, error = %e,
                    "ALERT platform unban confirmed but the Waddles floor ban could NOT be lifted"
                );
            }
        }
    }

    /// Emits the signed outbound `mod.ban` command for one floor-ban outbox row and waits for the
    /// sender's confirmation. `Ok(op_id)` ONLY on a confirmed ack; every other outcome is a typed
    /// [`FloorSendFailure`] (silence until the deadline is `relay_unconfirmed`, never success).
    ///
    /// # Errors
    /// A [`FloorSendFailure`] naming the stable code and whether a retry can help.
    pub(crate) async fn emit_floor_ban(
        &self,
        cmd: &FloorBanCommand,
    ) -> Result<String, FloorSendFailure> {
        let platform = cmd.platform.as_str();
        if !FLOOR_BAN_PLATFORMS.contains(&platform) {
            return Err(FloorSendFailure::permanent("platform_not_supported"));
        }
        let op_name = if cmd.unban { "mod.unban" } else { "mod.ban" };
        let Some(op) = bundle_capability_gate::platform_ops::registered(platform, op_name) else {
            return Err(FloorSendFailure::permanent("platform_not_supported"));
        };
        let relay_op = RelayOp::Registered(op);
        if cmd.tenant_id < 1 || cmd.community_id < 1 {
            return Err(FloorSendFailure::permanent("invalid_scope"));
        }
        let Some(wiring) = &self.tool else {
            return Err(FloorSendFailure::retry("service_unavailable", None));
        };
        let service = format!("penguintechinc.waddles.{platform}");
        let Some(entry) =
            wiring
                .snapshot
                .tool_entry_in_scope(cmd.tenant_id, cmd.community_id, &service)
        else {
            return Err(FloorSendFailure::retry("service_unavailable", None));
        };
        // The platform SERVICE bundle must hold the op's catalog permission here (the grant that
        // lets it act with the community's credential); the caller's own grant was enforced on
        // the process stage when the intent was recorded.
        let scope = InvokeScope {
            tenant: cmd.tenant_id.to_string(),
            community: Some(cmd.community_id.to_string()),
            app_id: service.clone(),
            origin_channel_id: None,
            origin_platform: None,
            origin_guild_id: None,
            origin_user_id: None,
            tenant_id: cmd.tenant_id,
            community_id: cmd.community_id,
            app_version: entry.version_id,
            tool_chain: vec![service],
            deadline_at: None,
            trace: None,
        };
        let permission = bundle_capability_gate::platform_ops::permission_for(platform, op)
            .ok_or_else(|| FloorSendFailure::permanent("platform_not_supported"))?;
        if let Err(denied) = self.gate.authorize(
            &scope.gate_scope(),
            permission,
            ResourceRef::AppScoped(AppScopedResource::None),
        ) {
            tracing::warn!(
                floor_ban_id = cmd.id, community_id = cmd.community_id, reason = %denied,
                "floor ban not sent: the platform bundle lacks the moderation grant"
            );
            return Err(FloorSendFailure::retry("scope_denied", None));
        }
        self.require_discord_drain_ready(&scope, relay_op)
            .await
            .map_err(|e| FloorSendFailure::retry(&e.code, None))?;
        self.throttle_platform_op(&scope, platform, relay_op)
            .await
            .map_err(|e| FloorSendFailure::retry(&e.code, None))?;
        // The destructive-op protected-target check: the id is resolved HOST-side for this check
        // alone and is never queued or logged.
        let uuid = cmd.user.to_string();
        let platform_id = self
            .detokenize_user(&scope, platform, &uuid)
            .await
            .map_err(|r| failure_of(&r))?;
        if !cmd.unban {
            self.refuse_protected_target(&scope, platform, "mod.ban", &platform_id)
                .await
                .map_err(|r| failure_of(&r))?;
        }

        let op_id = op_id_of(cmd);
        let exp_ms = now_epoch_ms().saturating_add(
            u64::try_from(self.discord_ack_timeout.as_millis()).unwrap_or(u64::MAX),
        );
        let mut env = serde_json::json!({
            "v": OUTBOUND_SCHEMA_VERSION,
            "op": op,
            "platform": platform,
            "community_id": i64::from(cmd.community_id),
            "tenant_id": i64::from(cmd.tenant_id),
            // A channel-less guild-scope op: the guild is pinned from the community's binding.
            "origin_channel": "guild:-",
            "user_id": format!("{{user:{uuid}}}"),
        });
        stamp_handshake(&mut env, &op_id, exp_ms);
        // Signed AFTER the handshake so the MAC binds `exp_ms` and the op id.
        self.sign_envelope(&mut env)
            .map_err(|e| FloorSendFailure::retry(&e.code, Some(op_id.clone())))?;
        let payload = env.to_string();
        let bytes = payload.len() as u64;
        self.relay_queue
            .lpush(&outbound_relay_queue_key(platform), payload)
            .await
            .map_err(|_| FloorSendFailure::retry("relay_unavailable", Some(op_id.clone())))?;
        tracing::info!(
            floor_ban_id = cmd.id, attempt = cmd.attempt, community_id = cmd.community_id,
            platform, op_id = %op_id, unban = cmd.unban, "floor command queued for the sender"
        );
        self.await_platform_ack(&scope, platform, relay_op, &op_id)
            .await
            .map_err(|e| FloorSendFailure::retry(&e.code, Some(op_id.clone())))?;
        self.usage
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .record_relay_call(
                &scope.tenant,
                scope.community.as_deref(),
                "",
                &scope.app_id,
                bytes,
            );
        Ok(op_id)
    }
}
