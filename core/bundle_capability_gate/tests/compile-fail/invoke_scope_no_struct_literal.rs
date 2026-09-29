// Confused-deputy regression (spec SS5.1, Gemini condition 1): `InvokeScope`
// must have no public constructor usable from outside this crate -- every
// field is private, so a struct-literal construction from an external crate
// (exactly what a `svc_process`/`svc_action` capability implementation would
// have to do if it tried to build a scope straight out of guest-supplied
// `HostCallBody.args` instead of going through
// `HostInvokeScopeBuilder::build`) must fail to compile.
use bundle_capability_gate::InvokeScope;

fn main() {
    let _scope = InvokeScope {
        tenant_id: 1,
        community_id: 0,
        app_id: "attacker-controlled".to_string(),
        app_version: 1,
        tenant_tier: bundle_capability_gate::TenantTier::Free,
    };
}
