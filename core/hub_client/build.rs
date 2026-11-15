//! Compiles `proto/waddles/hub/internal/v1/*.proto` into Rust client
//! stubs via `tonic-prost-build`. Deliberately points at the checked-in
//! `.proto` source of truth (not a vendored/generated copy) so the Rust
//! and Python (`hub_api/grpc_internal/pb`, `scripts/compile_protos_v1.sh`)
//! sides can never drift from each other, only independently from the
//! shared proto.

fn main() {
    let manifest_dir = std::env::var("CARGO_MANIFEST_DIR").unwrap();
    let proto_root = std::path::Path::new(&manifest_dir).join("../../proto");
    let proto_dir = proto_root.join("waddles/hub/internal/v1");

    // Every `*.proto` in the directory is compiled (sorted, so the build is deterministic):
    // a new service area adds a file, never an edit to this list.
    let mut protos: Vec<std::path::PathBuf> = std::fs::read_dir(&proto_dir)
        .expect("failed to read the waddles.hub.internal.v1 proto directory")
        .map(|entry| entry.expect("unreadable proto directory entry").path())
        .filter(|path| path.extension().is_some_and(|ext| ext == "proto"))
        .collect();
    protos.sort();
    assert!(!protos.is_empty(), "no .proto files found in {proto_dir:?}");
    println!("cargo:rerun-if-changed={}", proto_dir.display());

    tonic_prost_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&protos, &[proto_root])
        .expect("failed to compile waddles.hub.internal.v1 protos");
}
