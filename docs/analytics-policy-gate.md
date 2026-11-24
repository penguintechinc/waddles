# Analytics ExportPolicy gate (`core/analytics_policy`)

Phase 1 of the analytics foundation (no external export). `core/analytics_policy` is a
dependency-free pure-logic guard: deny-by-default and fail-closed over 13 gates (dataset,
schema/grain/dimension allowlist, k >= 50 with complementary suppression, provenance
clearance, tenant opt-in, recipient contract, exclusion freshness, age excluded at source,
EU/EEA/UK transfer hold, retention, kill switch, audit-before-allow, not-implemented).

**v3.0 has no export path:** `EXPORT_IMPLEMENTED = false`, so `Live` is always denied with
`EXPORT_NOT_IMPLEMENTED`; `DryRun` reports what the decision would be. Gate table, deny
codes and the suppression algorithm: `core/analytics_policy/README.md`. Merge this page into
`docs/analytics-foundation.md` once that file exists.
