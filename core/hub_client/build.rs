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

    tonic_prost_build::configure()
        .build_server(false)
        .build_client(true)
        .compile_protos(
            &[
                proto_dir.join("identity.proto"),
                proto_dir.join("key.proto"),
            ],
            &[proto_root],
        )
        .expect("failed to compile waddles.hub.internal.v1 protos");
}
