//! `analytics-policy`: the ExportPolicy guard for the analytics foundation (Phase 1).
//!
//! Pure logic: no IO, no DB, no HTTP, no async runtime, no dependencies. The only
//! sellable dataset is tenant-wide ANONYMIZED TREND METADATA (tenant x week or
//! coarser, closed int-enum dimensions, no id below tenant). [`evaluate`] is
//! DENY-BY-DEFAULT and FAILS CLOSED: every gate must pass for an `Allow`, any
//! missing fact or unknown code is a `Deny` with a specific [`DenyCode`], and the
//! evaluator never panics on bad input.
//!
//! v3.0 hard-codes [`EXPORT_IMPLEMENTED`]` = false`: a `Live` request is ALWAYS
//! denied with `EXPORT_NOT_IMPLEMENTED`; a `DryRun` only reports what the other
//! gates would decide. No export path exists in this crate.
#![forbid(unsafe_code)]

mod suppress;

use std::collections::BTreeSet;

pub use suppress::{suppress_cells, SuppressionReport};

/// v3.0 hard-code: no export path exists. `Live` mode is always denied while false.
pub const EXPORT_IMPLEMENTED: bool = false;

/// Default (and minimum) k-anonymity threshold: contributors per cell.
pub const DEFAULT_K: u32 = 50;

/// `purpose_code` of the only sellable purpose: aggregate trend.
pub const PURPOSE_AGGREGATE_TREND: u8 = 2;
/// `classification_code` of the only sellable classification: aggregate anonymous.
pub const CLASSIFICATION_AGGREGATE_ANONYMOUS: u8 = 1;
/// `layer_code` of layer B (the one-way, consent-filtered derived layer).
pub const LAYER_B: u8 = 2;

/// Provenance code: first-party Waddles data (always allowed).
pub const PROV_WADDLES_NATIVE: u8 = 1;
/// Provenance codes 2..=5: discord, twitch, youtube, other (need platform clearance).
pub const PROV_PLATFORM_MIN: u8 = 2;
/// Highest known provenance code.
pub const PROV_PLATFORM_MAX: u8 = 5;

/// Time grains: only week (2) or coarser (month 3, quarter 4, all-time 5) may leave.
/// Finer grains (hour 0, day 1) and unknown codes are rejected.
pub const ALLOWED_GRAINS: &[u8] = &[2, 3, 4, 5];

/// Closed allowlist of dimension codes a cell may carry (an allowlist, never a
/// blocklist: community / user / channel dimensions are simply not on it).
pub mod dim {
    /// Metric family (e.g. messages, commands).
    pub const METRIC: u16 = 1;
    /// Coarse feature category.
    pub const FEATURE_CATEGORY: u16 = 2;
    /// Platform the aggregate came from (still subject to provenance clearance).
    pub const PLATFORM: u16 = 3;
    /// Coarse cohort-size band.
    pub const SIZE_BAND: u16 = 4;
    /// The complete allowlist.
    pub const ALLOWED: &[u16] = &[METRIC, FEATURE_CATEGORY, PLATFORM, SIZE_BAND];
}

/// Region codes (ISO 3166-1 alpha-2) treated as EU/EEA/UK-and-adjacent: the 27 EU
/// member states, EEA-EFTA (IS, LI, NO), the UK (GB) and, conservatively,
/// Switzerland (CH). Denied until `eu_transfer_approved` (counsel-approved
/// transfer mechanism) is true.
pub const EU_REGION_CODES: &[&str] = &[
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
    "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "GB", "CH",
];

/// Stable, int-coded deny reasons. Numeric value equals the gate number.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[repr(u8)]
pub enum DenyCode {
    /// Gate 1: dataset unregistered / wrong purpose / wrong classification / incomplete.
    DatasetNotAllowed = 1,
    /// Gate 2: not layer B, disallowed dimension, too-fine grain or foreign tenant cell.
    SchemaNotAllowed = 2,
    /// Gate 3: below k (tenant contributors), no cells, or nothing releasable.
    KAnonymity = 3,
    /// Gate 4: platform provenance not cleared (or unknown provenance code).
    ProvenanceNotCleared = 4,
    /// Gate 5: tenant opt-in not active on the current terms.
    OptInMissing = 5,
    /// Gate 6: no recipient, inactive contract, no no-reidentify/no-join, region not allowed.
    RecipientNotQualified = 6,
    /// Gate 7: exclusions newer than the data.
    ExclusionsStale = 7,
    /// Gate 8: age not excluded at source (unknown counts as not excluded).
    AgeNotExcluded = 8,
    /// Gate 9: EU/EEA/UK tenant (or unknown region) without an approved transfer mechanism.
    EuTransferNotApproved = 9,
    /// Gate 10: layer B retention longer than its input (or unknown).
    RetentionNotOk = 10,
    /// Gate 11: kill-switch flag off (or unknown).
    KillSwitchOff = 11,
    /// Gate 12: audit record could not be committed.
    AuditUnavailable = 12,
    /// Gate 13: v3.0 hard-coded, no export path exists.
    ExportNotImplemented = 13,
}

impl DenyCode {
    /// Stable integer code (equals the gate number).
    pub fn code(self) -> u8 {
        self as u8
    }

    /// Stable string name, safe for logs and audit records.
    pub fn name(self) -> &'static str {
        match self {
            DenyCode::DatasetNotAllowed => "DATASET_NOT_ALLOWED",
            DenyCode::SchemaNotAllowed => "SCHEMA_NOT_ALLOWED",
            DenyCode::KAnonymity => "K_ANONYMITY",
            DenyCode::ProvenanceNotCleared => "PROVENANCE_NOT_CLEARED",
            DenyCode::OptInMissing => "OPT_IN_MISSING",
            DenyCode::RecipientNotQualified => "RECIPIENT_NOT_QUALIFIED",
            DenyCode::ExclusionsStale => "EXCLUSIONS_STALE",
            DenyCode::AgeNotExcluded => "AGE_NOT_EXCLUDED",
            DenyCode::EuTransferNotApproved => "EU_TRANSFER_NOT_APPROVED",
            DenyCode::RetentionNotOk => "RETENTION_NOT_OK",
            DenyCode::KillSwitchOff => "KILL_SWITCH_OFF",
            DenyCode::AuditUnavailable => "AUDIT_UNAVAILABLE",
            DenyCode::ExportNotImplemented => "EXPORT_NOT_IMPLEMENTED",
        }
    }
}

/// Identifier of each gate; used by the evaluator and by the mutation-style tests.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum GateId {
    /// Gate 1.
    Dataset,
    /// Gate 2.
    Schema,
    /// Gate 3.
    KAnonymity,
    /// Gate 4.
    Provenance,
    /// Gate 5.
    OptIn,
    /// Gate 6.
    Recipient,
    /// Gate 7.
    Exclusions,
    /// Gate 8.
    Age,
    /// Gate 9.
    EuTransfer,
    /// Gate 10.
    Retention,
    /// Gate 11.
    KillSwitch,
    /// Gate 12.
    Audit,
    /// Gate 13.
    ExportNotImplemented,
}

/// Every gate, in gate-number order.
pub const ALL_GATES: [GateId; 13] = [
    GateId::Dataset,
    GateId::Schema,
    GateId::KAnonymity,
    GateId::Provenance,
    GateId::OptIn,
    GateId::Recipient,
    GateId::Exclusions,
    GateId::Age,
    GateId::EuTransfer,
    GateId::Retention,
    GateId::KillSwitch,
    GateId::Audit,
    GateId::ExportNotImplemented,
];

/// Dry-run reports a decision; Live would export (never possible in v3.0).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// Report only what the decision would be.
    DryRun,
    /// Real export attempt: always denied while `EXPORT_IMPLEMENTED` is false.
    Live,
}

/// Final verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    /// Every gate passed.
    Allow,
    /// At least one gate failed.
    Deny,
}

/// Dataset registration metadata. `None` anywhere denies (gate 1 / gate 10).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct DatasetMeta {
    /// Registered dataset name (non-empty).
    pub name: Option<String>,
    /// Whether the dataset is in the registry.
    pub registered: Option<bool>,
    /// Layer code; must be [`LAYER_B`].
    pub layer_code: Option<u8>,
    /// Purpose code; must be [`PURPOSE_AGGREGATE_TREND`].
    pub purpose_code: Option<u8>,
    /// Classification code; must be [`CLASSIFICATION_AGGREGATE_ANONYMOUS`].
    pub classification_code: Option<u8>,
    /// Retention in days (must be > 0).
    pub retention_days: Option<u32>,
    /// Dataset minimum k; effective k is `max(DEFAULT_K, min_k)`.
    pub min_k: Option<u32>,
}

/// One tenant-wide aggregate cell. Carries NO community/user/channel id by construction.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrendCell {
    /// Owning tenant (must equal the request tenant).
    pub tenant_id: u64,
    /// Time grain code (see [`ALLOWED_GRAINS`]).
    pub grain_code: u8,
    /// Dimension codes this cell is broken down by (must all be in [`dim::ALLOWED`]).
    pub dims: Vec<u16>,
    /// Metric code.
    pub metric_code: u16,
    /// Provenance code (1 native, 2..=5 platform).
    pub provenance: u8,
    /// Number of contributors behind this cell.
    pub cohort_size: u32,
    /// Aggregated value.
    pub value: i64,
    /// Optional summed-set id: components with the same id sum to the `is_total` cell.
    pub group: Option<u32>,
    /// True for the published total of its `group`.
    pub is_total: bool,
}

/// Tenant facts.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct TenantFacts {
    /// Tenant id.
    pub tenant_id: u64,
    /// Opt-in active on the CURRENT terms version.
    pub opt_in_active_on_current_terms: Option<bool>,
    /// ISO 3166-1 alpha-2 region code.
    pub region_code: Option<String>,
    /// Distinct contributors in the tenant.
    pub member_contributors: Option<u32>,
}

/// Recipient facts.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RecipientFacts {
    /// Contract active.
    pub active: Option<bool>,
    /// Contract forbids re-identification and joining.
    pub no_reidentify_no_join: Option<bool>,
    /// Recipient region allowed.
    pub region_allowed: Option<bool>,
}

/// Failure to commit the audit record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuditError;

/// Audit commit hook: must return `Ok` BEFORE an `Allow` is returned.
pub trait AuditCommit {
    /// Durably record the decision about to be returned.
    fn commit(&self) -> Result<(), AuditError>;
}

impl<F: Fn() -> Result<(), AuditError>> AuditCommit for F {
    fn commit(&self) -> Result<(), AuditError> {
        self()
    }
}

/// Everything the guard needs. Every fact is explicit; `None` means unknown => deny.
pub struct ExportRequest<'a> {
    /// Dataset metadata.
    pub dataset: Option<DatasetMeta>,
    /// Candidate cells.
    pub rows: Vec<TrendCell>,
    /// Tenant facts.
    pub tenant: Option<TenantFacts>,
    /// Recipient facts.
    pub recipient: Option<RecipientFacts>,
    /// Cleared platform provenance codes (subset of 2..=5).
    pub platform_clearance: Option<BTreeSet<u8>>,
    /// `computed_at >= last exclusion change`.
    pub exclusions_fresh: Option<bool>,
    /// Age excluded at source (unknown age counts as excluded).
    pub age_excluded_at_source: Option<bool>,
    /// Layer B retention <= its input's retention.
    pub retention_ok: Option<bool>,
    /// Kill switch: must be true.
    pub flag_enabled: Option<bool>,
    /// Counsel-approved EU transfer mechanism in place.
    pub eu_transfer_approved: Option<bool>,
    /// Dry run or live.
    pub mode: Mode,
    /// Audit hook.
    pub audit: Option<&'a dyn AuditCommit>,
}

/// The guard's answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Decision {
    /// Mode the decision was made in.
    pub mode: Mode,
    /// Allow only if every gate passed.
    pub outcome: Outcome,
    /// Sorted, de-duplicated deny codes (empty iff `Allow`).
    pub deny_codes: Vec<DenyCode>,
    /// Suppression accounting.
    pub suppression: SuppressionReport,
    /// Releasable cells; non-empty only on `Allow` (dry-run inspection, never exported).
    pub emitted: Vec<TrendCell>,
    /// True while no export path exists (always true in v3.0).
    pub export_not_implemented: bool,
}

fn is_true(v: Option<bool>) -> bool {
    v == Some(true)
}

/// Effective k for the request: `max(DEFAULT_K, dataset.min_k)`; `None` when unknown.
fn effective_k(req: &ExportRequest<'_>) -> Option<u32> {
    let m = req.dataset.as_ref()?.min_k?;
    Some(DEFAULT_K.max(m))
}

fn gate_dataset(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    let ok = match &req.dataset {
        Some(d) => {
            d.name.as_deref().is_some_and(|n| !n.trim().is_empty())
                && is_true(d.registered)
                && d.purpose_code == Some(PURPOSE_AGGREGATE_TREND)
                && d.classification_code == Some(CLASSIFICATION_AGGREGATE_ANONYMOUS)
                && d.min_k.is_some()
        }
        None => false,
    };
    if ok {
        vec![]
    } else {
        vec![DenyCode::DatasetNotAllowed]
    }
}

fn gate_schema(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    let layer_ok = req.dataset.as_ref().and_then(|d| d.layer_code) == Some(LAYER_B);
    let tenant_id = req.tenant.as_ref().map(|t| t.tenant_id);
    let cells_ok = req.rows.iter().all(|c| {
        ALLOWED_GRAINS.contains(&c.grain_code)
            && c.dims.iter().all(|d| dim::ALLOWED.contains(d))
            && Some(c.tenant_id) == tenant_id
    });
    if layer_ok && cells_ok {
        vec![]
    } else {
        vec![DenyCode::SchemaNotAllowed]
    }
}

fn gate_k(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    let (Some(k), Some(t)) = (effective_k(req), req.tenant.as_ref()) else {
        return vec![DenyCode::KAnonymity];
    };
    let contributors_ok = t.member_contributors.is_some_and(|m| m >= k);
    if !contributors_ok || req.rows.is_empty() {
        return vec![DenyCode::KAnonymity];
    }
    let (_, report) = suppress_cells(&req.rows, k, t.tenant_id, false);
    if report.cells_total == report.cells_suppressed {
        vec![DenyCode::KAnonymity]
    } else {
        vec![]
    }
}

fn gate_provenance(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    let Some(cleared) = &req.platform_clearance else {
        return vec![DenyCode::ProvenanceNotCleared];
    };
    let ok = req.rows.iter().all(|c| {
        c.provenance == PROV_WADDLES_NATIVE
            || ((PROV_PLATFORM_MIN..=PROV_PLATFORM_MAX).contains(&c.provenance)
                && cleared.contains(&c.provenance))
    });
    if ok {
        vec![]
    } else {
        vec![DenyCode::ProvenanceNotCleared]
    }
}

fn gate_opt_in(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    let ok = req
        .tenant
        .as_ref()
        .is_some_and(|t| is_true(t.opt_in_active_on_current_terms));
    if ok {
        vec![]
    } else {
        vec![DenyCode::OptInMissing]
    }
}

fn gate_recipient(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    let ok = req.recipient.as_ref().is_some_and(|r| {
        is_true(r.active) && is_true(r.no_reidentify_no_join) && is_true(r.region_allowed)
    });
    if ok {
        vec![]
    } else {
        vec![DenyCode::RecipientNotQualified]
    }
}

fn gate_exclusions(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    if is_true(req.exclusions_fresh) {
        vec![]
    } else {
        vec![DenyCode::ExclusionsStale]
    }
}

fn gate_age(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    if is_true(req.age_excluded_at_source) {
        vec![]
    } else {
        vec![DenyCode::AgeNotExcluded]
    }
}

fn gate_eu(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    let region = req
        .tenant
        .as_ref()
        .and_then(|t| t.region_code.as_deref())
        .map(|r| r.trim().to_ascii_uppercase());
    let ok = match region {
        // Unknown region fails closed: it could be an EU tenant.
        None => false,
        Some(r) if r.is_empty() => false,
        Some(r) if EU_REGION_CODES.contains(&r.as_str()) => is_true(req.eu_transfer_approved),
        Some(_) => true,
    };
    if ok {
        vec![]
    } else {
        vec![DenyCode::EuTransferNotApproved]
    }
}

fn gate_retention(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    let days_ok = req
        .dataset
        .as_ref()
        .and_then(|d| d.retention_days)
        .is_some_and(|d| d > 0);
    if is_true(req.retention_ok) && days_ok {
        vec![]
    } else {
        vec![DenyCode::RetentionNotOk]
    }
}

fn gate_kill_switch(req: &ExportRequest<'_>) -> Vec<DenyCode> {
    if is_true(req.flag_enabled) {
        vec![]
    } else {
        vec![DenyCode::KillSwitchOff]
    }
}

type GateFn = fn(&ExportRequest<'_>) -> Vec<DenyCode>;

/// Pure fact gates (1..=11), evaluated independently; the audit and not-implemented
/// gates need effects/mode and are handled in `evaluate_inner`.
const FACT_GATES: [(GateId, GateFn); 11] = [
    (GateId::Dataset, gate_dataset),
    (GateId::Schema, gate_schema),
    (GateId::KAnonymity, gate_k),
    (GateId::Provenance, gate_provenance),
    (GateId::OptIn, gate_opt_in),
    (GateId::Recipient, gate_recipient),
    (GateId::Exclusions, gate_exclusions),
    (GateId::Age, gate_age),
    (GateId::EuTransfer, gate_eu),
    (GateId::Retention, gate_retention),
    (GateId::KillSwitch, gate_kill_switch),
];

/// Evaluate the ExportPolicy guard. Never panics; deterministic output.
pub fn evaluate(req: &ExportRequest<'_>) -> Decision {
    evaluate_inner(req, None)
}

/// Evaluator with an optional gate removed. Only the mutation-style tests pass
/// `Some(..)`; the public API always runs every gate.
pub(crate) fn evaluate_inner(req: &ExportRequest<'_>, skip: Option<GateId>) -> Decision {
    let mut codes: BTreeSet<DenyCode> = BTreeSet::new();
    for (id, gate) in FACT_GATES.iter() {
        if Some(*id) != skip {
            codes.extend(gate(req));
        }
    }

    let (suppressed, suppression) = match (skip, effective_k(req), req.tenant.as_ref()) {
        (Some(GateId::KAnonymity), _, _) => (
            vec![false; req.rows.len()],
            SuppressionReport {
                cells_total: req.rows.len() as u64,
                cells_suppressed: 0,
                tenants_suppressed: 0,
            },
        ),
        (_, Some(k), Some(t)) => {
            let below = t.member_contributors.is_none_or(|m| m < k);
            suppress_cells(&req.rows, k, t.tenant_id, below)
        }
        // Unknown k or tenant: nothing is releasable.
        _ => suppress_cells(&req.rows, u32::MAX, u64::MAX, true),
    };

    if skip != Some(GateId::Audit) {
        match req.audit {
            None => {
                codes.insert(DenyCode::AuditUnavailable);
            }
            // Only commit an audit record for a decision that would otherwise Allow.
            Some(a) if codes.is_empty() => {
                if a.commit().is_err() {
                    codes.insert(DenyCode::AuditUnavailable);
                }
            }
            Some(_) => {}
        }
    }

    let export_not_implemented = !EXPORT_IMPLEMENTED;
    if req.mode == Mode::Live
        && export_not_implemented
        && skip != Some(GateId::ExportNotImplemented)
    {
        codes.insert(DenyCode::ExportNotImplemented);
    }

    let outcome = if codes.is_empty() {
        Outcome::Allow
    } else {
        Outcome::Deny
    };
    let emitted = if outcome == Outcome::Allow {
        req.rows
            .iter()
            .zip(suppressed.iter())
            .filter(|(_, s)| !**s)
            .map(|(c, _)| c.clone())
            .collect()
    } else {
        Vec::new()
    };
    Decision {
        mode: req.mode,
        outcome,
        deny_codes: codes.into_iter().collect(),
        suppression,
        emitted,
        export_not_implemented,
    }
}

#[cfg(test)]
mod tests;
