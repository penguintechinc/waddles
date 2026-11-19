# Feature Flags vs License Domain Bypass

Decision (2026-11-12): the hardcoded license-bypass domains (`*.waddles.app`,
`*.penguincloud.io`, `*.penguintech.cloud`) affect **license/entitlement
checks only**. They never affect ordinary feature flags or
`{product}.disable-{mechanism}` kill switches.

| Check | Bypass domain | Other domain |
|-------|---------------|--------------|
| `tier()` / `get_tier()` | Enterprise | license server |
| `check_feature()` / licensed (tiered) flag tier gate | granted | license server |
| Ordinary feature flag | PostHog -> `FLAG_*` env -> default OFF | same |
| Kill switch `waddles.*.disable-*` | PostHog -> `FLAG_*` env -> default OFF (mechanism ON) | same |

- Env var name: `FLAG_` + key upper-cased, `.`/`-` -> `_`
  (e.g. `waddles.core.disable-db-bundle-config` -> `FLAG_WADDLES_CORE_DISABLE_DB_BUNDLE_CONFIG`).
- Never-seen flags are OFF everywhere. A kill switch that is OFF leaves its mechanism enabled.
- Licensed flags, entitlement and tier checks are never env-overridable (`flask_core.entitlement`;
  Rust: `tier()` / `check_feature()` / `check_tier()` never read `FLAG_*`).

## Precedence (exact)

1. **PostHog value if the snapshot defines the key** -- an explicit PostHog OFF beats `FLAG_*=true`.
2. Else the **`FLAG_*` ENV baseline**.
3. Else **OFF**.

The same order applies at every site (`svc_{action,process,ingest,presentation,analytics}`,
command-latency kill switch, canary, analytics producer): never `env || posthog`.

### Unseen keys and caller defaults (`flags.enabled` host call)

`FlagSource::flag_value` reports `Some((None, fresh))` for a key a present snapshot does not define
and `None` when no snapshot exists; both consult the ENV baseline, then OFF. A caller-supplied
`default_value` is honoured **only for `disable-*` kill switches** (ON-by-absence policy); for any
other key `default_value = true` never fails open (also when no snapshot / no client /
`FLAGS_CAPABILITY_ENABLED=false`).

### Licensed capabilities

`ai.generate` and `media.*` are gated by a plain feature flag AND a tier check (Professional+).
`FLAG_WADDLES_BUNDLE_AI_CAPABILITY=true` / `FLAG_WADDLES_HARNESS_MEDIA_GENERATION=true` only turn the
flag on; below Professional the tier gate still refuses (pinned by
`env_flag_baseline_true_does_not_grant_{ai,media}_capability_below_professional_tier`).

## Why

`penguin_licensing::LicenseClient::flag_enabled` returns `true` for every key
when the deployment domain is a bypass domain. Every Rust data-plane service
registers its own `*.waddles.app` domain, so before this fix every flag read
ON and no kill switch could be controlled from PostHog. Rust services now read
flags from `LicenseClient::snapshot()` directly (never the library's `flag_enabled`
for the value) through each crate's `PosthogFlag::flag_on` helper
(`core/svc_{action,process,ingest,presentation}`; `svc_analytics` reads the snapshot inline) and use
`bypass_active()` / `tier()` / `check_feature()` only for entitlement.
`LicenseClient::validate()` is called as the refresh trigger because `flag_enabled`
returns early under bypass and would leave the snapshot empty. The proper long-term fix is
upstream in `penguin-libs` (`flag_enabled` must not short-circuit on bypass).

## Alpha baselines

Flags the bypass used to force ON are kept ON on alpha via
`config.dataPlaneFlagBaselines` (`k8s/helm/waddlebot/values-alpha.yaml`,
`values-dal2alpha.yaml`; keys must start with `FLAG_`, enforced at render;
base values render none, so beta/prod inherit nothing). PostHog still overrides.
The render also fails on kill-switch keys (`_DISABLE_`) and on licensed-flag keys
(`FLAG_WADDLES_COMMUNITY_CONNECTIONS`, `..._COMPLIANCE_EXTERNAL_KMS`, `..._AUTH_SSO_SAML`,
`..._AUTH_SSO_CUSTOM`, `..._HUB_API_SSO`, `..._TENANCY_WHITELABEL`, `..._AI_BYOK`).
