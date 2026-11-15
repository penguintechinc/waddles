//! Compiles `proto/waddles/streaming/internal/v1/rooms.proto` (the hub-api -> svc-streaming
//! `RoomsService` contract) into tonic stubs. Points at the checked-in repo-root `.proto` so the
//! Rust server and the Python client stubs (`hub_api/grpc_internal/pb`) cannot drift from a
//! second copy.

fn main() {
    let manifest_dir = std::env::var("CARGO_MANIFEST_DIR")
        .unwrap_or_else(|e| panic!("build.rs: CARGO_MANIFEST_DIR unset: {e}"));
    let proto_root = std::path::Path::new(&manifest_dir).join("../../proto");
    let proto = proto_root.join("waddles/streaming/internal/v1/rooms.proto");
    println!("cargo:rerun-if-changed={}", proto.display());

    tonic_prost_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&[proto], &[proto_root])
        .unwrap_or_else(|e| panic!("failed to compile rooms.proto: {e}"));
}
