//! Compile-fail test proving `InvokeScope` cannot be constructed via a
//! struct literal from outside `bundle_capability_gate` (spec SS5.1, Gemini
//! condition 1's "(1) a static/type-level test... asserts none contains a
//! tenant/community/app_id-shaped parameter" -- adapted here to this
//! crate's own boundary: no external code, guest-facing or otherwise, can
//! ever build a scope except through `HostInvokeScopeBuilder`).
//!
//! No `.stderr` snapshot is committed alongside the fixture -- `trybuild`
//! then only asserts the fixture fails to compile at all, which is the
//! entire property under test (a private-field error's exact wording is not
//! part of the contract this test protects).

#[test]
fn invoke_scope_has_no_public_struct_literal_constructor() {
    let t = trybuild::TestCases::new();
    t.compile_fail("tests/compile-fail/invoke_scope_no_struct_literal.rs");
}
