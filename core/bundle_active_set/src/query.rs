//! The active-set read and its cheap watermark pre-check (spec: "poll a
//! cheap CHANGE-WATERMARK first and only do the full active-set re-read
//! ... when it moves"). Both queries are scoped to one `(tenant_id,
//! community_id)` pair and, for [`read_active_set`], an optional single
//! `app_id` -- callers pass `None` to manage the whole scope's active set
//! (spec's multi-app requirement) or `Some(app_id)` to scope to one bundle.

use sea_orm::{ColumnTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter, QueryOrder};
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::entities::{
    app_active_versions, app_install_approvals, app_source_bindings, app_versions,
};

#[derive(Debug, Error)]
pub enum ActiveSetError {
    #[error("database query failed: {0}")]
    Db(#[from] DbErr),
}

/// One ACTIVE, APPROVED bundle version in scope -- the row shape
/// [`crate::diff`] and each service's own executor `load`/`unload` wiring
/// consume.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActiveBundleRow {
    pub app_id: String,
    pub version: String,
    pub digest: String,
    pub component_key: String,
    pub sidecar_key: String,
    /// Artifact-signature columns (spec SS5.6/Gemini review condition 9,
    /// migration `0040_bundle_artifact_signature`) -- surfaced here for
    /// observability/audit only. The AUTHORITATIVE check is
    /// `core/bundle_executor/src/signing.rs`'s verification of the signed
    /// `.json` sidecar fetched from the bucket at `sidecar_key`, not a
    /// comparison against these columns directly (see
    /// `entities::app_versions`'s module doc for why: the wire protocol's
    /// `LoadBody` has no field to carry them to the executor).
    pub artifact_signature: Option<String>,
    pub artifact_signature_key_id: Option<String>,
    pub artifact_signed_approval_id: Option<i64>,
    /// The install-time consent summary's derived `"capabilities"` array
    /// (`app_install_approvals.summary_json`, `crate::entities::
    /// app_install_approvals`'s doc) -- e.g. `["context","kv","flags",
    /// "log","clock","http"]`. Each service's own `bundle_loader` folds
    /// this into a per-`app_id` declared-capability snapshot
    /// (`bundle_host_kv::authorize::CapabilitySnapshot`) that
    /// `authorize_kv` checks before granting `kv` (coordinator fix on PR
    /// #425: "undeclared means denied", no more unconditional grant).
    /// Empty (never `None`) if `summary_json` was missing the key, was not
    /// an array of strings, or failed to parse -- a malformed/absent
    /// summary denies every capability it might have granted, the correct
    /// fail-closed direction for a consent record this crate cannot
    /// validate further than "is this valid JSON shaped like the summary
    /// schema".
    pub declared_capabilities: Vec<String>,
}

/// Extracts `summary_json.capabilities` (a JSON array of strings) as a
/// plain `Vec<String>`, or `vec![]` for any malformed/missing shape (this
/// function's own doc: fail closed, never fail the whole active-set read
/// over one bundle's malformed consent record).
fn declared_capabilities_from_summary(summary_json: &sea_orm::JsonValue) -> Vec<String> {
    summary_json
        .get("capabilities")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// A cheap change signal for one `(tenant_id, community_id)` scope: a
/// SHA-256 fingerprint over every `(app_id, version_id)` pair currently
/// active in that scope, sorted by `app_id` for a deterministic byte
/// sequence regardless of the row order Postgres happens to return.
///
/// **Not `COUNT(*) + SUM(version_id)` (security review fix -- that
/// approach silently cancels):** if app A's `version_id` decreases by N in
/// the same tick app B's increases by N, both the count and the sum stay
/// identical, so that watermark would never move and the hot-swap would
/// miss the change until an unrelated row nudged the sum back out of
/// coincidental alignment -- proven by
/// `read_watermark_moves_when_two_rows_shift_by_equal_and_opposite_amounts`
/// below, which is the exact regression this type exists to prevent. A
/// cryptographic hash over every row's *own* identity (not an aggregate
/// that discards which row changed) cannot cancel this way: two distinct
/// active sets hashing to the same digest would require a genuine SHA-256
/// collision, not a coincidental arithmetic identity.
///
/// Also sidesteps the `count`/`version_sum` design's other documented
/// concern: `app_active_versions` has no `updated_at` column to fall back
/// to (only `activated_at`, `DEFAULT NOW()` on INSERT only, with no
/// confirmed guarantee a future activation-service's rollback UPDATE also
/// bumps it) -- this fingerprint depends on neither column.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct Watermark(String);

/// Reads [`Watermark`] for `(tenant_id, community_id)` -- two queries
/// (`app_active_versions` and `app_source_bindings`, no join), small
/// result sets (bounded by the number of apps/bindings active in this one
/// scope). Cheap enough to run every poll tick even when nothing has
/// changed; still meaningfully cheaper than [`read_active_set`], which
/// additionally joins `app_versions` and `app_install_approvals`.
///
/// `app_source_bindings` rows are folded into the same fingerprint as
/// `app_active_versions` (rather than a second, independent watermark) so
/// a binding add/remove alone -- with no `app_active_versions` change at
/// all -- still moves the one watermark both `crate::query::read_active_set`
/// callers (bundle load/unload) and `svc_process`'s source-binding
/// supervisor poll against; the two callers already tolerate reading a
/// larger row set than they individually need (mirrors `read_active_set`
/// itself, which every caller re-reads in full on any scope-wide change).
pub async fn read_watermark(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
) -> Result<Watermark, ActiveSetError> {
    let rows = app_active_versions::Entity::find()
        .filter(app_active_versions::Column::TenantId.eq(tenant_id))
        .filter(app_active_versions::Column::CommunityId.eq(community_id))
        .order_by_asc(app_active_versions::Column::AppId)
        .all(conn)
        .await?;

    let mut hasher = Sha256::new();
    for row in &rows {
        // `app_id` is the natural unique key within this one `(tenant_id,
        // community_id)` scope (the table's own PK), so sorting by it
        // alone -- no secondary sort needed -- yields a deterministic
        // sequence. A `\0`/`\n` separator between fields/rows prevents a
        // pathological app_id boundary shift (e.g. "ab"+"1" vs "a"+"b1")
        // from ever producing the same byte stream for two different sets.
        hasher.update(row.app_id.as_bytes());
        hasher.update(b"\0");
        hasher.update(row.version_id.to_le_bytes());
        hasher.update(b"\n");
    }

    let mut binding_rows = app_source_bindings::Entity::find()
        .filter(app_source_bindings::Column::TenantId.eq(tenant_id))
        .filter(app_source_bindings::Column::CommunityId.eq(community_id))
        .all(conn)
        .await?;
    // `(app_id, platform, source_id)` is the table's own composite PK
    // (alongside tenant/community, both already fixed by this scope), so
    // sorting by all three -- in Rust, not `order_by_asc` chaining, kept
    // simple since this list is always small -- yields the same
    // deterministic-sequence guarantee `app_active_versions`'s own
    // `app_id`-only sort relies on above.
    binding_rows.sort_by(|a, b| {
        (a.app_id.as_str(), a.platform.as_str(), a.source_id.as_str()).cmp(&(
            b.app_id.as_str(),
            b.platform.as_str(),
            b.source_id.as_str(),
        ))
    });
    for row in &binding_rows {
        hasher.update(row.app_id.as_bytes());
        hasher.update(b"\0");
        hasher.update(row.platform.as_bytes());
        hasher.update(b"\0");
        hasher.update(row.source_id.as_bytes());
        hasher.update(b"\n");
    }

    Ok(Watermark(format!("{:x}", hasher.finalize())))
}

/// Content-addressed `component_key`/`sidecar_key` derivation --
/// **defensive fallback only**, used by [`read_active_set`] exclusively
/// when `app_versions.component_key` is `NULL` (a row published before the
/// column-adding migration and/or hub-api's publish-step backfill landed;
/// see `crate::entities::app_versions`'s module doc for the full
/// contract). No longer the primary path -- `read_active_set` reads the
/// real `component_key`/`sidecar_key` columns directly when set. `digest`
/// is the `sha256:<64 hex>` `artifact_digest`; this strips the `sha256:`
/// prefix and keys both objects by the raw hex digest under `bundles/`,
/// the same convention this crate used before the real columns existed --
/// kept only so an un-backfilled row still loads instead of being dropped.
pub fn derive_component_keys(digest: &str) -> (String, String) {
    let hex = digest.strip_prefix("sha256:").unwrap_or(digest);
    (
        format!("bundles/{hex}/component.wasm"),
        format!("bundles/{hex}/sidecar.json"),
    )
}

/// A digest that is neither bare 64-hex nor `sha256:<64 hex>` -- see
/// [`canonical_digest`]'s doc for why this is a typed, fail-loud error
/// rather than a silent pass-through.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("malformed digest {0:?}: expected 64 hex chars, optionally prefixed with \"sha256:\"")]
pub struct DigestError(pub String);

/// Canonicalizes a digest to the `sha256:<64 lowercase hex>` form the
/// executor's `Load`/`Unload`/`Invoke` wire protocol requires
/// (`core/bundle_executor/src/invoke.rs::verify_digest`'s own parse:
/// `strip_prefix("sha256:")`, `hex.len() == 64`, `is_ascii_hexdigit`) --
/// this is a deliberate, commented duplicate of that validation, not an
/// independent reimplementation; `bundle_executor` pulls in `wasmtime`, so
/// it cannot be a dependency of this crate or of `svc_process`/`svc_action`
/// without pulling `wasmtime` into their `cargo deny` scope (an earlier
/// attempt did exactly that and broke `svc_action`'s deny gate).
///
/// **The one normalization point for every digest crossing the svc<->
/// executor boundary** -- [`assemble_active_set`] is the sole caller, so
/// every `ActiveBundleRow::digest` this crate ever produces (consumed by
/// both `core/svc_process` and `core/svc_action`'s `bundle_loader::
/// ExecutorSink::load`/`unload`, their `loaded: HashMap<app_id, digest>`
/// state, and `crate::diff::plan`'s digest-equality check) is already
/// canonical. `app_versions.artifact_digest` is stored as bare 64-hex by
/// the control plane -- the legacy `PROCESS_BUNDLE_DIGEST`/
/// `ACTION_BUNDLE_DIGEST` env path has always sent the `sha256:`-prefixed
/// form directly to the executor without going through this crate at all,
/// which is why the mismatch never surfaced until the DB-driven path
/// shipped. Accepts either input form so both are idempotently normalized;
/// lowercases hex so a canonical-form comparison never misses a match over
/// case alone. Storage-key derivation ([`derive_component_keys`]) keeps
/// using the bare-hex form it has always used -- it strips any `sha256:`
/// prefix itself, so feeding it this function's canonical (prefixed) output
/// is unaffected.
///
/// regression: DB bare-hex digest rejected by executor Load (malformed
/// digest), UnknownBundle (alpha 2026-10-03)
pub fn canonical_digest(input: &str) -> Result<String, DigestError> {
    let hex = input.strip_prefix("sha256:").unwrap_or(input);
    if hex.len() != 64 || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(DigestError(input.to_string()));
    }
    Ok(format!("sha256:{}", hex.to_ascii_lowercase()))
}

/// Why one `app_active_versions` row was excluded from
/// [`ActiveSetRead::rows`] -- ops-visibility fix (security review):
/// exclusion used to be a `debug!`/`warn!` log line only, easy to miss when
/// a feature silently goes dark (e.g. an approval expiring/getting
/// superseded with nothing re-approving it). Returned alongside the rows
/// so each service's own `bundle_loader` can drive a Prometheus counter
/// from it (`telemetry::register_bundle_loader_excluded_metrics`), not
/// just a log line.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum ExclusionReason {
    /// `app_active_versions.version_id` points at no row in `app_versions`
    /// at all -- a referential-integrity gap, never expected in a healthy
    /// system.
    MissingVersionRow,
    /// No current (`superseded_by IS NULL`) `app_install_approvals` row
    /// matches -- the version is active but not (or no longer) approved.
    NoApproval,
    /// The `app_versions` row has no `artifact_digest` yet (not published,
    /// or a race with a concurrent publish).
    MissingDigest,
    /// `app_versions.artifact_digest` failed [`canonical_digest`] -- neither
    /// bare 64-hex nor `sha256:<64 hex>`. Logged at `ERROR` (not `warn!`
    /// like the other reasons) by [`assemble_active_set`): a malformed
    /// digest the control plane itself wrote is a data-integrity bug, not
    /// routine rollout/approval-lifecycle noise. Logged by
    /// [`assemble_active_set`].
    MalformedDigest,
}

impl ExclusionReason {
    /// Stable label value for the Prometheus counter -- never the `Debug`
    /// form, which is not a contract callers should depend on.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::MissingVersionRow => "missing_version_row",
            Self::NoApproval => "no_approval",
            Self::MissingDigest => "missing_digest",
            Self::MalformedDigest => "malformed_digest",
        }
    }
}

/// Why one row's `component_key`/`sidecar_key` came from
/// [`derive_component_keys`]'s fallback rather than the real
/// `app_versions` columns -- rollout visibility (component_key contract):
/// the row still loads (never excluded), but an un-backfilled row is a
/// signal worth surfacing, not silently patched over. Shares the same
/// `(app_id, reason)` shape and Prometheus label space as
/// [`ExclusionReason`] (both plug into the same
/// `bundle_active_set_excluded_total`-style counter via `as_str`), kept as
/// a separate type since a degraded row is a distinct condition from an
/// excluded one.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum DegradedReason {
    /// `app_versions.component_key` is `NULL` -- published before the
    /// column-adding migration and/or hub-api's publish-step backfill
    /// landed. Expected during rollout, not a steady-state condition.
    MissingComponentKey,
}

impl DegradedReason {
    /// Stable label value for the Prometheus counter -- same convention as
    /// [`ExclusionReason::as_str`].
    pub fn as_str(self) -> &'static str {
        match self {
            Self::MissingComponentKey => "missing_component_key",
        }
    }
}

/// [`read_active_set`]'s full result: the ACTIVE+APPROVED rows to load,
/// every row this tick excluded and why (see [`ExclusionReason`]'s doc),
/// and every row that loaded but via [`derive_component_keys`]'s fallback
/// rather than a real `component_key` column value (see
/// [`DegradedReason`]'s doc) -- exclusions and degradations are both data,
/// never just a log line.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ActiveSetRead {
    pub rows: Vec<ActiveBundleRow>,
    pub excluded: Vec<(String, ExclusionReason)>,
    pub degraded: Vec<(String, DegradedReason)>,
}

/// Reads the full ACTIVE, APPROVED set for `(tenant_id, community_id)`,
/// optionally narrowed to one `app_id`. Three sequential queries (no
/// SeaORM `Relation` wiring -- see `crate::entities`'s module doc) joined
/// in Rust:
///
/// 1. `app_active_versions` rows in scope (the ACTIVE half).
/// 2. `app_versions` rows for the resulting `version_id`s (the digest).
/// 3. `app_install_approvals` current (`superseded_by IS NULL`) rows for
///    the resulting `(app_id, version)` pairs in this tenant (the
///    APPROVED half) -- matched against each active row's own
///    `community_id` per the sentinel-mismatch rule documented on
///    `crate::entities::app_install_approvals`.
///
/// A version with no current approval, or an `app_versions` row with no
/// `artifact_digest` set (not yet published, or a data race with a
/// concurrent publish), is excluded from [`ActiveSetRead::rows`] (recorded
/// in [`ActiveSetRead::excluded`], never silently dropped) rather than
/// erroring the whole read -- one bad/incomplete row must never block
/// every other bundle's hot-swap. A row that loads but with a `NULL`
/// `component_key` (derived via [`derive_component_keys`]'s fallback
/// instead) is recorded in [`ActiveSetRead::degraded`], not excluded.
pub async fn read_active_set(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    app_id: Option<&str>,
) -> Result<ActiveSetRead, ActiveSetError> {
    let mut active_q = app_active_versions::Entity::find()
        .filter(app_active_versions::Column::TenantId.eq(tenant_id))
        .filter(app_active_versions::Column::CommunityId.eq(community_id));
    if let Some(app_id) = app_id {
        active_q = active_q.filter(app_active_versions::Column::AppId.eq(app_id));
    }
    let active_rows = active_q.all(conn).await?;
    if active_rows.is_empty() {
        return Ok(ActiveSetRead::default());
    }

    let version_ids: Vec<i64> = active_rows.iter().map(|r| r.version_id).collect();
    let version_rows = app_versions::Entity::find()
        .filter(app_versions::Column::Id.is_in(version_ids))
        .all(conn)
        .await?;
    let versions_by_id: std::collections::HashMap<i64, app_versions::Model> =
        version_rows.into_iter().map(|v| (v.id, v)).collect();

    let approval_rows = app_install_approvals::Entity::find()
        .filter(app_install_approvals::Column::TenantId.eq(tenant_id))
        .filter(app_install_approvals::Column::SupersededBy.is_null())
        .all(conn)
        .await?;

    Ok(assemble_active_set(
        &active_rows,
        &versions_by_id,
        &approval_rows,
    ))
}

/// Shared row-assembly logic behind [`read_active_set`] (one `(tenant_id,
/// community_id)` scope, `approval_rows` pre-filtered to that scope's
/// tenant) and `crate::multi_tenant::read_active_set_all` (every scope at
/// once, `approval_rows` unfiltered across every tenant) -- pure, no I/O,
/// so both callers share one tested implementation of the ACTIVE+APPROVED
/// join/exclusion/degradation logic rather than maintaining two copies
/// that could silently drift apart.
///
/// **Multi-tenant correctness fix vs. the pre-rev-4 inline version this
/// replaces:** the approval-match predicate now also checks `appr.
/// tenant_id == active.tenant_id` explicitly. [`read_active_set`]'s own
/// `approval_rows` query was already tenant-filtered, so this was always
/// implicitly true there and changes nothing for that caller -- but
/// `read_active_set_all` intentionally reads `app_install_approvals`
/// UNFILTERED (one bulk query across every tenant, for efficiency, see
/// that function's own doc), so without this explicit check two different
/// tenants' apps sharing an `app_id` string and an identical `version`
/// value could cross-match each other's approval row. Tenant isolation is
/// a hard invariant (`rules/security.md` Tenant Isolation) -- this must be
/// checked here, once, rather than trusted to always be true of whatever
/// `approval_rows` slice a caller happens to pass in.
pub(crate) fn assemble_active_set(
    active_rows: &[app_active_versions::Model],
    versions_by_id: &std::collections::HashMap<i64, app_versions::Model>,
    approval_rows: &[app_install_approvals::Model],
) -> ActiveSetRead {
    let mut rows = Vec::with_capacity(active_rows.len());
    let mut excluded = Vec::new();
    let mut degraded = Vec::new();
    for active in active_rows {
        let Some(version_row) = versions_by_id.get(&active.version_id) else {
            tracing::warn!(
                app_id = %active.app_id,
                version_id = active.version_id,
                reason = ExclusionReason::MissingVersionRow.as_str(),
                "excluding from active set: active_versions points at a version_id with no app_versions row"
            );
            excluded.push((active.app_id.clone(), ExclusionReason::MissingVersionRow));
            continue;
        };

        let approval = approval_rows.iter().find(|appr| {
            appr.tenant_id == active.tenant_id
                && appr.app_id == active.app_id
                && appr.version == version_row.version
                && (appr.community_id == Some(active.community_id)
                    || (appr.community_id.is_none() && active.community_id == 0))
        });
        if approval.is_none() {
            // Ops-visibility fix (security review): a bundle silently
            // losing its approval is a feature going dark, not routine
            // background noise -- this was `debug!` and easy to miss.
            tracing::warn!(
                app_id = %active.app_id,
                version = %version_row.version,
                reason = ExclusionReason::NoApproval.as_str(),
                "excluding from active set: active version has no current install approval"
            );
            excluded.push((active.app_id.clone(), ExclusionReason::NoApproval));
            continue;
        }

        let Some(raw_digest) = version_row.artifact_digest.clone() else {
            tracing::warn!(
                app_id = %active.app_id,
                version = %version_row.version,
                reason = ExclusionReason::MissingDigest.as_str(),
                "excluding from active set: active, approved version has no artifact_digest yet"
            );
            excluded.push((active.app_id.clone(), ExclusionReason::MissingDigest));
            continue;
        };
        // Digest-format contract fix (regression: DB bare-hex digest
        // rejected by executor Load (malformed digest), UnknownBundle
        // (alpha 2026-10-03)): `artifact_digest` is stored bare-hex by the
        // control plane but the executor's wire protocol requires
        // `sha256:<64 hex>` -- canonicalize once here, the sole boundary
        // every downstream `ActiveBundleRow::digest` consumer shares. Fail
        // loud (not the `warn!` the other exclusion reasons use): a
        // malformed digest the control plane itself wrote is a
        // data-integrity bug.
        let digest = match canonical_digest(&raw_digest) {
            Ok(digest) => digest,
            Err(err) => {
                tracing::error!(
                    app_id = %active.app_id,
                    version = %version_row.version,
                    error = %err,
                    reason = ExclusionReason::MalformedDigest.as_str(),
                    "excluding from active set: artifact_digest is malformed"
                );
                excluded.push((active.app_id.clone(), ExclusionReason::MalformedDigest));
                continue;
            }
        };

        // component_key contract (data-plane half; hub-api half is the
        // migration + publish-step backfill landing in parallel): use the
        // real `app_versions.component_key`/`sidecar_key` columns
        // directly when set -- `derive_component_keys` is now a defensive
        // fallback for rows published before either lands, never the
        // primary path. `sidecar_key` falls back independently of
        // `component_key` (a present component with no sidecar is a
        // distinct, non-degraded case -- not every bundle ships a
        // sidecar).
        let (component_key, sidecar_key) = match version_row.component_key.clone() {
            Some(component_key) => {
                let sidecar_key = version_row
                    .sidecar_key
                    .clone()
                    .unwrap_or_else(|| derive_component_keys(&digest).1);
                (component_key, sidecar_key)
            }
            None => {
                tracing::warn!(
                    app_id = %active.app_id,
                    version = %version_row.version,
                    reason = DegradedReason::MissingComponentKey.as_str(),
                    "app_versions.component_key is NULL; falling back to content-addressed \
                     derivation (row published before the migration/backfill landed)"
                );
                degraded.push((active.app_id.clone(), DegradedReason::MissingComponentKey));
                derive_component_keys(&digest)
            }
        };
        // `approval` is `Some` here -- the `None` arm above always
        // `continue`s before this point.
        let declared_capabilities = approval
            .map(|appr| declared_capabilities_from_summary(&appr.summary_json))
            .unwrap_or_default();
        // Structural guard (regression: multi_tenant path sent bare-hex
        // digest to Invoke, UnknownBundle despite loaded bundle (alpha
        // 2026-10-03)): this is the ONE sanctioned production construction
        // site for `ActiveBundleRow` in this crate -- every other caller
        // (`crate::multi_tenant::read_active_set_all`, every service's
        // `changelog_consumer`/`bundle_loader`) only ever clones a row this
        // function already produced, never builds one from a raw
        // `app_versions.artifact_digest` value directly. `ActiveBundleRow`
        // can't be made a private-field/newtype-enforced type without a
        // wide `bundle_active_set`/`svc_process`/`svc_action` test-fixture
        // refactor out of this fix's scope -- this `debug_assert` plus
        // `active_bundle_row_is_only_ever_constructed_from_canonical_digest_in_this_module`
        // (below) are the cheaper structural substitute: any future
        // construction site added outside this function's test-gated
        // fixtures fails that scan test immediately.
        debug_assert!(
            digest.starts_with("sha256:") && digest.len() == 71,
            "ActiveBundleRow::digest must always be canonical_digest()'s output, got {digest:?}"
        );
        rows.push(ActiveBundleRow {
            app_id: active.app_id.clone(),
            version: version_row.version.clone(),
            digest,
            component_key,
            sidecar_key,
            artifact_signature: version_row.artifact_signature.clone(),
            artifact_signature_key_id: version_row.artifact_signature_key_id.clone(),
            artifact_signed_approval_id: version_row.artifact_signed_approval_id,
            declared_capabilities,
        });
    }

    ActiveSetRead {
        rows,
        excluded,
        degraded,
    }
}

/// Tracks the last-seen [`Watermark`] for one poller instance and decides
/// whether a tick's freshly-read watermark represents a real change --
/// pure, no I/O, so the "unchanged watermark skips the full read"
/// requirement is testable without a database. The first tick after
/// construction always reports changed (`None` -> `Some`), matching the
/// required "still do one full read on startup to establish current
/// state".
#[derive(Debug, Default)]
pub struct WatermarkTracker {
    last: Option<Watermark>,
}

impl WatermarkTracker {
    pub fn new() -> Self {
        Self::default()
    }

    /// Records `current` and reports whether it differs from the
    /// previously recorded watermark (or there was none yet). Callers do
    /// the full [`read_active_set`] read only when this returns `true`.
    pub fn observe(&mut self, current: Watermark) -> bool {
        let changed = self.last.as_ref() != Some(&current);
        self.last = Some(current);
        changed
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn watermark_of(pairs: &[(&str, i64)]) -> Watermark {
        let mut hasher = Sha256::new();
        for (app_id, version_id) in pairs {
            hasher.update(app_id.as_bytes());
            hasher.update(b"\0");
            hasher.update(version_id.to_le_bytes());
            hasher.update(b"\n");
        }
        Watermark(format!("{:x}", hasher.finalize()))
    }

    #[test]
    fn watermark_tracker_reports_changed_on_the_first_observation() {
        let mut tracker = WatermarkTracker::new();
        assert!(tracker.observe(watermark_of(&[("waddles.a", 10)])));
    }

    #[test]
    fn watermark_tracker_reports_unchanged_when_the_watermark_repeats() {
        let mut tracker = WatermarkTracker::new();
        let w = watermark_of(&[("waddles.a", 10)]);
        assert!(tracker.observe(w.clone()));
        assert!(
            !tracker.observe(w.clone()),
            "an unchanged watermark must skip the full read"
        );
        assert!(!tracker.observe(w));
    }

    #[test]
    fn watermark_tracker_reports_changed_when_the_active_set_moves() {
        let mut tracker = WatermarkTracker::new();
        assert!(tracker.observe(watermark_of(&[("waddles.a", 10)])));
        assert!(tracker.observe(watermark_of(&[("waddles.a", 10), ("waddles.b", 1)])));
        let third = watermark_of(&[("waddles.a", 10), ("waddles.b", 2)]);
        assert!(tracker.observe(third.clone()));
        assert!(!tracker.observe(third));
    }

    #[test]
    fn derive_component_keys_strips_the_sha256_prefix_and_is_content_addressed() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let (component, sidecar) = derive_component_keys(&digest);
        assert_eq!(
            component,
            format!("bundles/{}/component.wasm", "a".repeat(64))
        );
        assert_eq!(sidecar, format!("bundles/{}/sidecar.json", "a".repeat(64)));
    }

    #[test]
    fn derive_component_keys_tolerates_a_digest_without_the_prefix() {
        let (component, _sidecar) = derive_component_keys("deadbeef");
        assert_eq!(component, "bundles/deadbeef/component.wasm");
    }

    fn active_model(
        app_id: &str,
        tenant_id: i32,
        community_id: i32,
        version_id: i64,
    ) -> app_active_versions::Model {
        app_active_versions::Model {
            app_id: app_id.to_string(),
            tenant_id,
            community_id,
            version_id,
        }
    }

    #[tokio::test]
    async fn read_watermark_matches_the_pure_fingerprint_of_its_rows() -> Result<(), ActiveSetError>
    {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![
                active_model("waddles.a", 1, 0, 10),
                active_model("waddles.b", 1, 0, 3),
            ]])
            .append_query_results([Vec::<app_source_bindings::Model>::new()])
            .into_connection();
        let watermark = read_watermark(&db, 1, 0).await?;
        assert_eq!(
            watermark,
            watermark_of(&[("waddles.a", 10), ("waddles.b", 3)])
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_watermark_is_a_fixed_empty_hash_on_an_empty_scope() -> Result<(), ActiveSetError>
    {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<app_active_versions::Model>::new()])
            .append_query_results([Vec::<app_source_bindings::Model>::new()])
            .into_connection();
        let watermark = read_watermark(&db, 1, 0).await?;
        // The empty-scope watermark is SHA-256 of zero bytes, NOT
        // `Watermark::default()`'s derived empty string -- `Default` on
        // this type exists only so `WatermarkTracker` can hold `Option
        // <Watermark>`'s `None` case cleanly, never as a stand-in for "the
        // hash of an empty active set".
        assert_eq!(watermark, watermark_of(&[]));
        Ok(())
    }

    /// Security review fix: **the regression this `Watermark` type exists
    /// to prevent.** The retired `COUNT(*) + SUM(version_id)` watermark
    /// canceled here -- app A's `version_id` drops by 1 (10 -> 9) in the
    /// same tick app B's rises by 1 (5 -> 6): `COUNT` stays 2, `SUM` stays
    /// 15 both before and after, so that watermark would never move and
    /// the hot-swap would silently miss this change. The SHA-256
    /// fingerprint hashes each row's own `(app_id, version_id)` identity,
    /// not an aggregate that discards which row changed, so it cannot
    /// cancel this way.
    #[tokio::test]
    async fn read_watermark_moves_when_two_rows_shift_by_equal_and_opposite_amounts(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![
                active_model("waddles.a", 1, 0, 10),
                active_model("waddles.b", 1, 0, 5),
            ]])
            .append_query_results([Vec::<app_source_bindings::Model>::new()])
            .append_query_results([vec![
                active_model("waddles.a", 1, 0, 9),
                active_model("waddles.b", 1, 0, 6),
            ]])
            .append_query_results([Vec::<app_source_bindings::Model>::new()])
            .into_connection();

        let before = read_watermark(&db, 1, 0).await?;
        let after = read_watermark(&db, 1, 0).await?;
        assert_ne!(
            before, after,
            "A-1/B+1 in the same tick must still move the watermark"
        );
        Ok(())
    }

    fn binding_model(
        app_id: &str,
        tenant_id: i32,
        community_id: i32,
        platform: &str,
        source_id: &str,
    ) -> app_source_bindings::Model {
        app_source_bindings::Model {
            tenant_id,
            community_id,
            app_id: app_id.to_string(),
            platform: platform.to_string(),
            source_id: source_id.to_string(),
        }
    }

    /// Regression test for the source-binding supervisor's own change-
    /// detection dependency: an `app_source_bindings` row appearing with NO
    /// `app_active_versions` change at all must still move the watermark --
    /// otherwise a newly-added binding for an already-active app would
    /// never be picked up until some unrelated activation churn happened to
    /// also occur.
    #[tokio::test]
    async fn read_watermark_moves_when_a_binding_is_added_with_no_active_versions_change(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_model("waddles.a", 1, 0, 10)]])
            .append_query_results([Vec::<app_source_bindings::Model>::new()])
            .append_query_results([vec![active_model("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![binding_model(
                "waddles.a",
                1,
                0,
                "twitch",
                "tw-channelA",
            )]])
            .into_connection();

        let before = read_watermark(&db, 1, 0).await?;
        let after = read_watermark(&db, 1, 0).await?;
        assert_ne!(
            before, after,
            "a binding add with no active_versions change must still move the watermark"
        );
        Ok(())
    }

    /// Complementary regression: the same two rows in a *different* order
    /// off the mock's own return sequence must still fingerprint identically
    /// -- proves the in-Rust `sort_by` before hashing (not incidental query
    /// ordering) is what makes the binding half of the watermark
    /// deterministic, mirroring `app_active_versions`'s own `order_by_asc`
    /// guarantee.
    #[tokio::test]
    async fn read_watermark_binding_order_does_not_affect_the_fingerprint(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_model("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![
                binding_model("waddles.a", 1, 0, "twitch", "tw-channelA"),
                binding_model("waddles.a", 1, 0, "discord", "dg-x"),
            ]])
            .append_query_results([vec![active_model("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![
                binding_model("waddles.a", 1, 0, "discord", "dg-x"),
                binding_model("waddles.a", 1, 0, "twitch", "tw-channelA"),
            ]])
            .into_connection();

        let first = read_watermark(&db, 1, 0).await?;
        let second = read_watermark(&db, 1, 0).await?;
        assert_eq!(
            first, second,
            "the same binding set in a different query-return order must hash identically"
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_active_set_returns_empty_when_no_rows_are_active() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<app_active_versions::Model>::new()])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert!(result.rows.is_empty());
        assert!(result.excluded.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn read_active_set_excludes_a_version_with_no_current_approval(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "b".repeat(64));
        // `MockDatabase::append_query_results` is generic per call over one
        // row type -- three sequential queries against three different
        // entities need three chained calls, not one array mixing
        // `Vec<app_active_versions::Model>`/`Vec<app_versions::Model>`/
        // `Vec<app_install_approvals::Model>` (which wouldn't type-check).
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest),
                scan_status: "scanned".to_string(),
                component_key: None,
                sidecar_key: None,
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([Vec::<app_install_approvals::Model>::new()])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert!(
            result.rows.is_empty(),
            "no approval row -> excluded, got {:?}",
            result.rows
        );
        assert_eq!(
            result.excluded,
            vec![("waddles.test.app".to_string(), ExclusionReason::NoApproval)]
        );
        Ok(())
    }

    /// `component_key` contract: a `NULL` column value falls back to
    /// [`derive_component_keys`] and records a [`DegradedReason::
    /// MissingComponentKey`] entry -- the row still loads (never
    /// excluded), but the fallback is visible, not silent.
    #[tokio::test]
    async fn read_active_set_falls_back_to_derived_keys_when_component_key_is_null(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "c".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: None,
                sidecar_key: None,
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                // Tenant-wide approval encoded as NULL (see the
                // sentinel-mismatch doc on the entity module) against
                // this active row's `community_id = 0` sentinel.
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: sea_orm::JsonValue::Null,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows.len(), 1);
        assert_eq!(result.rows[0].app_id, "waddles.test.app");
        assert_eq!(result.rows[0].digest, digest);
        let (expected_component, expected_sidecar) = derive_component_keys(&digest);
        assert_eq!(result.rows[0].component_key, expected_component);
        assert_eq!(result.rows[0].sidecar_key, expected_sidecar);
        assert!(result.excluded.is_empty());
        assert_eq!(
            result.degraded,
            vec![(
                "waddles.test.app".to_string(),
                DegradedReason::MissingComponentKey
            )]
        );
        Ok(())
    }

    /// `component_key` contract, the primary (non-fallback) path: a
    /// non-`NULL` `component_key`/`sidecar_key` column value is used
    /// directly, verbatim -- `derive_component_keys` is never consulted,
    /// and no degraded entry is recorded.
    #[tokio::test]
    async fn read_active_set_uses_the_real_component_key_column_when_present(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "f".repeat(64));
        let real_component_key = "bundles/waddles.test.app/1/real.wasm".to_string();
        let real_sidecar_key = "bundles/waddles.test.app/1/real.json".to_string();
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: Some(real_component_key.clone()),
                sidecar_key: Some(real_sidecar_key.clone()),
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: sea_orm::JsonValue::Null,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows.len(), 1);
        assert_eq!(result.rows[0].component_key, real_component_key);
        assert_eq!(result.rows[0].sidecar_key, real_sidecar_key);
        // Never the derived fallback -- proves the real column value won,
        // not a coincidental match.
        let (derived_component, _) = derive_component_keys(&digest);
        assert_ne!(result.rows[0].component_key, derived_component);
        assert!(result.excluded.is_empty());
        assert!(
            result.degraded.is_empty(),
            "a present component_key must never be recorded as degraded"
        );
        Ok(())
    }

    /// Artifact-signature columns (migration `0040_bundle_artifact_
    /// signature`) pass through `ActiveBundleRow` verbatim -- surfaced for
    /// observability only, the authoritative check lives in
    /// `core/bundle_executor/src/signing.rs` against the bucket sidecar
    /// (see `entities::app_versions`'s module doc).
    #[tokio::test]
    async fn read_active_set_surfaces_the_artifact_signature_columns() -> Result<(), ActiveSetError>
    {
        let digest = format!("sha256:{}", "d".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest),
                scan_status: "scanned".to_string(),
                component_key: Some("bundles/waddles.test.app/1/real.wasm".to_string()),
                sidecar_key: Some("bundles/waddles.test.app/1/real.json".to_string()),
                artifact_signature: Some("c2lnbmF0dXJl".to_string()),
                artifact_signature_key_id: Some("platform-2026-09".to_string()),
                artifact_signed_approval_id: Some(42),
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 42,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: sea_orm::JsonValue::Null,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows.len(), 1);
        assert_eq!(
            result.rows[0].artifact_signature.as_deref(),
            Some("c2lnbmF0dXJl")
        );
        assert_eq!(
            result.rows[0].artifact_signature_key_id.as_deref(),
            Some("platform-2026-09")
        );
        assert_eq!(result.rows[0].artifact_signed_approval_id, Some(42));
        Ok(())
    }

    /// A not-yet-signed row (pre-migration backfill, or approved before
    /// hub-api's signing step ran) surfaces `None` for all three columns
    /// rather than erroring or excluding the row -- `bundle_executor`'s own
    /// sidecar-based check is what fails closed on a genuinely missing
    /// signature, not this crate.
    #[tokio::test]
    async fn read_active_set_surfaces_none_when_artifact_signature_columns_are_unset(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "e".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest),
                scan_status: "scanned".to_string(),
                component_key: Some("bundles/waddles.test.app/1/real.wasm".to_string()),
                sidecar_key: Some("bundles/waddles.test.app/1/real.json".to_string()),
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: sea_orm::JsonValue::Null,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows.len(), 1);
        assert!(result.rows[0].artifact_signature.is_none());
        assert!(result.rows[0].artifact_signature_key_id.is_none());
        assert!(result.rows[0].artifact_signed_approval_id.is_none());
        Ok(())
    }

    /// `sidecar_key` falls back independently of `component_key`: a real
    /// `component_key` with a `NULL` `sidecar_key` uses the real component
    /// key verbatim and only derives the sidecar half -- not treated as
    /// degraded (many bundles have no sidecar at all).
    #[tokio::test]
    async fn read_active_set_derives_only_the_sidecar_when_component_key_is_present_but_sidecar_is_null(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "a1".repeat(32));
        let real_component_key = "bundles/waddles.test.app/1/real.wasm".to_string();
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: Some(real_component_key.clone()),
                sidecar_key: None,
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: sea_orm::JsonValue::Null,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows[0].component_key, real_component_key);
        let (_, expected_sidecar) = derive_component_keys(&digest);
        assert_eq!(result.rows[0].sidecar_key, expected_sidecar);
        assert!(
            result.degraded.is_empty(),
            "a present component_key must never be recorded as degraded, even with a null sidecar_key"
        );
        Ok(())
    }

    /// Multi-tenant correctness regression: an approval row belonging to a
    /// DIFFERENT tenant, sharing this active row's `app_id`/`version`/
    /// community-sentinel shape, must never satisfy the approval check --
    /// proves `assemble_active_set`'s explicit `tenant_id` comparison (added
    /// for `crate::multi_tenant::read_active_set_all`'s unfiltered
    /// `approval_rows`) is actually enforced, not just documented.
    #[tokio::test]
    async fn read_active_set_does_not_cross_match_an_approval_from_a_different_tenant(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "9".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.shared".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.shared".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest),
                scan_status: "scanned".to_string(),
                component_key: None,
                sidecar_key: None,
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            // Approval belongs to tenant 2, not tenant 1 -- same app_id/
            // version/community sentinel otherwise.
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 2,
                community_id: None,
                app_id: "waddles.shared".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: sea_orm::JsonValue::Null,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert!(
            result.rows.is_empty(),
            "a different tenant's approval must never satisfy this tenant's active row"
        );
        assert_eq!(
            result.excluded,
            vec![("waddles.shared".to_string(), ExclusionReason::NoApproval)]
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_active_set_excludes_a_version_missing_its_digest() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: None,
                scan_status: "not_scanned".to_string(),
                component_key: None,
                sidecar_key: None,
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: sea_orm::JsonValue::Null,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert!(result.rows.is_empty());
        assert_eq!(
            result.excluded,
            vec![(
                "waddles.test.app".to_string(),
                ExclusionReason::MissingDigest
            )]
        );
        Ok(())
    }

    // -- `canonical_digest` contract (regression: DB bare-hex digest
    // rejected by executor Load (malformed digest), UnknownBundle (alpha
    // 2026-10-03)) --------------------------------------------------------

    /// Mirrors `core/bundle_executor/src/invoke.rs::verify_digest`'s own
    /// accept case -- an already-`sha256:`-prefixed, already-lowercase
    /// digest is returned unchanged.
    #[test]
    fn canonical_digest_leaves_an_already_prefixed_lowercase_digest_unchanged() {
        let digest = format!("sha256:{}", "a".repeat(64));
        assert_eq!(canonical_digest(&digest).unwrap(), digest);
    }

    /// The exact bug this function exists to fix: `app_versions.
    /// artifact_digest` is stored bare-hex by the control plane; the
    /// executor's wire protocol rejects anything without the `sha256:`
    /// prefix (`error "executor reported error LoadFailed: malformed digest
    /// ...: expected sha256:<64 hex chars>"`, alpha 2026-10-03).
    #[test]
    fn canonical_digest_prefixes_a_bare_hex_digest() {
        let hex = "b".repeat(64);
        assert_eq!(canonical_digest(&hex).unwrap(), format!("sha256:{hex}"));
    }

    /// Lowercases hex so a canonical-form comparison (`diff::plan`'s
    /// `loaded` map, `LoadState::is_loaded_on`) never misses a match over
    /// case alone -- mirrors `verify_digest`'s `eq_ignore_ascii_case`
    /// tolerance on the compare side by normalizing on the way in instead.
    #[test]
    fn canonical_digest_lowercases_mixed_case_hex_in_either_input_form() {
        let upper_hex = "C".repeat(64);
        let expected = format!("sha256:{}", "c".repeat(64));
        assert_eq!(canonical_digest(&upper_hex).unwrap(), expected);
        assert_eq!(
            canonical_digest(&format!("sha256:{upper_hex}")).unwrap(),
            expected
        );
    }

    #[test]
    fn canonical_digest_rejects_the_wrong_hex_length() {
        assert!(canonical_digest("deadbeef").is_err());
        assert!(canonical_digest(&format!("sha256:{}", "a".repeat(63))).is_err());
        assert!(canonical_digest(&format!("sha256:{}", "a".repeat(65))).is_err());
    }

    #[test]
    fn canonical_digest_rejects_non_hex_characters() {
        assert!(canonical_digest(&format!("sha256:{}z", "a".repeat(63))).is_err());
    }

    #[test]
    fn canonical_digest_rejects_an_empty_string() {
        assert!(canonical_digest("").is_err());
    }

    /// The DB-path contract end to end: a bare-hex `artifact_digest` (what
    /// the control plane actually stores) produces a `sha256:`-prefixed
    /// `ActiveBundleRow::digest` -- the form the executor's `Load` wire
    /// request requires. Storage-key derivation keeps using the bare hex it
    /// has always used (`derive_component_keys` strips the prefix itself).
    #[tokio::test]
    async fn read_active_set_canonicalizes_a_bare_hex_artifact_digest() -> Result<(), ActiveSetError>
    {
        let hex = "1".repeat(64);
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(hex.clone()),
                scan_status: "scanned".to_string(),
                component_key: None,
                sidecar_key: None,
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: sea_orm::JsonValue::Null,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows.len(), 1);
        assert_eq!(result.rows[0].digest, format!("sha256:{hex}"));
        // Storage keys derive from the bare hex, never double-prefixed.
        assert_eq!(
            result.rows[0].component_key,
            format!("bundles/{hex}/component.wasm")
        );
        assert_eq!(
            result.rows[0].sidecar_key,
            format!("bundles/{hex}/sidecar.json")
        );
        Ok(())
    }

    /// regression: same-digest manifest-only release (ping 1.0.2/1.0.3)
    /// emptied svc-action dispatch digest (alpha 2026-10-03). `app_versions`
    /// can hold an older, now-inactive version row sharing the EXACT same
    /// `artifact_digest` as the currently-active version (#537 dropped the
    /// global digest-unique constraint, allowing manifest-only re-releases
    /// that don't change the artifact at all) -- `read_active_set` must
    /// still resolve to the ACTIVE `version_id`'s own row (never confuse the
    /// two just because their digests match), and the result must carry a
    /// real, non-empty canonical digest.
    #[tokio::test]
    async fn read_active_set_resolves_the_active_version_even_when_an_inactive_sibling_version_shares_its_digest(
    ) -> Result<(), ActiveSetError> {
        let shared_digest = "2".repeat(64);
        // `app_active_versions` points at version_id 20 (the newer "1.0.3")
        // -- version_id 10 ("1.0.2", the old, now-inactive version sharing
        // the same digest) is never referenced from the active row at all,
        // so the second query (`Id.is_in(version_ids)`) only ever fetches
        // version_id 20 in production; this fixture exercises that exact
        // join, not a hypothetical where both ids leak through.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "ping".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 20,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 20,
                app_id: "ping".to_string(),
                version: "1.0.3".to_string(),
                artifact_digest: Some(shared_digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: None,
                sidecar_key: None,
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 2,
                tenant_id: 1,
                community_id: None,
                app_id: "ping".to_string(),
                version: "1.0.3".to_string(),
                superseded_by: None,
                summary_json: sea_orm::JsonValue::Null,
            }]])
            .into_connection();

        let result = read_active_set(&db, 1, 0, None).await?;

        assert_eq!(result.rows.len(), 1);
        assert_eq!(result.rows[0].version, "1.0.3");
        assert_eq!(result.rows[0].digest, format!("sha256:{shared_digest}"));
        assert!(!result.rows[0].digest.is_empty());
        assert!(result.excluded.is_empty());
        Ok(())
    }

    /// Same scenario, driven directly through [`assemble_active_set`] with
    /// BOTH version rows present in `versions_by_id` (the inactive 1.0.2
    /// sibling included, simulating a future caller that over-fetches) --
    /// proves the join keys strictly on `active.version_id`, never
    /// incidentally matching the OTHER row just because it shares a digest.
    #[test]
    fn assemble_active_set_keys_strictly_on_version_id_not_on_a_shared_digest() {
        let shared_digest = "3".repeat(64);
        let active_rows = vec![app_active_versions::Model {
            app_id: "ping".to_string(),
            tenant_id: 1,
            community_id: 0,
            version_id: 20,
        }];
        let mut versions_by_id = std::collections::HashMap::new();
        versions_by_id.insert(
            10,
            app_versions::Model {
                id: 10,
                app_id: "ping".to_string(),
                version: "1.0.2".to_string(),
                artifact_digest: Some(shared_digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: None,
                sidecar_key: None,
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            },
        );
        versions_by_id.insert(
            20,
            app_versions::Model {
                id: 20,
                app_id: "ping".to_string(),
                version: "1.0.3".to_string(),
                artifact_digest: Some(shared_digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: None,
                sidecar_key: None,
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            },
        );
        let approval_rows = vec![app_install_approvals::Model {
            id: 2,
            tenant_id: 1,
            community_id: None,
            app_id: "ping".to_string(),
            version: "1.0.3".to_string(),
            superseded_by: None,
            summary_json: sea_orm::JsonValue::Null,
        }];

        let result = assemble_active_set(&active_rows, &versions_by_id, &approval_rows);

        assert_eq!(result.rows.len(), 1);
        assert_eq!(result.rows[0].version, "1.0.3");
        assert_eq!(result.rows[0].digest, format!("sha256:{shared_digest}"));
    }

    /// An `artifact_digest` that is neither bare 64-hex nor `sha256:<64
    /// hex>` must exclude the row (fail loud, never load a bundle the
    /// executor is guaranteed to reject) rather than pass a malformed value
    /// through to the wire.
    #[tokio::test]
    async fn read_active_set_excludes_a_malformed_artifact_digest() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some("not-a-digest".to_string()),
                scan_status: "scanned".to_string(),
                component_key: None,
                sidecar_key: None,
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: sea_orm::JsonValue::Null,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert!(result.rows.is_empty());
        assert_eq!(
            result.excluded,
            vec![(
                "waddles.test.app".to_string(),
                ExclusionReason::MalformedDigest
            )]
        );
        Ok(())
    }

    /// `declared_capabilities_from_summary`'s own contract, exercised
    /// standalone (no database) -- `summary_json.capabilities` renders
    /// verbatim as `Vec<String>`.
    #[test]
    fn declared_capabilities_from_summary_reads_the_capabilities_array() {
        let summary: sea_orm::JsonValue = serde_json::json!({
            "capabilities": ["context", "kv", "flags", "log", "clock", "http"],
            "egress": [],
        });
        assert_eq!(
            declared_capabilities_from_summary(&summary),
            vec!["context", "kv", "flags", "log", "clock", "http"]
        );
    }

    #[test]
    fn declared_capabilities_from_summary_is_empty_for_every_malformed_shape() {
        for summary in [
            sea_orm::JsonValue::Null,
            serde_json::json!({}),
            serde_json::json!({"capabilities": "kv"}),
            serde_json::json!({"capabilities": [1, 2, 3]}),
            serde_json::json!("not even an object"),
        ] {
            assert_eq!(
                declared_capabilities_from_summary(&summary),
                Vec::<String>::new(),
                "expected an empty result for {summary:?}"
            );
        }
    }

    /// End-to-end through [`read_active_set`]: a real `summary_json` with a
    /// `"capabilities"` array containing `"kv"` surfaces on the resulting
    /// [`ActiveBundleRow::declared_capabilities`] -- the field
    /// `bundle_host_kv::authorize::CapabilitySnapshot` is populated from.
    #[tokio::test]
    async fn read_active_set_surfaces_declared_capabilities_from_summary_json(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "d".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: Some("bundles/waddles.test.app/1/c.wasm".to_string()),
                sidecar_key: Some("bundles/waddles.test.app/1/s.json".to_string()),
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: serde_json::json!({
                    "capabilities": ["context", "kv", "flags", "log", "clock"]
                }),
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows.len(), 1);
        assert_eq!(
            result.rows[0].declared_capabilities,
            vec!["context", "kv", "flags", "log", "clock"]
        );
        Ok(())
    }

    /// A row whose approval's `summary_json` never declares `"kv"` (the
    /// entire point of the coordinator fix on PR #425 -- `kv` is no longer
    /// unconditionally present in every bundle's derived capability set)
    /// surfaces an empty/absent-`kv` list, never a fabricated grant.
    #[tokio::test]
    async fn read_active_set_reflects_a_bundle_that_never_declared_kv() -> Result<(), ActiveSetError>
    {
        let digest = format!("sha256:{}", "e".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: Some("bundles/waddles.test.app/1/c.wasm".to_string()),
                sidecar_key: Some("bundles/waddles.test.app/1/s.json".to_string()),
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
                summary_json: serde_json::json!({
                    "capabilities": ["context", "flags", "log", "clock", "http"]
                }),
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows.len(), 1);
        assert!(!result.rows[0]
            .declared_capabilities
            .iter()
            .any(|c| c == "kv"));
        Ok(())
    }

    /// Structural regression guard (regression: multi_tenant path sent
    /// bare-hex digest to Invoke, UnknownBundle despite loaded bundle
    /// (alpha 2026-10-03)) -- `ActiveBundleRow` can't be made
    /// newtype/private-field-enforced without a wide cross-crate test-
    /// fixture refactor (see the `debug_assert` at this module's own
    /// construction site), so this is the cheaper substitute: a textual
    /// scan proving `assemble_active_set` (above, the sole canonicalizing
    /// constructor) is still the ONLY place any of these five sibling
    /// files builds an `ActiveBundleRow { .. }` outside their own
    /// `#[cfg(test)] mod tests` fixtures. A future caller that copies
    /// `app_versions::Model::artifact_digest` straight into a new
    /// `ActiveBundleRow` literal -- bypassing `canonical_digest()` exactly
    /// like the original bug -- fails this test immediately instead of
    /// waiting for another alpha incident.
    #[test]
    fn active_bundle_row_is_only_ever_constructed_from_canonical_digest_outside_tests() {
        // (file contents, byte offset of the file's own `mod tests` marker)
        // -- every `ActiveBundleRow {` occurrence in a given file must come
        // AFTER that file's `mod tests`, i.e. live only inside test
        // fixtures. `include_str!` paths are resolved relative to this
        // file (`src/query.rs`).
        let files: &[(&str, &str)] = &[
            (
                "bundle_active_set/src/multi_tenant.rs",
                include_str!("multi_tenant.rs"),
            ),
            (
                "bundle_active_set/src/full_sync.rs",
                include_str!("full_sync.rs"),
            ),
            ("bundle_active_set/src/diff.rs", include_str!("diff.rs")),
            (
                "svc_process/src/changelog_consumer.rs",
                include_str!("../../svc_process/src/changelog_consumer.rs"),
            ),
            (
                "svc_action/src/changelog_consumer.rs",
                include_str!("../../svc_action/src/changelog_consumer.rs"),
            ),
        ];
        let mut scanned = 0usize;
        for (name, contents) in files {
            let test_mod_at = contents
                .find("mod tests")
                .unwrap_or_else(|| panic!("{name}: expected a `mod tests` marker to scan against"));
            for (idx, _) in contents.match_indices("ActiveBundleRow {") {
                scanned += 1;
                assert!(
                    idx > test_mod_at,
                    "{name}: found an `ActiveBundleRow {{` construction at byte {idx}, \
                     before this file's `mod tests` (byte {test_mod_at}) -- a production \
                     construction site outside `crate::query::assemble_active_set` must \
                     canonicalize its digest via `canonical_digest()` or it will reproduce \
                     the alpha 2026-10-03 UnknownBundle regression"
                );
            }
        }
        assert!(
            scanned > 0,
            "expected to find at least one ActiveBundleRow {{ construction across the scanned \
             files (all currently test-only) -- a zero count means the scan itself is broken, \
             not that the invariant holds"
        );
    }
}
