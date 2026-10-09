//! The `PermissionId` catalog (spec SS1): every permission this platform's
//! Android-style consent flow can grant, with its risk level, the
//! `CapabilityKind` label it maps to, and its default quota shape.
//!
//! The catalog is **closed** (spec SS2.3: "Reject an unknown permission id
//! outright -- the catalog is closed, not extensible per-bundle") -- adding
//! a permission means adding a variant here, never accepting an arbitrary
//! string from a manifest at parse time.

use std::fmt;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::time::Duration;

/// No `net.http.private-ip` grant may be coarser than this -- mirrors
/// hub-api's own `_MAX_PRIVATE_PREFIX_V4/6` (`bundle_permission_catalog.py`).
/// A /8 would hand a bundle the whole RFC1918 `10.0.0.0/8` block; /16 (v4) /
/// /64 (v6) is the widest an operator should ever hand a single bundle.
const MAX_PRIVATE_PREFIX_V4: u8 = 16;
const MAX_PRIVATE_PREFIX_V6: u8 = 64;

/// AWS IMDSv2's IPv6 metadata address -- outside `fe80::/10`, so it needs
/// its own explicit check (mirrors `bundle_permission_catalog.py`).
const METADATA_V6: Ipv6Addr = Ipv6Addr::new(0xfd00, 0x0ec2, 0, 0, 0, 0, 0, 0x0254);

fn ipv6_is_link_local(v6: &Ipv6Addr) -> bool {
    (v6.segments()[0] & 0xffc0) == 0xfe80
}

fn ipv6_is_unique_local(v6: &Ipv6Addr) -> bool {
    (v6.segments()[0] & 0xfe00) == 0xfc00
}

/// Loopback, link-local, or cloud-metadata -- always denied regardless of
/// family or instance policy (spec: "Loopback, link-local and metadata...
/// are ALWAYS denied, even with private-ip"). This crate does not (yet) know
/// this cluster's own pod/service/node CIDRs -- that check lives at
/// hub-api's manifest/grant-time validation (`bundle_permission_catalog.
/// py::_cluster_deny_networks`), which runs before any grant this crate's
/// `GrantSnapshot` would ever serve; a defense-in-depth cluster-CIDR check
/// here is a follow-up once this crate gains a config-injection point for
/// per-deployment CIDRs.
fn is_always_denied_ip(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            v4.is_loopback() || v4.is_link_local() || *v4 == Ipv4Addr::new(169, 254, 169, 254)
        }
        IpAddr::V6(v6) => v6.is_loopback() || ipv6_is_link_local(v6) || *v6 == METADATA_V6,
    }
}

fn is_globally_routable(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            !v4.is_private()
                && !v4.is_loopback()
                && !v4.is_link_local()
                && !v4.is_broadcast()
                && !v4.is_documentation()
                && !v4.is_unspecified()
        }
        IpAddr::V6(v6) => {
            !v6.is_loopback()
                && !ipv6_is_link_local(v6)
                && !ipv6_is_unique_local(v6)
                && !v6.is_unspecified()
        }
    }
}

fn is_rfc1918_or_ula(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => v4.is_private(),
        IpAddr::V6(v6) => ipv6_is_unique_local(v6),
    }
}

/// Parses `<ip>` or `<ip>/<prefix>` -- a bare IP is treated as `/32` (v4) or
/// `/128` (v6), the full width, so the prefix-bound check below is trivially
/// satisfied for a single address.
fn parse_ip_and_prefix(value: &str) -> Option<(IpAddr, u8)> {
    match value.split_once('/') {
        Some((addr_str, prefix_str)) => {
            let addr: IpAddr = addr_str.parse().ok()?;
            let max_prefix = if addr.is_ipv4() { 32 } else { 128 };
            let prefix: u8 = prefix_str.parse().ok()?;
            if prefix > max_prefix {
                return None;
            }
            Some((addr, prefix))
        }
        None => {
            let addr: IpAddr = value.parse().ok()?;
            let full = if addr.is_ipv4() { 32 } else { 128 };
            Some((addr, full))
        }
    }
}

fn mask_ipv4(addr: Ipv4Addr, prefix: u8) -> Ipv4Addr {
    let bits = u32::from(addr);
    let mask: u32 = if prefix == 0 {
        0
    } else {
        !0u32 << (32 - prefix)
    };
    Ipv4Addr::from(bits & mask)
}

fn mask_ipv6(addr: Ipv6Addr, prefix: u8) -> Ipv6Addr {
    let bits = u128::from(addr);
    let mask: u128 = if prefix == 0 {
        0
    } else {
        !0u128 << (128 - prefix)
    };
    Ipv6Addr::from(bits & mask)
}

/// Android-style risk tier (spec SS1). `Normal` permissions are shown on
/// every consent screen but never block approval on a per-id ack; `Dangerous`
/// permissions require an explicit reviewer/admin ack at every tier that
/// sees them for the first time (spec SS3).
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum Risk {
    Normal,
    Dangerous,
}

/// Mirrors (but is deliberately independent of) `wit/waddle-bundle/
/// stage.wit`'s `CapabilityKind` variants plus this spec's new ones
/// (`Objects`, `Overlay`, `Ai`, `Users`, `Telemetry`, `Reputation`) --
/// audit/documentation metadata only. Reconciling this with the real WIT
/// binding type (`penguin_bundle_host::wire::CapabilityKind`) is the linker-
/// wiring integration task (spec SS12 Phase 4's `CapabilityHandler::handle`
/// item), not this crate, which has no dependency on `penguin-bundle-host`.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum CapabilityKind {
    Kv,
    Db,
    Objects,
    Http,
    Relay,
    Moderation,
    Overlay,
    Ai,
    Users,
    Telemetry,
    Reputation,
    Flags,
    Context,
    Clock,
    Log,
    /// `streaming.lifecycle.subscribe` -- read-only subscription to
    /// svc-streaming-rust's stream-lifecycle hooks (`wit/waddle-bundle/
    /// stage.wit`'s `streaming-lifecycle` export, issue #456). No
    /// enforcement mechanics live in this crate yet; host wiring is a
    /// separate implementation task.
    Streaming,
    /// `interaction.pii.receive` -- raw PII delivery in form/modal/
    /// interaction inputs. No enforcement mechanics live in this crate;
    /// the host's default-filter behavior when this is NOT granted is a
    /// separate implementation task.
    Interaction,
}

/// A permission's default quota shape (spec SS1's "Default quota" column).
/// Only the shapes this gate itself enforces on the hot path are numeric
/// ([`Quota::CallsPerWindow`], [`Quota::ReputationDelta`]) --
/// byte/row/object-count ceilings are [`Quota::Descriptive`]: real, but
/// enforced by the capability-specific implementation downstream of
/// `authorize()` (spec SS5.3's "post-authorize" layer), never by this gate.
#[derive(Clone, Debug, PartialEq)]
pub enum Quota {
    Unlimited,
    /// A simple rate limit: at most `max_calls` per `window` (e.g. `net.http`'s
    /// 10 rps, `overlay.media`'s 1/10s, `ai.generate`'s 10/min). A breach is
    /// [`crate::denied::Denied::RateLimited`].
    CallsPerWindow {
        max_calls: u32,
        window: Duration,
    },
    /// `reputation.*.write`'s three-part cap (spec SS1, SS7.3): a per-call
    /// magnitude bound, a per-target-user daily aggregate, and a per-
    /// community-or-tenant daily aggregate. A per-call breach is
    /// [`crate::denied::Denied::DeltaOutOfBounds`]; either aggregate breach
    /// is [`crate::denied::Denied::QuotaExceeded`].
    ReputationDelta {
        per_call_abs_max: i32,
        per_user_daily_abs_max: i64,
        per_scope_daily_abs_max: i64,
    },
    /// Real, but not numerically enforced by this gate -- see the type doc.
    Descriptive(&'static str),
}

/// The permission-id family -- the part of the catalog id before any `:`
/// parameter (spec SS1's id column, e.g. `net.http` for `net.http:<host>`).
/// Used as the catalog lookup key and for the AppScoped/ReputationScoped
/// split (spec SS5.2).
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum PermissionFamily {
    StorageKv,
    StorageTables,
    StorageObjects,
    /// `net.http.fqdn:<host>` -- the preferred, `Normal`-risk outbound-HTTP
    /// family (2026-09-28 decision). A bundle should reach for this family
    /// first; `NetHttpPublicIp`/`NetHttpPrivateIp` exist for the exceptional
    /// case where no stable hostname is available.
    NetHttpFqdn,
    /// `net.http.public-ip:<ip>` -- a bare, globally-routable IP with no
    /// FQDN. `Dangerous` (2026-09-28 refinement: an IP-literal target is
    /// opaque, rebinding/cache-poisoning-prone, and harder to audit than a
    /// hostname, regardless of whether the address itself is publicly
    /// routable).
    NetHttpPublicIp,
    /// `net.http.private-ip:<ip|cidr>` -- `Dangerous`, and additionally
    /// deny-by-default at the instance-policy layer (opt-in only) since this
    /// is the one family capable of naming the platform's own internal
    /// network. See [`crate::instance_policy`].
    NetHttpPrivateIp,
    ChatSend,
    Moderation,
    OverlayMedia,
    AiGenerate,
    UsersProfileRead,
    TelemetryLogs,
    TelemetryMetrics,
    ReputationRead,
    ReputationCommunityWrite,
    ReputationTenantWrite,
    FlagsRead,
    PlatformScheduled,
    PlatformContext,
    PlatformClock,
    PlatformLog,
    /// `interaction.pii.receive` -- raw PII (not just a tenant-tokenized
    /// UUID) embedded in a form/modal/interaction input the bundle
    /// receives. `Dangerous`, DEFAULT NO: without this granted, the host
    /// filters PII out of interaction inputs before delivery (best-effort;
    /// the host-side filter is separate implementation work, not this
    /// catalog entry). Subject to the instance-policy layer like any other
    /// family (spec SS1.3).
    InteractionPiiReceive,
    /// `streaming.lifecycle.subscribe` -- gates linking a component's
    /// `streaming-lifecycle` WIT export (issue #456). Catalog/permission
    /// definition only; host-side enforcement (actually registering the
    /// export per grant) is a later task.
    StreamingLifecycleSubscribe,
}

/// One row of the catalog table (spec SS1).
#[derive(Clone, Debug, PartialEq)]
pub struct CatalogEntry {
    pub family: PermissionFamily,
    pub risk: Risk,
    pub capability_kind: CapabilityKind,
    pub default_quota: Quota,
    /// Free-text from spec SS1's "Notes" column -- documentation only.
    pub notes: &'static str,
}

impl PermissionFamily {
    pub const ALL: &'static [PermissionFamily] = &[
        Self::StorageKv,
        Self::StorageTables,
        Self::StorageObjects,
        Self::NetHttpFqdn,
        Self::NetHttpPublicIp,
        Self::NetHttpPrivateIp,
        Self::ChatSend,
        Self::Moderation,
        Self::OverlayMedia,
        Self::AiGenerate,
        Self::UsersProfileRead,
        Self::TelemetryLogs,
        Self::TelemetryMetrics,
        Self::ReputationRead,
        Self::ReputationCommunityWrite,
        Self::ReputationTenantWrite,
        Self::FlagsRead,
        Self::PlatformScheduled,
        Self::PlatformContext,
        Self::PlatformClock,
        Self::PlatformLog,
        Self::InteractionPiiReceive,
        Self::StreamingLifecycleSubscribe,
    ];

    /// The static catalog id prefix -- for a parameterized family
    /// (`net.http`, `chat.send`, `moderation`) this is the part before `:`.
    pub fn id_prefix(&self) -> &'static str {
        match self {
            Self::StorageKv => "storage.kv",
            Self::StorageTables => "storage.tables",
            Self::StorageObjects => "storage.objects",
            Self::NetHttpFqdn => "net.http.fqdn",
            Self::NetHttpPublicIp => "net.http.public-ip",
            Self::NetHttpPrivateIp => "net.http.private-ip",
            Self::ChatSend => "chat.send",
            Self::Moderation => "moderation",
            Self::OverlayMedia => "overlay.media",
            Self::AiGenerate => "ai.generate",
            Self::UsersProfileRead => "users.profile.read",
            Self::TelemetryLogs => "telemetry.logs",
            Self::TelemetryMetrics => "telemetry.metrics",
            Self::ReputationRead => "reputation.read",
            Self::ReputationCommunityWrite => "reputation.community.write",
            Self::ReputationTenantWrite => "reputation.tenant.write",
            Self::FlagsRead => "flags.read",
            Self::PlatformScheduled => "platform.scheduled",
            Self::PlatformContext => "platform.context",
            Self::PlatformClock => "platform.clock",
            Self::PlatformLog => "platform.log",
            Self::InteractionPiiReceive => "interaction.pii.receive",
            Self::StreamingLifecycleSubscribe => "streaming.lifecycle.subscribe",
        }
    }

    /// `true` for the families whose ids carry a `:<param>` suffix
    /// (spec SS1: `net.http:<host>`, `chat.send:<platform>`,
    /// `moderation.<platform>` -- the last is dot-joined in the spec table
    /// but parsed identically to the colon-joined pair here for a single
    /// consistent grammar; see [`PermissionId::parse`]).
    pub fn is_parameterized(&self) -> bool {
        matches!(
            self,
            Self::NetHttpFqdn
                | Self::NetHttpPublicIp
                | Self::NetHttpPrivateIp
                | Self::ChatSend
                | Self::Moderation
        )
    }

    /// spec SS5.2: `AppScoped` permissions have their resource derived
    /// server-side from `(tenant, community, app_id)` alone.
    pub fn is_app_scoped(&self) -> bool {
        !self.is_reputation_scoped()
    }

    /// spec SS5.2 + SS10.2: `reputation.*` and `users.profile.read` share the
    /// identical target-user membership-check mechanism.
    pub fn is_reputation_scoped(&self) -> bool {
        matches!(
            self,
            Self::ReputationRead
                | Self::ReputationCommunityWrite
                | Self::ReputationTenantWrite
                | Self::UsersProfileRead
        )
    }

    /// Which [`crate::resource::AppScopedResource`] variant a capability
    /// implementation must pass for this (necessarily `AppScoped`) family --
    /// a mismatch (e.g. `storage.kv`'s call site accidentally passing
    /// `Table`) is a host-wiring bug `authorize()` rejects as
    /// `resource_scope_mismatch` rather than silently deriving the wrong
    /// resource. Meaningless for a `ReputationScoped` family; callers only
    /// consult this after `is_app_scoped()` is confirmed true.
    pub fn expected_app_scoped_resource(&self) -> crate::resource::AppScopedResource {
        use crate::resource::AppScopedResource;
        match self {
            Self::StorageKv => AppScopedResource::KvState,
            Self::StorageTables => AppScopedResource::Table,
            Self::StorageObjects => AppScopedResource::Objects,
            Self::OverlayMedia => AppScopedResource::Overlay,
            _ => AppScopedResource::None,
        }
    }

    pub fn catalog_entry(&self) -> CatalogEntry {
        match self {
            Self::StorageKv => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Kv,
                default_quota: Quota::Descriptive("64 KiB/value, 10k keys/app, KV_MAX_TTL_S"),
                notes: "Bundle's own ...:state hash -- grant is explicit, not implicit",
            },
            Self::StorageTables => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Db,
                default_quota: Quota::Descriptive("100k rows / 50 MB per (tenant, app)"),
                notes: "Exactly one table, in a server-derived schema (spec SS1.1)",
            },
            Self::StorageObjects => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Objects,
                default_quota: Quota::Descriptive("1k objects / 500 MB per (tenant, app)"),
                notes: "Tiered bucket/prefix topology, spec SS6",
            },
            Self::NetHttpFqdn => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Http,
                default_quota: Quota::CallsPerWindow {
                    max_calls: 10,
                    window: Duration::from_secs(1),
                },
                notes: "10 rps, 1 MB response -- the preferred net.http form",
            },
            Self::NetHttpPublicIp => CatalogEntry {
                family: *self,
                risk: Risk::Dangerous,
                capability_kind: CapabilityKind::Http,
                default_quota: Quota::CallsPerWindow {
                    max_calls: 10,
                    window: Duration::from_secs(1),
                },
                notes: "10 rps, 1 MB response; bare IP, no FQDN -- prefer net.http.fqdn",
            },
            Self::NetHttpPrivateIp => CatalogEntry {
                family: *self,
                risk: Risk::Dangerous,
                capability_kind: CapabilityKind::Http,
                default_quota: Quota::CallsPerWindow {
                    max_calls: 10,
                    window: Duration::from_secs(1),
                },
                notes: "10 rps, 1 MB response; deny-by-default at the instance-policy layer, \
                        loopback/link-local/metadata/cluster CIDRs always denied",
            },
            Self::ChatSend => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Relay,
                default_quota: Quota::Descriptive("existing relay rate limits (UsageBatcher)"),
                notes: "Action-stage only; outbound text is placeholder-only (spec SS10.4)",
            },
            Self::Moderation => CatalogEntry {
                family: *self,
                risk: Risk::Dangerous,
                capability_kind: CapabilityKind::Moderation,
                default_quota: Quota::Descriptive(
                    "existing relay-authz + UsageBatcher (wit-v1.1 SS2)",
                ),
                notes: "Subsumes wit-v1.1's bare `moderation` capability flag",
            },
            Self::OverlayMedia => CatalogEntry {
                family: *self,
                risk: Risk::Dangerous,
                capability_kind: CapabilityKind::Overlay,
                default_quota: Quota::CallsPerWindow {
                    max_calls: 1,
                    window: Duration::from_secs(10),
                },
                notes: "<=30s duration; answers the !vso shoutout requirement",
            },
            Self::AiGenerate => CatalogEntry {
                family: *self,
                risk: Risk::Dangerous,
                capability_kind: CapabilityKind::Ai,
                default_quota: Quota::CallsPerWindow {
                    max_calls: 10,
                    window: Duration::from_secs(60),
                },
                notes: "2k prompt / 512 completion tokens; Enterprise-gated at marketplace time (spec SS9), not here",
            },
            Self::UsersProfileRead => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Users,
                default_quota: Quota::Unlimited,
                notes: "Non-identifying attributes only (spec SS10.2)",
            },
            Self::TelemetryLogs => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Telemetry,
                default_quota: Quota::Descriptive("declared instruments only, per-instrument rate limit"),
                notes: "Mechanics owned by docs/bundle-telemetry-capability",
            },
            Self::TelemetryMetrics => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Telemetry,
                default_quota: Quota::Descriptive("declared instruments only, per-instrument rate limit"),
                notes: "Mechanics owned by docs/bundle-telemetry-capability",
            },
            Self::ReputationRead => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Reputation,
                default_quota: Quota::Unlimited,
                notes: "Own-scope + community/tenant leaderboard reads only -- never cross-tenant",
            },
            Self::ReputationCommunityWrite => CatalogEntry {
                family: *self,
                risk: Risk::Dangerous,
                capability_kind: CapabilityKind::Reputation,
                default_quota: Quota::ReputationDelta {
                    per_call_abs_max: 5,
                    per_user_daily_abs_max: 5,
                    per_scope_daily_abs_max: 50,
                },
                notes: "spec SS7",
            },
            Self::ReputationTenantWrite => CatalogEntry {
                family: *self,
                risk: Risk::Dangerous,
                capability_kind: CapabilityKind::Reputation,
                default_quota: Quota::ReputationDelta {
                    per_call_abs_max: 5,
                    per_user_daily_abs_max: 5,
                    per_scope_daily_abs_max: 200,
                },
                notes: "spec SS7; strictly a superset grant of community.write's bound",
            },
            Self::FlagsRead => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Flags,
                default_quota: Quota::Unlimited,
                notes: "Was \"always granted\" -- now explicit, still auto-approved",
            },
            Self::PlatformScheduled => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Context,
                default_quota: Quota::Descriptive("60s floor interval, existing tenant quota (wit-v1.1 SS1)"),
                notes: "hub-api CRUD only -- no CapabilityKind of its own",
            },
            Self::PlatformContext => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Context,
                default_quota: Quota::Unlimited,
                notes: "Always-granted, zero-config",
            },
            Self::PlatformClock => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Clock,
                default_quota: Quota::Unlimited,
                notes: "Always-granted, zero-config",
            },
            Self::PlatformLog => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Log,
                default_quota: Quota::Unlimited,
                notes: "Always-granted, zero-config",
            },
            Self::InteractionPiiReceive => CatalogEntry {
                family: *self,
                risk: Risk::Dangerous,
                capability_kind: CapabilityKind::Interaction,
                default_quota: Quota::Descriptive(
                    "no gate-enforced quota -- this permission gates raw-PII delivery, not a \
                     call rate; the host's default-filter behavior when NOT granted is \
                     enforced at delivery time, separate implementation work",
                ),
                notes: "DEFAULT NO; without this grant the host filters PII out of \
                        form/modal/interaction inputs (best-effort)",
            },
            Self::StreamingLifecycleSubscribe => CatalogEntry {
                family: *self,
                risk: Risk::Normal,
                capability_kind: CapabilityKind::Streaming,
                default_quota: Quota::Descriptive(
                    "no call-rate quota -- host-pushed callbacks only, not a bundle-initiated call",
                ),
                notes: "Gates linking the streaming-lifecycle WIT export (wit-stage-v1-1 C1, issue #456)",
            },
        }
    }
}

impl fmt::Display for PermissionFamily {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.id_prefix())
    }
}

/// A single validated permission id -- either a bare catalog id
/// (`storage.kv`) or a parameterized one (`net.http:api.example.com`).
/// Constructed only via [`PermissionId::parse`]/the family-specific
/// constructors, never by deserializing an arbitrary string straight from a
/// manifest without validation (that validation is `bundle_manifest_v2.py`'s
/// job at manifest-parse time, spec SS2.3 -- this type is the Rust-side
/// mirror of an already-validated id).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum PermissionId {
    StorageKv,
    StorageTables,
    StorageObjects,
    NetHttpFqdn(String),
    NetHttpPublicIp(String),
    NetHttpPrivateIp(String),
    ChatSend(String),
    Moderation(String),
    OverlayMedia,
    AiGenerate,
    UsersProfileRead,
    TelemetryLogs,
    TelemetryMetrics,
    ReputationRead,
    ReputationCommunityWrite,
    ReputationTenantWrite,
    FlagsRead,
    PlatformScheduled,
    PlatformContext,
    PlatformClock,
    PlatformLog,
    InteractionPiiReceive,
    StreamingLifecycleSubscribe,
}

/// Platforms compiled into this build's relay/moderation providers (spec
/// SS1: "`<platform>` in compiled-in providers (`twitch`, `discord`, ...)").
/// Mirrors the provider sets already live in `hub_api` (`SUPPORTED_PLATFORMS`/
/// `RELAY_PROVIDERS`); kept as a plain allowlist here rather than an enum so
/// a new platform lands as a one-line addition, not a breaking Rust API
/// change in every crate that matches on [`PermissionId`].
pub const COMPILED_IN_PLATFORMS: &[&str] = &["twitch", "kick", "youtube", "discord", "slack"];

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ParsePermissionIdError {
    #[error("unknown_permission: {0:?} matches no entry in the closed catalog")]
    UnknownPermission(String),
    #[error("unknown_permission: {family} requires a non-empty :<param> suffix")]
    MissingParam { family: &'static str },
    #[error("unsupported_platform: {0:?} is not a compiled-in provider")]
    UnsupportedPlatform(String),
    #[error(
        "net.http.fqdn host {0:?} is not a valid, non-wildcard hostname (or is an IP literal)"
    )]
    InvalidHost(String),
    #[error("net.http.public-ip {0:?} is not a single, globally-routable IP address")]
    InvalidPublicIp(String),
    #[error(
        "net.http.private-ip {0:?} is not a private IP/CIDR within the allowed prefix bound, \
         or overlaps an always-denied range (loopback/link-local/metadata/cluster CIDR)"
    )]
    InvalidPrivateIp(String),
}

impl PermissionId {
    pub fn family(&self) -> PermissionFamily {
        match self {
            Self::StorageKv => PermissionFamily::StorageKv,
            Self::StorageTables => PermissionFamily::StorageTables,
            Self::StorageObjects => PermissionFamily::StorageObjects,
            Self::NetHttpFqdn(_) => PermissionFamily::NetHttpFqdn,
            Self::NetHttpPublicIp(_) => PermissionFamily::NetHttpPublicIp,
            Self::NetHttpPrivateIp(_) => PermissionFamily::NetHttpPrivateIp,
            Self::ChatSend(_) => PermissionFamily::ChatSend,
            Self::Moderation(_) => PermissionFamily::Moderation,
            Self::OverlayMedia => PermissionFamily::OverlayMedia,
            Self::AiGenerate => PermissionFamily::AiGenerate,
            Self::UsersProfileRead => PermissionFamily::UsersProfileRead,
            Self::TelemetryLogs => PermissionFamily::TelemetryLogs,
            Self::TelemetryMetrics => PermissionFamily::TelemetryMetrics,
            Self::ReputationRead => PermissionFamily::ReputationRead,
            Self::ReputationCommunityWrite => PermissionFamily::ReputationCommunityWrite,
            Self::ReputationTenantWrite => PermissionFamily::ReputationTenantWrite,
            Self::FlagsRead => PermissionFamily::FlagsRead,
            Self::PlatformScheduled => PermissionFamily::PlatformScheduled,
            Self::PlatformContext => PermissionFamily::PlatformContext,
            Self::PlatformClock => PermissionFamily::PlatformClock,
            Self::PlatformLog => PermissionFamily::PlatformLog,
            Self::InteractionPiiReceive => PermissionFamily::InteractionPiiReceive,
            Self::StreamingLifecycleSubscribe => PermissionFamily::StreamingLifecycleSubscribe,
        }
    }

    pub fn risk(&self) -> Risk {
        self.family().catalog_entry().risk
    }

    pub fn default_quota(&self) -> Quota {
        self.family().catalog_entry().default_quota
    }

    /// The exact string stored in `community_permission_grants.permission_id`
    /// / `app_permission_requests.permission_id` (spec SS4) -- the
    /// `GrantSnapshot`/`GrantCache` lookup key.
    pub fn canonical_id(&self) -> String {
        match self {
            Self::NetHttpFqdn(host) => format!("net.http.fqdn:{host}"),
            Self::NetHttpPublicIp(ip) => format!("net.http.public-ip:{ip}"),
            Self::NetHttpPrivateIp(value) => format!("net.http.private-ip:{value}"),
            Self::ChatSend(platform) => format!("chat.send:{platform}"),
            Self::Moderation(platform) => format!("moderation.{platform}"),
            other => other.family().id_prefix().to_string(),
        }
    }

    fn validate_platform(platform: &str) -> Result<(), ParsePermissionIdError> {
        if COMPILED_IN_PLATFORMS.contains(&platform) {
            Ok(())
        } else {
            Err(ParsePermissionIdError::UnsupportedPlatform(
                platform.to_string(),
            ))
        }
    }

    /// `net.http.fqdn` only -- rejects a wildcard (already excluded by the
    /// charset below), an IP literal (must use `net.http.public-ip`/
    /// `net.http.private-ip` instead), and anything else not shaped like a
    /// DNS hostname.
    fn validate_host(host: &str) -> Result<(), ParsePermissionIdError> {
        let shape_valid = !host.is_empty()
            && host.len() <= 253
            && host
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-')
            && !host.starts_with('.')
            && !host.starts_with('-')
            && !host.ends_with('.')
            && !host.ends_with('-');
        if !shape_valid || host.parse::<IpAddr>().is_ok() {
            return Err(ParsePermissionIdError::InvalidHost(host.to_string()));
        }
        Ok(())
    }

    fn validate_public_ip(value: &str) -> Result<(), ParsePermissionIdError> {
        let err = || ParsePermissionIdError::InvalidPublicIp(value.to_string());
        if value.contains('/') {
            return Err(err()); // never a CIDR
        }
        let addr: IpAddr = value.parse().map_err(|_| err())?;
        if is_always_denied_ip(&addr) || !is_globally_routable(&addr) {
            return Err(err());
        }
        Ok(())
    }

    fn validate_private_ip_or_cidr(value: &str) -> Result<(), ParsePermissionIdError> {
        let err = || ParsePermissionIdError::InvalidPrivateIp(value.to_string());
        let (addr, prefix) = parse_ip_and_prefix(value).ok_or_else(err)?;
        let network = match addr {
            IpAddr::V4(v4) => {
                if prefix < MAX_PRIVATE_PREFIX_V4 {
                    return Err(err());
                }
                IpAddr::V4(mask_ipv4(v4, prefix))
            }
            IpAddr::V6(v6) => {
                if prefix < MAX_PRIVATE_PREFIX_V6 {
                    return Err(err());
                }
                IpAddr::V6(mask_ipv6(v6, prefix))
            }
        };
        if is_always_denied_ip(&network) || !is_rfc1918_or_ula(&network) {
            return Err(err());
        }
        Ok(())
    }

    /// Parses a catalog id string (`storage.kv`, `net.http.fqdn:
    /// api.example.com`, `chat.send:discord`, `moderation.twitch`) against
    /// the closed catalog (spec SS2.3) -- an id matching no entry is
    /// `unknown_permission`, and a parameterized family with an unrecognized
    /// platform is `unsupported_platform`, exactly the two denial-reason
    /// strings spec SS5.4 defines for these cases.
    pub fn parse(raw: &str) -> Result<Self, ParsePermissionIdError> {
        if let Some(host) = raw.strip_prefix("net.http.fqdn:") {
            Self::validate_host(host)?;
            return Ok(Self::NetHttpFqdn(host.to_string()));
        }
        if let Some(ip) = raw.strip_prefix("net.http.public-ip:") {
            Self::validate_public_ip(ip)?;
            return Ok(Self::NetHttpPublicIp(ip.to_string()));
        }
        if let Some(value) = raw.strip_prefix("net.http.private-ip:") {
            Self::validate_private_ip_or_cidr(value)?;
            return Ok(Self::NetHttpPrivateIp(value.to_string()));
        }
        if let Some(platform) = raw.strip_prefix("chat.send:") {
            Self::validate_platform(platform)?;
            return Ok(Self::ChatSend(platform.to_string()));
        }
        if let Some(platform) = raw.strip_prefix("moderation.") {
            Self::validate_platform(platform)?;
            return Ok(Self::Moderation(platform.to_string()));
        }
        if raw == "net.http.fqdn" || raw == "net.http.public-ip" || raw == "net.http.private-ip" {
            return Err(ParsePermissionIdError::MissingParam {
                family: match raw {
                    "net.http.fqdn" => "net.http.fqdn",
                    "net.http.public-ip" => "net.http.public-ip",
                    _ => "net.http.private-ip",
                },
            });
        }
        if raw == "chat.send" {
            return Err(ParsePermissionIdError::MissingParam {
                family: "chat.send",
            });
        }

        match raw {
            "storage.kv" => Ok(Self::StorageKv),
            "storage.tables" => Ok(Self::StorageTables),
            "storage.objects" => Ok(Self::StorageObjects),
            "overlay.media" => Ok(Self::OverlayMedia),
            "ai.generate" => Ok(Self::AiGenerate),
            "users.profile.read" => Ok(Self::UsersProfileRead),
            "telemetry.logs" => Ok(Self::TelemetryLogs),
            "telemetry.metrics" => Ok(Self::TelemetryMetrics),
            "reputation.read" => Ok(Self::ReputationRead),
            "reputation.community.write" => Ok(Self::ReputationCommunityWrite),
            "reputation.tenant.write" => Ok(Self::ReputationTenantWrite),
            "flags.read" => Ok(Self::FlagsRead),
            "platform.scheduled" => Ok(Self::PlatformScheduled),
            "platform.context" => Ok(Self::PlatformContext),
            "platform.clock" => Ok(Self::PlatformClock),
            "platform.log" => Ok(Self::PlatformLog),
            "interaction.pii.receive" => Ok(Self::InteractionPiiReceive),
            "streaming.lifecycle.subscribe" => Ok(Self::StreamingLifecycleSubscribe),
            other => Err(ParsePermissionIdError::UnknownPermission(other.to_string())),
        }
    }
}

impl fmt::Display for PermissionId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.canonical_id())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_family_has_a_catalog_entry_with_a_matching_family_field() {
        for family in PermissionFamily::ALL {
            let entry = family.catalog_entry();
            assert_eq!(entry.family, *family);
        }
    }

    #[test]
    fn parse_round_trips_every_bare_catalog_id() {
        for family in PermissionFamily::ALL {
            if family.is_parameterized() {
                continue;
            }
            let id = family.id_prefix();
            let parsed = PermissionId::parse(id).unwrap_or_else(|e| panic!("{id}: {e}"));
            assert_eq!(parsed.canonical_id(), id);
        }
    }

    #[test]
    fn interaction_pii_receive_is_dangerous_and_round_trips() {
        let id = PermissionId::parse("interaction.pii.receive").unwrap();
        assert_eq!(id.canonical_id(), "interaction.pii.receive");
        assert_eq!(id.family(), PermissionFamily::InteractionPiiReceive);
        assert_eq!(id.risk(), Risk::Dangerous);
        assert_eq!(
            id.family().catalog_entry().capability_kind,
            CapabilityKind::Interaction
        );
    }

    #[test]
    fn streaming_lifecycle_subscribe_is_normal_risk_and_round_trips() {
        let id = PermissionId::parse("streaming.lifecycle.subscribe").unwrap();
        assert_eq!(id.canonical_id(), "streaming.lifecycle.subscribe");
        assert_eq!(id.family(), PermissionFamily::StreamingLifecycleSubscribe);
        assert_eq!(id.risk(), Risk::Normal);
        assert_eq!(
            id.family().catalog_entry().capability_kind,
            CapabilityKind::Streaming
        );
        assert_eq!(
            PermissionFamily::StreamingLifecycleSubscribe.expected_app_scoped_resource(),
            crate::resource::AppScopedResource::None
        );
    }

    #[test]
    fn parse_accepts_a_compiled_in_net_http_fqdn_host() {
        let id = PermissionId::parse("net.http.fqdn:api.weatherapi.example.com").unwrap();
        assert_eq!(
            id.canonical_id(),
            "net.http.fqdn:api.weatherapi.example.com"
        );
        assert_eq!(id.family(), PermissionFamily::NetHttpFqdn);
        assert_eq!(id.risk(), Risk::Normal);
    }

    #[test]
    fn parse_rejects_an_invalid_net_http_fqdn_host() {
        let err = PermissionId::parse("net.http.fqdn:not a host!").unwrap_err();
        assert!(matches!(err, ParsePermissionIdError::InvalidHost(_)));
    }

    #[test]
    fn parse_rejects_a_wildcard_net_http_fqdn_host() {
        let err = PermissionId::parse("net.http.fqdn:*.example.com").unwrap_err();
        assert!(matches!(err, ParsePermissionIdError::InvalidHost(_)));
    }

    #[test]
    fn parse_rejects_an_ip_literal_as_net_http_fqdn() {
        let err = PermissionId::parse("net.http.fqdn:93.184.216.34").unwrap_err();
        assert!(matches!(err, ParsePermissionIdError::InvalidHost(_)));
    }

    #[test]
    fn parse_accepts_a_globally_routable_net_http_public_ip() {
        let id = PermissionId::parse("net.http.public-ip:93.184.216.34").unwrap();
        assert_eq!(id.canonical_id(), "net.http.public-ip:93.184.216.34");
        assert_eq!(id.family(), PermissionFamily::NetHttpPublicIp);
        assert_eq!(id.risk(), Risk::Dangerous);
    }

    #[test]
    fn parse_rejects_a_cidr_as_net_http_public_ip() {
        let err = PermissionId::parse("net.http.public-ip:93.184.216.0/24").unwrap_err();
        assert!(matches!(err, ParsePermissionIdError::InvalidPublicIp(_)));
    }

    #[test]
    fn parse_rejects_a_private_address_as_net_http_public_ip() {
        let err = PermissionId::parse("net.http.public-ip:10.0.0.5").unwrap_err();
        assert!(matches!(err, ParsePermissionIdError::InvalidPublicIp(_)));
    }

    #[test]
    fn parse_rejects_loopback_and_metadata_as_net_http_public_ip() {
        assert!(PermissionId::parse("net.http.public-ip:127.0.0.1").is_err());
        assert!(PermissionId::parse("net.http.public-ip:169.254.169.254").is_err());
    }

    #[test]
    fn parse_accepts_a_private_ip_within_the_prefix_bound() {
        let id = PermissionId::parse("net.http.private-ip:10.20.0.0/16").unwrap();
        assert_eq!(id.canonical_id(), "net.http.private-ip:10.20.0.0/16");
        assert_eq!(id.family(), PermissionFamily::NetHttpPrivateIp);
        assert_eq!(id.risk(), Risk::Dangerous);
    }

    #[test]
    fn parse_rejects_a_private_ip_coarser_than_the_prefix_bound() {
        let err = PermissionId::parse("net.http.private-ip:10.0.0.0/8").unwrap_err();
        assert!(matches!(err, ParsePermissionIdError::InvalidPrivateIp(_)));
    }

    #[test]
    fn parse_rejects_a_public_address_as_net_http_private_ip() {
        let err = PermissionId::parse("net.http.private-ip:93.184.216.34").unwrap_err();
        assert!(matches!(err, ParsePermissionIdError::InvalidPrivateIp(_)));
    }

    #[test]
    fn parse_rejects_loopback_link_local_and_metadata_as_net_http_private_ip() {
        assert!(PermissionId::parse("net.http.private-ip:127.0.0.1").is_err());
        assert!(PermissionId::parse("net.http.private-ip:127.0.0.0/24").is_err());
        assert!(PermissionId::parse("net.http.private-ip:169.254.169.254").is_err());
    }

    #[test]
    fn parse_accepts_a_private_ipv6_ula_within_the_prefix_bound() {
        assert!(PermissionId::parse("net.http.private-ip:fd12:3456:789a::/64").is_ok());
        assert!(PermissionId::parse("net.http.private-ip:fd12:3456:789a::/48").is_err());
    }

    #[test]
    fn parse_accepts_a_compiled_in_chat_send_platform() {
        let id = PermissionId::parse("chat.send:discord").unwrap();
        assert_eq!(id.canonical_id(), "chat.send:discord");
    }

    #[test]
    fn parse_rejects_an_uncompiled_chat_send_platform() {
        let err = PermissionId::parse("chat.send:myspace").unwrap_err();
        assert_eq!(
            err,
            ParsePermissionIdError::UnsupportedPlatform("myspace".to_string())
        );
    }

    #[test]
    fn parse_accepts_a_compiled_in_moderation_platform() {
        let id = PermissionId::parse("moderation.twitch").unwrap();
        assert_eq!(id.canonical_id(), "moderation.twitch");
    }

    /// The catalog is closed (spec SS2.3) -- an id resembling the grammar
    /// but naming nothing in the catalog must be `unknown_permission`, never
    /// silently accepted.
    #[test]
    fn parse_rejects_an_unknown_permission_id() {
        let err = PermissionId::parse("storage.unlimited_everything").unwrap_err();
        assert_eq!(
            err,
            ParsePermissionIdError::UnknownPermission("storage.unlimited_everything".to_string())
        );
    }

    #[test]
    fn parse_rejects_a_bare_parameterized_family_with_no_param() {
        let err = PermissionId::parse("net.http.fqdn").unwrap_err();
        assert_eq!(
            err,
            ParsePermissionIdError::MissingParam {
                family: "net.http.fqdn"
            }
        );
    }

    #[test]
    fn reputation_tenant_write_is_a_superset_of_community_write_per_scope_cap() {
        let community = PermissionFamily::ReputationCommunityWrite.catalog_entry();
        let tenant = PermissionFamily::ReputationTenantWrite.catalog_entry();
        let (
            Quota::ReputationDelta {
                per_scope_daily_abs_max: c,
                ..
            },
            Quota::ReputationDelta {
                per_scope_daily_abs_max: t,
                ..
            },
        ) = (community.default_quota, tenant.default_quota)
        else {
            panic!("expected ReputationDelta quotas");
        };
        assert!(
            t >= c,
            "tenant.write's aggregate ceiling must never be looser (spec SS1)"
        );
    }

    #[test]
    fn expected_app_scoped_resource_maps_storage_and_overlay_families_distinctly() {
        use crate::resource::AppScopedResource;
        assert_eq!(
            PermissionFamily::StorageKv.expected_app_scoped_resource(),
            AppScopedResource::KvState
        );
        assert_eq!(
            PermissionFamily::StorageTables.expected_app_scoped_resource(),
            AppScopedResource::Table
        );
        assert_eq!(
            PermissionFamily::StorageObjects.expected_app_scoped_resource(),
            AppScopedResource::Objects
        );
        assert_eq!(
            PermissionFamily::OverlayMedia.expected_app_scoped_resource(),
            AppScopedResource::Overlay
        );
        assert_eq!(
            PermissionFamily::NetHttpFqdn.expected_app_scoped_resource(),
            AppScopedResource::None
        );
        assert_eq!(
            PermissionFamily::FlagsRead.expected_app_scoped_resource(),
            AppScopedResource::None
        );
    }

    #[test]
    fn app_scoped_and_reputation_scoped_partition_every_family() {
        for family in PermissionFamily::ALL {
            assert_ne!(family.is_app_scoped(), family.is_reputation_scoped());
        }
    }
}
