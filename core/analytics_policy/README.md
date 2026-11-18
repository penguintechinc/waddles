# analytics-policy

Pure-logic **ExportPolicy guard** for the analytics foundation (Phase 1). No IO, no DB,
no HTTP, no async runtime, `#![forbid(unsafe_code)]`, **zero dependencies**.

> **No export path exists in v3.0.** `EXPORT_IMPLEMENTED = false` is hard-coded: a `Live`
> request is always denied with `EXPORT_NOT_IMPLEMENTED`; `DryRun` only reports what the
> other gates would decide (and `Decision::emitted` is for inspection, never for sending).

Sellable dataset: tenant-wide ANONYMIZED TREND METADATA only (tenant x week or coarser,
closed int-enum dimensions, nothing below tenant). Deny-by-default, fail-closed: a missing
fact (`None`) or unknown code denies; nothing panics; output is deterministic (sorted codes).

`evaluate(&ExportRequest) -> Decision { outcome, deny_codes, suppression, emitted, .. }`

| # | Gate | Passes when | Deny code |
|---|------|-------------|-----------|
| 1 | Dataset | registered, named, purpose = aggregate_trend(2), classification = aggregate_anonymous(1), min_k known | 1 `DATASET_NOT_ALLOWED` |
| 2 | Schema | layer B(2); every cell grain in {week,month,quarter,all}, dims only from the closed allowlist (`dim::ALLOWED`), cell tenant == request tenant | 2 `SCHEMA_NOT_ALLOWED` |
| 3 | k-anonymity | tenant contributors >= k, rows present, something releasable; cells < k suppressed; complementary suppression | 3 `K_ANONYMITY` |
| 4 | Provenance | native(1) or platform(2..5) present in `platform_clearance`; unknown code denied | 4 `PROVENANCE_NOT_CLEARED` |
| 5 | Opt-in | tenant opt-in active on current terms | 5 `OPT_IN_MISSING` |
| 6 | Recipient | present, active, no-reidentify/no-join, region allowed | 6 `RECIPIENT_NOT_QUALIFIED` |
| 7 | Exclusions | `exclusions_fresh` | 7 `EXCLUSIONS_STALE` |
| 8 | Age | `age_excluded_at_source` (unknown age = excluded at source) | 8 `AGE_NOT_EXCLUDED` |
| 9 | EU | region known and not in `EU_REGION_CODES` (EU27+IS,LI,NO+GB+CH), unless `eu_transfer_approved` | 9 `EU_TRANSFER_NOT_APPROVED` |
| 10 | Retention | `retention_ok` and `retention_days > 0` | 10 `RETENTION_NOT_OK` |
| 11 | Kill switch | `flag_enabled` | 11 `KILL_SWITCH_OFF` |
| 12 | Audit | `audit.commit()` is `Ok` BEFORE Allow (only invoked when all other gates pass; missing hook denies) | 12 `AUDIT_UNAVAILABLE` |
| 13 | Not implemented | never passes in `Live` while `EXPORT_IMPLEMENTED` is false | 13 `EXPORT_NOT_IMPLEMENTED` |

k = `max(50, dataset.min_k)`; it can be raised, never lowered.

## Complementary suppression (1-D summed sets)

Cells sharing `group` are components of one published `is_total` cell. If exactly one
component is suppressed (< k) while the total is published, it is recoverable by
subtraction, so a second component (smallest cohort, ties by index) is suppressed too; a
lone component also suppresses the total. A suppressed or duplicate total suppresses the
whole group. Two or more suppressed components reveal only their sum. See `src/suppress.rs`.

## Tests ("a gate that cannot fail is not a gate")

Per-gate tables (every gate flips to exactly its code), single-missing-fact and all-`None`
fail-closed, a mutation harness (`evaluate_inner(req, Some(gate))` must wrongly Allow for
every per-gate case, proving each test detects that gate's removal), seeded property tests
(never Allow unless all gates pass, never emit < k, no lone recoverable cell), and
`tests/no_io_deps.rs` (no dependencies; no HTTP/DB/async crate in Cargo.toml/Cargo.lock).

Run: `cargo fmt --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked && cargo deny check`
