//! Compiles `proto/waddles/presentation/internal/v1/*.proto` (client AND server stubs: the stage
//! hosts call it, svc-presentation serves it) from the checked-in repo-root `proto/` tree, the
//! same single-source-of-truth approach as `core/hub_client/build.rs`.

fn main() {
    let manifest_dir = std::env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is set by cargo");
    let proto_root = std::path::Path::new(&manifest_dir).join("../../proto");
    let proto_dir = proto_root.join("waddles/presentation/internal/v1");

    let mut protos: Vec<std::path::PathBuf> = std::fs::read_dir(&proto_dir)
        .expect("failed to read the waddles.presentation.internal.v1 proto directory")
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
        .expect("failed to compile waddles.presentation.internal.v1 protos");
}
