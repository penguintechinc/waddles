//! End-to-end recording watcher coverage: segments are written to disk the
//! way ffmpeg's `-f segment` muxer would (see `egress_record_args.rs` for
//! the argv this reproduces), and the watcher spawned by `RecordSink::start`
//! must detect each closed segment, upload it to object storage, delete the
//! local copy, and index it -- then upload the final in-progress segment on
//! `RecordSink::stop`.
//!
//! Exercises `RecordSink` purely through its public API against an
//! `object_store::memory::InMemory` store, per the S8 task scope.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use object_store::memory::InMemory;
use object_store::path::Path as StorePath;
use object_store::ObjectStoreExt;
use svc_streaming::egress::record::{register_metrics, RecordSink};
use svc_streaming::egress::OutputSink;
use svc_streaming::pipeline::{ObjectStoreRef, OutputSpec, PipelineId};
use uuid::Uuid;

fn target() -> ObjectStoreRef {
    ObjectStoreRef {
        store: "s3-recordings".into(),
        prefix: "tenant-1/community-1".into(),
    }
}

fn segment_dir(root: &std::path::Path, pipeline_id: PipelineId) -> PathBuf {
    root.join("tenant-1/community-1")
        .join(pipeline_id.to_string())
}

async fn write_segment(dir: &std::path::Path, name: &str, size_bytes: usize) {
    tokio::fs::create_dir_all(dir)
        .await
        .expect("create segment dir");
    tokio::fs::write(dir.join(name), vec![0xAB; size_bytes])
        .await
        .expect("write fake segment file");
}

/// Polls until `sink` has indexed at least `expected` segments for
/// `community`, or fails after a bounded deadline. The watcher records a
/// segment in the index only after the upload and the local delete both
/// finished, so this is the deterministic completion signal -- a fixed sleep
/// races the watcher's poll cycle under CPU contention (e.g. `cargo llvm-cov`).
async fn wait_for_indexed(sink: &RecordSink, community: &str, expected: usize) {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let indexed = sink.index().list(community).len();
        if indexed >= expected {
            return;
        }
        assert!(
            Instant::now() < deadline,
            "timed out after 10s waiting for {expected} indexed segments, saw {indexed}"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn closed_segments_are_uploaded_deleted_locally_and_indexed_then_the_final_segment_uploads_on_stop(
) {
    let root = std::env::temp_dir().join(format!("svc-streaming-record-upload-{}", Uuid::new_v4()));
    let store: Arc<InMemory> = Arc::new(InMemory::new());
    let metrics = register_metrics(&prometheus::Registry::new());
    let sink = RecordSink::new(
        root.clone(),
        store.clone() as Arc<dyn object_store::ObjectStore>,
        metrics,
    )
    .with_poll_interval(Duration::from_millis(30));

    let pipeline_id = Uuid::new_v4();
    let dir = segment_dir(&root, pipeline_id);

    sink.start(
        pipeline_id,
        OutputSpec::Record {
            profile: "1080p60".into(),
            target: target(),
        },
    )
    .await
    .expect("start must succeed");

    // ffmpeg's segment muxer: a new file only appears once the previous one
    // is finalized. Writing the next file is what closes the previous one,
    // so wait for the watcher's completion signal (index entry) before
    // moving on rather than sleeping a fixed time.
    write_segment(&dir, "20260911120000.ts", 1024).await;
    write_segment(&dir, "20260911120100.ts", 2048).await;
    wait_for_indexed(&sink, "community-1", 1).await;
    write_segment(&dir, "20260911120200.ts", 4096).await;
    wait_for_indexed(&sink, "community-1", 2).await;

    // Only the first two are closed (a third, still-open file exists) --
    // both must already be uploaded, deleted locally, and indexed.
    assert!(
        !dir.join("20260911120000.ts").exists(),
        "closed segment 1 must be deleted locally"
    );
    assert!(
        !dir.join("20260911120100.ts").exists(),
        "closed segment 2 must be deleted locally"
    );
    assert!(
        dir.join("20260911120200.ts").exists(),
        "the still-open segment must not be touched yet"
    );

    sink.stop(pipeline_id).await.expect("stop must succeed");

    // `stop` finalizes the last (previously open) segment too.
    assert!(
        !dir.join("20260911120200.ts").exists(),
        "the final segment must upload on stop"
    );

    let segments = sink.index().list("community-1");
    assert_eq!(
        segments.len(),
        3,
        "all three segments must be indexed: {segments:?}"
    );
    let mut filenames: Vec<&str> = segments.iter().map(|s| s.filename.as_str()).collect();
    filenames.sort_unstable();
    assert_eq!(
        filenames,
        vec![
            "20260911120000.ts",
            "20260911120100.ts",
            "20260911120200.ts"
        ]
    );
    for segment in &segments {
        assert_eq!(segment.pipeline_id, pipeline_id);
        assert_eq!(segment.tenant, "tenant-1");
        assert_eq!(segment.community_id, "community-1");
        assert_eq!(segment.profile, "1080p60");
    }

    // Verify the bytes actually landed in the object store, not just the
    // index -- the object key is `{prefix}/{pipeline_id}/{filename}`.
    let key = StorePath::from(format!(
        "tenant-1/community-1/{pipeline_id}/20260911120000.ts"
    ));
    let fetched = store
        .get(&key)
        .await
        .expect("segment 1 must exist in the store");
    let bytes = fetched.bytes().await.expect("read uploaded bytes");
    assert_eq!(bytes.len(), 1024);

    tokio::fs::remove_dir_all(&root).await.ok();
}

#[tokio::test(flavor = "multi_thread")]
async fn a_pipeline_with_no_segments_yet_uploads_nothing_on_stop() {
    let root = std::env::temp_dir().join(format!("svc-streaming-record-upload-{}", Uuid::new_v4()));
    let store: Arc<InMemory> = Arc::new(InMemory::new());
    let metrics = register_metrics(&prometheus::Registry::new());
    let sink = RecordSink::new(
        root.clone(),
        store as Arc<dyn object_store::ObjectStore>,
        metrics,
    )
    .with_poll_interval(Duration::from_millis(20));
    let pipeline_id = Uuid::new_v4();

    sink.start(
        pipeline_id,
        OutputSpec::Record {
            profile: "1080p60".into(),
            target: target(),
        },
    )
    .await
    .expect("start must succeed");

    sink.stop(pipeline_id)
        .await
        .expect("stop on an empty spool must not error");
    assert!(sink.index().list("community-1").is_empty());

    tokio::fs::remove_dir_all(&root).await.ok();
}
