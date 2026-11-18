//! K-anonymity and complementary suppression over trend cells (pure, deterministic).
//!
//! Algorithm (1-D summed sets). Cells sharing a `group` id are the components of one
//! published total (`is_total`). After primary suppression (`cohort_size < k`, or a
//! foreign-tenant cell, or the whole tenant below k):
//!
//! 1. A group with more than one total, or with a suppressed total, is suppressed whole
//!    (malformed or contradictory: fail closed).
//! 2. If a published total remains and EXACTLY ONE component is suppressed, the lone
//!    value is recoverable as `total - sum(others)`. Suppress a second component: the
//!    unsuppressed component with the smallest `(cohort_size, index)`. If there is no
//!    other component (single-component group), suppress the total instead.
//! 3. Zero suppressed components, or two or more, need nothing further: with two or
//!    more unknowns only their SUM is recoverable, not any single value.
//!
//! Invariant (tested): no group with an emitted total has exactly one suppressed component.

use crate::TrendCell;
use std::collections::BTreeMap;

/// Suppression accounting.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct SuppressionReport {
    /// Cells examined.
    pub cells_total: u64,
    /// Cells suppressed (primary + complementary).
    pub cells_suppressed: u64,
    /// Tenants suppressed whole (0 or 1 per request).
    pub tenants_suppressed: u32,
}

/// Compute the suppression mask for `cells`. `tenant_below_k` suppresses everything.
/// Returns one bool per input cell (`true` = suppressed) and the accounting report.
pub fn suppress_cells(
    cells: &[TrendCell],
    k: u32,
    tenant_id: u64,
    tenant_below_k: bool,
) -> (Vec<bool>, SuppressionReport) {
    let mut mask: Vec<bool> = cells
        .iter()
        .map(|c| tenant_below_k || c.cohort_size < k || c.tenant_id != tenant_id)
        .collect();

    let mut groups: BTreeMap<u32, Vec<usize>> = BTreeMap::new();
    for (i, c) in cells.iter().enumerate() {
        if let Some(g) = c.group {
            groups.entry(g).or_default().push(i);
        }
    }
    for idxs in groups.values() {
        let totals: Vec<usize> = idxs
            .iter()
            .copied()
            .filter(|&i| cells[i].is_total)
            .collect();
        let comps: Vec<usize> = idxs
            .iter()
            .copied()
            .filter(|&i| !cells[i].is_total)
            .collect();
        if totals.is_empty() {
            continue;
        }
        if totals.len() > 1 || mask[totals[0]] {
            for &i in idxs {
                mask[i] = true;
            }
            continue;
        }
        let suppressed = comps.iter().filter(|&&i| mask[i]).count();
        if suppressed == 1 {
            let next = comps
                .iter()
                .copied()
                .filter(|&i| !mask[i])
                .min_by_key(|&i| (cells[i].cohort_size, i));
            match next {
                Some(i) => mask[i] = true,
                None => mask[totals[0]] = true,
            }
        }
    }

    let report = SuppressionReport {
        cells_total: cells.len() as u64,
        cells_suppressed: mask.iter().filter(|&&m| m).count() as u64,
        tenants_suppressed: u32::from(tenant_below_k),
    };
    (mask, report)
}
