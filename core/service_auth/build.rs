//! Compiles `proto/waddles/hub/internal/v1/service_auth.proto` (the machine-JWT bootstrap
//! contract: `ServiceAuthService.MintServiceToken` / `GetServiceJwks`) into tonic stubs.
//! Points at the checked-in repo-root `.proto` so Rust and the Python server stubs
//! (`hub_api/grpc_internal/pb`) cannot drift from a second copy; the server half is generated
//! too because `test_support::MockHub` serves it in dependents' tests.

fn main() {
    let manifest_dir = std::env::var("CARGO_MANIFEST_DIR")
        .unwrap_or_else(|e| panic!("build.rs: CARGO_MANIFEST_DIR unset: {e}"));
    let proto_root = std::path::Path::new(&manifest_dir).join("../../proto");
    let proto = proto_root.join("waddles/hub/internal/v1/service_auth.proto");
    println!("cargo:rerun-if-changed={}", proto.display());

    tonic_prost_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&[proto], &[proto_root])
        .unwrap_or_else(|e| panic!("failed to compile service_auth.proto: {e}"));
}
