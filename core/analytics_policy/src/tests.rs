//! Gate tests: per-gate flips, mutation-style gate-removal detection, fail-closed,
//! determinism and seeded property tests. "A gate that cannot fail is not a gate."

use super::*;

struct AuditOk;
impl AuditCommit for AuditOk {
    fn commit(&self) -> Result<(), AuditError> {
        Ok(())
    }
}
struct AuditFail;
impl AuditCommit for AuditFail {
    fn commit(&self) -> Result<(), AuditError> {
        Err(AuditError)
    }
}
static OK: AuditOk = AuditOk;
static FAIL: AuditFail = AuditFail;

fn cell(cohort: u32, group: Option<u32>, is_total: bool) -> TrendCell {
    TrendCell {
        tenant_id: 7,
        grain_code: 2,
        dims: vec![dim::METRIC],
        metric_code: 1,
        provenance: PROV_WADDLES_NATIVE,
        cohort_size: cohort,
        value: 100,
        group,
        is_total,
    }
}

/// All facts good, dry run.
fn good() -> ExportRequest<'static> {
    ExportRequest {
        dataset: Some(DatasetMeta {
            name: Some("weekly_trend".into()),
            registered: Some(true),
            layer_code: Some(LAYER_B),
            purpose_code: Some(PURPOSE_AGGREGATE_TREND),
            classification_code: Some(CLASSIFICATION_AGGREGATE_ANONYMOUS),
            retention_days: Some(365),
            min_k: Some(50),
        }),
        rows: vec![
            cell(200, Some(1), true),
            cell(70, Some(1), false),
            cell(60, Some(1), false),
            cell(70, Some(1), false),
            cell(90, None, false),
        ],
        tenant: Some(TenantFacts {
            tenant_id: 7,
            opt_in_active_on_current_terms: Some(true),
            region_code: Some("US".into()),
            member_contributors: Some(500),
        }),
        recipient: Some(RecipientFacts {
            active: Some(true),
            no_reidentify_no_join: Some(true),
            region_allowed: Some(true),
        }),
        platform_clearance: Some(BTreeSet::new()),
        exclusions_fresh: Some(true),
        age_excluded_at_source: Some(true),
        retention_ok: Some(true),
        flag_enabled: Some(true),
        eu_transfer_approved: Some(false),
        mode: Mode::DryRun,
        audit: Some(&OK),
    }
}

type Mutator = fn(&mut ExportRequest<'static>);

/// Each row breaks ONLY one fact. (name, gate, expected code, mutator)
fn bad_table() -> Vec<(&'static str, GateId, DenyCode, Mutator)> {
    use DenyCode::*;
    vec![
        ("purpose wrong", GateId::Dataset, DatasetNotAllowed, |r| {
            r.dataset.as_mut().unwrap().purpose_code = Some(1)
        }),
        (
            "classification wrong",
            GateId::Dataset,
            DatasetNotAllowed,
            |r| r.dataset.as_mut().unwrap().classification_code = Some(2),
        ),
        ("unregistered", GateId::Dataset, DatasetNotAllowed, |r| {
            r.dataset.as_mut().unwrap().registered = Some(false)
        }),
        ("empty name", GateId::Dataset, DatasetNotAllowed, |r| {
            r.dataset.as_mut().unwrap().name = Some("  ".into())
        }),
        ("layer A", GateId::Schema, SchemaNotAllowed, |r| {
            r.dataset.as_mut().unwrap().layer_code = Some(1)
        }),
        ("community dim", GateId::Schema, SchemaNotAllowed, |r| {
            if let Some(c) = r.rows.last_mut() {
                c.dims.push(100);
            }
        }),
        ("user dim", GateId::Schema, SchemaNotAllowed, |r| {
            if let Some(c) = r.rows.last_mut() {
                c.dims.push(101);
            }
        }),
        ("channel dim", GateId::Schema, SchemaNotAllowed, |r| {
            if let Some(c) = r.rows.last_mut() {
                c.dims = vec![102];
            }
        }),
        ("day grain", GateId::Schema, SchemaNotAllowed, |r| {
            if let Some(c) = r.rows.last_mut() {
                c.grain_code = 1;
            }
        }),
        (
            "foreign tenant cell",
            GateId::Schema,
            SchemaNotAllowed,
            |r| {
                if let Some(c) = r.rows.last_mut() {
                    c.tenant_id = 8;
                }
            },
        ),
        ("tenant < k", GateId::KAnonymity, KAnonymity, |r| {
            r.tenant.as_mut().unwrap().member_contributors = Some(49)
        }),
        ("no rows", GateId::KAnonymity, KAnonymity, |r| {
            r.rows.clear()
        }),
        ("all cells < k", GateId::KAnonymity, KAnonymity, |r| {
            r.rows = vec![cell(10, None, false)]
        }),
        (
            "discord uncleared",
            GateId::Provenance,
            ProvenanceNotCleared,
            |r| {
                if let Some(c) = r.rows.last_mut() {
                    c.provenance = 2;
                }
            },
        ),
        (
            "unknown provenance",
            GateId::Provenance,
            ProvenanceNotCleared,
            |r| {
                if let Some(c) = r.rows.last_mut() {
                    c.provenance = 9;
                }
            },
        ),
        (
            "zero provenance",
            GateId::Provenance,
            ProvenanceNotCleared,
            |r| {
                if let Some(c) = r.rows.last_mut() {
                    c.provenance = 0;
                }
            },
        ),
        ("opt-in lapsed", GateId::OptIn, OptInMissing, |r| {
            r.tenant.as_mut().unwrap().opt_in_active_on_current_terms = Some(false)
        }),
        (
            "recipient inactive",
            GateId::Recipient,
            RecipientNotQualified,
            |r| r.recipient.as_mut().unwrap().active = Some(false),
        ),
        (
            "recipient may reidentify",
            GateId::Recipient,
            RecipientNotQualified,
            |r| r.recipient.as_mut().unwrap().no_reidentify_no_join = Some(false),
        ),
        (
            "recipient region",
            GateId::Recipient,
            RecipientNotQualified,
            |r| r.recipient.as_mut().unwrap().region_allowed = Some(false),
        ),
        (
            "exclusions stale",
            GateId::Exclusions,
            ExclusionsStale,
            |r| r.exclusions_fresh = Some(false),
        ),
        ("age not excluded", GateId::Age, AgeNotExcluded, |r| {
            r.age_excluded_at_source = Some(false)
        }),
        (
            "EU tenant",
            GateId::EuTransfer,
            EuTransferNotApproved,
            |r| r.tenant.as_mut().unwrap().region_code = Some("DE".into()),
        ),
        (
            "UK tenant lowercase",
            GateId::EuTransfer,
            EuTransferNotApproved,
            |r| r.tenant.as_mut().unwrap().region_code = Some(" gb ".into()),
        ),
        (
            "empty region",
            GateId::EuTransfer,
            EuTransferNotApproved,
            |r| r.tenant.as_mut().unwrap().region_code = Some("".into()),
        ),
        ("retention not ok", GateId::Retention, RetentionNotOk, |r| {
            r.retention_ok = Some(false)
        }),
        (
            "retention zero days",
            GateId::Retention,
            RetentionNotOk,
            |r| r.dataset.as_mut().unwrap().retention_days = Some(0),
        ),
        ("kill switch off", GateId::KillSwitch, KillSwitchOff, |r| {
            r.flag_enabled = Some(false)
        }),
        ("audit fails", GateId::Audit, AuditUnavailable, |r| {
            r.audit = Some(&FAIL)
        }),
        (
            "live mode",
            GateId::ExportNotImplemented,
            ExportNotImplemented,
            |r| r.mode = Mode::Live,
        ),
    ]
}

/// Each row sets ONE fact to None. (name, expected code, mutator)
fn missing_table() -> Vec<(&'static str, DenyCode, Mutator)> {
    use DenyCode::*;
    vec![
        ("dataset", DatasetNotAllowed, |r| r.dataset = None),
        ("dataset.name", DatasetNotAllowed, |r| {
            r.dataset.as_mut().unwrap().name = None
        }),
        ("dataset.registered", DatasetNotAllowed, |r| {
            r.dataset.as_mut().unwrap().registered = None
        }),
        ("dataset.layer", SchemaNotAllowed, |r| {
            r.dataset.as_mut().unwrap().layer_code = None
        }),
        ("dataset.purpose", DatasetNotAllowed, |r| {
            r.dataset.as_mut().unwrap().purpose_code = None
        }),
        ("dataset.class", DatasetNotAllowed, |r| {
            r.dataset.as_mut().unwrap().classification_code = None
        }),
        ("dataset.retention_days", RetentionNotOk, |r| {
            r.dataset.as_mut().unwrap().retention_days = None
        }),
        ("dataset.min_k", DatasetNotAllowed, |r| {
            r.dataset.as_mut().unwrap().min_k = None
        }),
        ("tenant.opt_in", OptInMissing, |r| {
            r.tenant.as_mut().unwrap().opt_in_active_on_current_terms = None
        }),
        ("tenant.region", EuTransferNotApproved, |r| {
            r.tenant.as_mut().unwrap().region_code = None
        }),
        ("tenant.contributors", KAnonymity, |r| {
            r.tenant.as_mut().unwrap().member_contributors = None
        }),
        ("recipient", RecipientNotQualified, |r| r.recipient = None),
        ("recipient.active", RecipientNotQualified, |r| {
            r.recipient.as_mut().unwrap().active = None
        }),
        ("recipient.no_reid", RecipientNotQualified, |r| {
            r.recipient.as_mut().unwrap().no_reidentify_no_join = None
        }),
        ("recipient.region", RecipientNotQualified, |r| {
            r.recipient.as_mut().unwrap().region_allowed = None
        }),
        ("platform_clearance", ProvenanceNotCleared, |r| {
            r.platform_clearance = None
        }),
        ("exclusions_fresh", ExclusionsStale, |r| {
            r.exclusions_fresh = None
        }),
        ("age_excluded", AgeNotExcluded, |r| {
            r.age_excluded_at_source = None
        }),
        ("retention_ok", RetentionNotOk, |r| r.retention_ok = None),
        ("flag_enabled", KillSwitchOff, |r| r.flag_enabled = None),
        ("audit", AuditUnavailable, |r| r.audit = None),
    ]
}

fn codes(d: &Decision) -> Vec<u8> {
    d.deny_codes.iter().map(|c| c.code()).collect()
}

#[test]
fn all_good_allows_dry_run_only() {
    let d = evaluate(&good());
    assert_eq!(d.outcome, Outcome::Allow, "{:?}", d.deny_codes);
    assert!(d.deny_codes.is_empty());
    assert!(d.export_not_implemented);
    assert!(!d.emitted.is_empty());

    let mut live = good();
    live.mode = Mode::Live;
    let d = evaluate(&live);
    assert_eq!(d.outcome, Outcome::Deny);
    assert_eq!(d.deny_codes, vec![DenyCode::ExportNotImplemented]);
    assert!(d.emitted.is_empty(), "Live must never emit rows");
    const { assert!(!EXPORT_IMPLEMENTED) };
}

#[test]
fn per_gate_each_bad_fact_denies_with_exactly_its_code() {
    let table = bad_table();
    assert!(table.len() >= 30, "examined {}", table.len());
    for (name, _, code, m) in table {
        let mut r = good();
        m(&mut r);
        let d = evaluate(&r);
        assert_eq!(d.outcome, Outcome::Deny, "{name}");
        assert_eq!(d.deny_codes, vec![code], "{name}");
        assert!(d.emitted.is_empty(), "{name}: deny must emit nothing");
    }
}

#[test]
fn every_gate_has_at_least_one_per_gate_test() {
    let table = bad_table();
    for g in ALL_GATES {
        assert!(
            table.iter().any(|(_, id, _, _)| *id == g),
            "no case for {g:?}"
        );
    }
}

#[test]
fn deny_code_numbers_and_names_are_stable() {
    let all = [
        (DenyCode::DatasetNotAllowed, 1, "DATASET_NOT_ALLOWED"),
        (DenyCode::SchemaNotAllowed, 2, "SCHEMA_NOT_ALLOWED"),
        (DenyCode::KAnonymity, 3, "K_ANONYMITY"),
        (DenyCode::ProvenanceNotCleared, 4, "PROVENANCE_NOT_CLEARED"),
        (DenyCode::OptInMissing, 5, "OPT_IN_MISSING"),
        (
            DenyCode::RecipientNotQualified,
            6,
            "RECIPIENT_NOT_QUALIFIED",
        ),
        (DenyCode::ExclusionsStale, 7, "EXCLUSIONS_STALE"),
        (DenyCode::AgeNotExcluded, 8, "AGE_NOT_EXCLUDED"),
        (
            DenyCode::EuTransferNotApproved,
            9,
            "EU_TRANSFER_NOT_APPROVED",
        ),
        (DenyCode::RetentionNotOk, 10, "RETENTION_NOT_OK"),
        (DenyCode::KillSwitchOff, 11, "KILL_SWITCH_OFF"),
        (DenyCode::AuditUnavailable, 12, "AUDIT_UNAVAILABLE"),
        (DenyCode::ExportNotImplemented, 13, "EXPORT_NOT_IMPLEMENTED"),
    ];
    for (c, n, s) in all {
        assert_eq!((c.code(), c.name()), (n, s));
    }
}

#[test]
fn platform_provenance_allowed_only_when_cleared() {
    let mut r = good();
    r.rows[4].provenance = 3;
    r.platform_clearance = Some(BTreeSet::from([3]));
    assert_eq!(evaluate(&r).outcome, Outcome::Allow);
    r.platform_clearance = Some(BTreeSet::from([2, 4, 5]));
    assert_eq!(
        evaluate(&r).deny_codes,
        vec![DenyCode::ProvenanceNotCleared]
    );
}

#[test]
fn eu_tenant_allowed_only_with_transfer_approval() {
    let mut r = good();
    r.tenant.as_mut().unwrap().region_code = Some("FR".into());
    r.eu_transfer_approved = Some(true);
    assert_eq!(evaluate(&r).outcome, Outcome::Allow);
    r.eu_transfer_approved = None;
    assert_eq!(
        evaluate(&r).deny_codes,
        vec![DenyCode::EuTransferNotApproved]
    );
    for c in EU_REGION_CODES {
        assert_eq!(c.len(), 2);
    }
}

#[test]
fn min_k_raises_the_threshold_but_never_lowers_it() {
    let mut r = good();
    r.dataset.as_mut().unwrap().min_k = Some(65);
    let d = evaluate(&r);
    assert_eq!(d.outcome, Outcome::Allow);
    assert!(d.emitted.iter().all(|c| c.cohort_size >= 65));
    assert!(d.suppression.cells_suppressed >= 1);
    r.dataset.as_mut().unwrap().min_k = Some(1); // cannot go below DEFAULT_K
    r.rows.push(cell(49, None, false));
    let d = evaluate(&r);
    assert!(d.emitted.iter().all(|c| c.cohort_size >= DEFAULT_K));
}

#[test]
fn audit_is_committed_before_allow_and_not_for_denied_requests() {
    use std::cell::Cell;
    let calls = Cell::new(0u32);
    let counting = || {
        calls.set(calls.get() + 1);
        Ok(())
    };
    let mut r = good();
    r.audit = Some(&counting);
    assert_eq!(evaluate(&r).outcome, Outcome::Allow);
    assert_eq!(calls.get(), 1);
    r.flag_enabled = Some(false);
    assert_eq!(evaluate(&r).outcome, Outcome::Deny);
    assert_eq!(
        calls.get(),
        1,
        "no audit commit for an already-denied request"
    );
}

#[test]
fn fail_closed_every_option_none() {
    let r = ExportRequest {
        dataset: None,
        rows: vec![],
        tenant: None,
        recipient: None,
        platform_clearance: None,
        exclusions_fresh: None,
        age_excluded_at_source: None,
        retention_ok: None,
        flag_enabled: None,
        eu_transfer_approved: None,
        mode: Mode::DryRun,
        audit: None,
    };
    let d = evaluate(&r);
    assert_eq!(d.outcome, Outcome::Deny);
    assert_eq!(codes(&d), (1..=12).collect::<Vec<u8>>());
    assert!(d.emitted.is_empty());
    let mut live = r;
    live.mode = Mode::Live;
    assert_eq!(codes(&evaluate(&live)), (1..=13).collect::<Vec<u8>>());
}

#[test]
fn fail_closed_each_single_missing_fact() {
    let table = missing_table();
    assert_eq!(table.len(), 21);
    for (name, code, m) in table {
        let mut r = good();
        m(&mut r);
        let d = evaluate(&r);
        assert_eq!(d.outcome, Outcome::Deny, "{name}");
        assert!(d.deny_codes.contains(&code), "{name}: {:?}", d.deny_codes);
        assert!(d.emitted.is_empty(), "{name}");
    }
}

#[test]
fn deny_codes_are_sorted_and_deterministic() {
    let mut r = good();
    r.flag_enabled = Some(false);
    r.exclusions_fresh = Some(false);
    r.tenant.as_mut().unwrap().opt_in_active_on_current_terms = Some(false);
    r.mode = Mode::Live;
    r.audit = Some(&FAIL);
    let a = evaluate(&r);
    let b = evaluate(&r);
    assert_eq!(a, b);
    let mut sorted = a.deny_codes.clone();
    sorted.sort();
    sorted.dedup();
    assert_eq!(a.deny_codes, sorted);
    assert_eq!(codes(&a), vec![5, 7, 11, 13]); // audit not committed once denied
}

#[test]
fn mutation_removing_any_gate_is_detected() {
    // For every single-bad-fact case, an evaluator with THAT gate removed must wrongly
    // Allow (so the per-gate test above would fail against it), while the real
    // evaluator denies. A gate nothing can detect the removal of is not a gate.
    let mut checked = 0;
    for (name, gate, code, m) in bad_table() {
        let mut r = good();
        m(&mut r);
        let real = evaluate_inner(&r, None);
        assert_eq!(real.deny_codes, vec![code], "{name}");
        let mutant = evaluate_inner(&r, Some(gate));
        assert_eq!(
            mutant.outcome,
            Outcome::Allow,
            "{name}: removing {gate:?} was NOT detected (codes {:?})",
            mutant.deny_codes
        );
        checked += 1;
    }
    assert!(checked >= 30);
    // and removing a gate never changes an all-good verdict
    for g in ALL_GATES {
        assert_eq!(evaluate_inner(&good(), Some(g)).outcome, Outcome::Allow);
    }
}

#[test]
fn mutation_removing_k_gate_also_removes_suppression() {
    let mut r = good();
    r.rows.push(cell(3, None, false));
    let real = evaluate_inner(&r, None);
    assert!(real.emitted.iter().all(|c| c.cohort_size >= DEFAULT_K));
    let mutant = evaluate_inner(&r, Some(GateId::KAnonymity));
    assert!(mutant.emitted.iter().any(|c| c.cohort_size < DEFAULT_K));
}

#[test]
fn suppression_below_k_and_whole_tenant() {
    let mut r = good();
    r.rows.push(cell(3, None, false));
    let d = evaluate(&r);
    assert_eq!(d.suppression.cells_total, 6);
    assert_eq!(d.suppression.cells_suppressed, 1);
    assert_eq!(d.suppression.tenants_suppressed, 0);
    r.tenant.as_mut().unwrap().member_contributors = Some(10);
    let d = evaluate(&r);
    assert_eq!(d.suppression.tenants_suppressed, 1);
    assert_eq!(d.suppression.cells_suppressed, 6);
}

fn mask(cells: &[TrendCell]) -> Vec<bool> {
    suppress_cells(cells, 50, 7, false).0
}

#[test]
fn complementary_single_suppression_adds_second_smallest() {
    let cells = vec![
        cell(300, Some(1), true),
        cell(10, Some(1), false), // primary
        cell(90, Some(1), false),
        cell(60, Some(1), false), // smallest remaining => complementary
        cell(120, Some(1), false),
    ];
    assert_eq!(mask(&cells), vec![false, true, false, true, false]);
}

#[test]
fn complementary_ties_break_by_index() {
    let cells = vec![
        cell(300, Some(1), true),
        cell(10, Some(1), false),
        cell(60, Some(1), false),
        cell(60, Some(1), false),
    ];
    assert_eq!(mask(&cells), vec![false, true, true, false]);
}

#[test]
fn complementary_single_component_suppresses_total_too() {
    let cells = vec![cell(100, Some(1), true), cell(10, Some(1), false)];
    assert_eq!(mask(&cells), vec![true, true]);
}

#[test]
fn complementary_two_primaries_need_nothing_more() {
    let cells = vec![
        cell(300, Some(1), true),
        cell(10, Some(1), false),
        cell(20, Some(1), false),
        cell(90, Some(1), false),
    ];
    assert_eq!(mask(&cells), vec![false, true, true, false]);
}

#[test]
fn complementary_suppressed_or_duplicate_total_suppresses_group() {
    let cells = vec![
        cell(10, Some(1), true),
        cell(60, Some(1), false),
        cell(70, Some(1), false),
    ];
    assert_eq!(mask(&cells), vec![true, true, true]);
    let cells = vec![
        cell(100, Some(1), true),
        cell(100, Some(1), true),
        cell(60, Some(1), false),
    ];
    assert_eq!(mask(&cells), vec![true, true, true]);
}

#[test]
fn complementary_groups_independent_and_totalless_group_untouched() {
    let cells = vec![
        cell(10, Some(1), false), // no total in group 1: nothing subtractable
        cell(90, Some(1), false),
        cell(300, Some(2), true),
        cell(90, Some(2), false),
        cell(95, Some(2), false),
    ];
    assert_eq!(mask(&cells), vec![true, false, false, false, false]);
}

/// Deterministic xorshift64* PRNG (no dependencies).
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
    fn chance(&mut self, num: u64, den: u64) -> bool {
        self.below(den) < num
    }
}

#[test]
fn property_never_allow_unless_every_gate_passes() {
    let table = bad_table();
    let mut rng = Rng(0x9E37_79B9_7F4A_7C15);
    let (mut allows, mut denies, mut iterations) = (0u32, 0u32, 0u32);
    for _ in 0..20_000 {
        iterations += 1;
        let mut r = good();
        let mut any_bad = false;
        // Each mutator only ever breaks (never repairs) a fact, so any applied => must Deny.
        for (_, _, _, m) in &table {
            if rng.chance(1, 14) {
                m(&mut r);
                any_bad = true;
            }
        }
        let live = r.mode == Mode::Live;
        let d = evaluate(&r);
        if any_bad {
            assert_eq!(d.outcome, Outcome::Deny);
            denies += 1;
        } else {
            assert_eq!(d.outcome, Outcome::Allow);
            allows += 1;
        }
        if live {
            assert!(d.deny_codes.contains(&DenyCode::ExportNotImplemented));
            assert!(d.emitted.is_empty());
        }
        if d.outcome == Outcome::Deny {
            assert!(d.emitted.is_empty());
            assert!(!d.deny_codes.is_empty());
        }
        assert!(d.emitted.iter().all(|c| c.cohort_size >= DEFAULT_K));
        // one missing fact at a time (mutators unwrap sibling options): must Deny, no panic
        let missing = missing_table();
        let (_, _, m) = &missing[rng.below(missing.len() as u64) as usize];
        let mut r2 = good();
        m(&mut r2);
        assert_eq!(evaluate(&r2).outcome, Outcome::Deny);
    }
    assert_eq!(iterations, 20_000);
    assert!(
        allows > 100 && denies > 1000,
        "allows={allows} denies={denies}"
    );
}

#[test]
fn property_random_cells_never_emit_below_k_or_leave_recoverable_cell() {
    let mut rng = Rng(0xDEAD_BEEF_CAFE_F00D);
    let mut cases_with_total = 0u32;
    for _ in 0..20_000 {
        let n = 1 + rng.below(9) as usize;
        let mut cells = Vec::new();
        for i in 0..n {
            let group = if rng.chance(2, 3) {
                Some(rng.below(3) as u32)
            } else {
                None
            };
            let mut c = cell(rng.below(140) as u32, group, rng.chance(1, 5));
            c.tenant_id = if rng.chance(1, 30) { 9 } else { 7 };
            c.metric_code = i as u16;
            cells.push(c);
        }
        let below = rng.chance(1, 20);
        let (m, rep) = suppress_cells(&cells, 50, 7, below);
        assert_eq!(rep.cells_total as usize, cells.len());
        assert_eq!(
            rep.cells_suppressed as usize,
            m.iter().filter(|x| **x).count()
        );
        for (c, s) in cells.iter().zip(&m) {
            if !*s {
                assert!(c.cohort_size >= 50 && c.tenant_id == 7 && !below);
            }
        }
        let groups: BTreeSet<u32> = cells.iter().filter_map(|c| c.group).collect();
        for g in groups {
            let idx: Vec<usize> = (0..cells.len())
                .filter(|&i| cells[i].group == Some(g))
                .collect();
            let total_published = idx.iter().any(|&i| cells[i].is_total && !m[i]);
            if total_published {
                cases_with_total += 1;
                let sup = idx.iter().filter(|&&i| !cells[i].is_total && m[i]).count();
                assert_ne!(
                    sup, 1,
                    "single recoverable suppressed component in group {g}"
                );
            }
        }
        // full evaluator over the same cells: no Allow ever emits < k
        let mut r = good();
        r.rows = cells;
        let d = evaluate(&r);
        assert!(d.emitted.iter().all(|c| c.cohort_size >= DEFAULT_K));
        if d.outcome == Outcome::Allow {
            assert!(!d.emitted.is_empty());
        }
    }
    assert!(cases_with_total > 1000, "{cases_with_total}");
}

#[test]
fn property_arbitrary_codes_never_panic() {
    let mut rng = Rng(42);
    for _ in 0..20_000 {
        let mut r = good();
        r.rows[0].grain_code = rng.below(256) as u8;
        r.rows[0].provenance = rng.below(256) as u8;
        r.rows[0].dims = vec![rng.below(65536) as u16];
        r.dataset.as_mut().unwrap().purpose_code = Some(rng.below(256) as u8);
        r.dataset.as_mut().unwrap().min_k = Some(rng.next() as u32);
        r.tenant.as_mut().unwrap().member_contributors = Some(rng.next() as u32);
        r.tenant.as_mut().unwrap().region_code = Some(format!("{}", rng.next()));
        r.mode = if rng.chance(1, 2) {
            Mode::Live
        } else {
            Mode::DryRun
        };
        let d = evaluate(&r);
        if d.outcome == Outcome::Allow {
            assert!(r.rows[0].provenance == 1 || d.deny_codes.is_empty());
            assert!(ALLOWED_GRAINS.contains(&r.rows[0].grain_code));
            assert_eq!(r.dataset.as_ref().unwrap().purpose_code, Some(2));
            assert_eq!(r.mode, Mode::DryRun);
        }
    }
}
