//! `world stage-tools` (P3b Task 1): the new world type-checks, its linker arm
//! registers the `tool` host import, and a minimal real component (imports
//! `tool.invoke`, exports `tool-provider.call`) pre-links against it. Also
//! pins the isolation direction: a `stage-v2` linker never registers `tool`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use bundle_executor::config::CliConfig;
use bundle_executor::engine::{build_engine, build_linker_for, stage_tools_world};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use common::test_config;
use wasmtime::component::Component;

const FIXTURE: &[u8] = include_bytes!("fixtures/stage_tools_fixture.wasm");

fn manifest(world: WitWorld) -> VerifiedManifest {
    VerifiedManifest::new(
        "waddles.test.stage-tools-fixture",
        "sha256:aa",
        world,
        Default::default(),
    )
}

fn cfg() -> CliConfig {
    test_config()
}

#[test]
fn stage_tools_fixture_pre_links_against_the_stage_tools_linker() {
    let engine = build_engine(&cfg()).unwrap();
    let linker = build_linker_for(&engine, &manifest(WitWorld::StageTools)).unwrap();
    let component = Component::new(&engine, FIXTURE).unwrap();
    let pre = linker.instantiate_pre(&component).unwrap();
    // Typed pre-link: proves the component exports exactly `tool-provider`.
    stage_tools_world::StageToolsPre::new(pre).unwrap();
}

#[test]
fn stage_v2_linker_does_not_register_tool_import() {
    let engine = build_engine(&cfg()).unwrap();
    let linker = build_linker_for(&engine, &manifest(WitWorld::StageV2)).unwrap();
    let component = Component::new(&engine, FIXTURE).unwrap();
    assert!(
        linker.instantiate_pre(&component).is_err(),
        "a stage-v2 linker must not satisfy the `tool` import"
    );
}
