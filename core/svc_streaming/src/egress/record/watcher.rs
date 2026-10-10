//! Segment-watcher loop.
//!
//! Detects ffmpeg `-f segment` output files as they close (a new file
//! appearing means the previous one is finalized -- on a graceful stop, the
//! last file is treated as closed too, since `RecordSink::stop` is only
//! invoked once the owning pipeline's ffmpeg process has already exited)
//! and uploads each closed segment to object storage via `object_store`,
//! deleting the local copy once the upload succeeds.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::{DateTime, NaiveDateTime, Utc};
use object_store::path::Path as StorePath;
use object_store::{
    Attribute, AttributeValue, Attributes, ObjectStore, PutMultipartOptions, PutOptions,
};
use tokio::time::sleep;
use tracing::Instrument;

use crate::pipeline::model::PipelineId;
use crate::telemetry::stream::{ExternalPeer, StreamMetrics};

use super::index::{RecordingIndex, RecordingSegment};
use super::metrics::RecordMetrics;
use super::spool::FreeSpaceProbe;

/// Segment files at or above this size go through `put_multipart` instead
/// of a single `put`.
pub(super) const MULTIPART_THRESHOLD_BYTES: u64 = 8 * 1024 * 1024;
/// Multipart chunk size. Must stay >= 5 MiB per `object_store`'s
/// `MultipartUpload::put_part` docs -- most backends reject smaller
/// non-final parts.
const MULTIPART_CHUNK_BYTES: usize = 8 * 1024 * 1024;

/// Everything a single pipeline's watcher task needs -- built once in
/// `RecordSink::start` and moved into the spawned task.
pub(super) struct WatcherContext {
    pub dir: PathBuf,
    pub pipeline_id: PipelineId,
    pub tenant: String,
    pub community_id: String,
    pub profile: String,
    /// Object-store key prefix, `"{tenant}/{community_id}/{pipeline_id}"`.
    pub key_prefix: String,
    pub store: Arc<dyn ObjectStore>,
    pub index: RecordingIndex,
    pub metrics: RecordMetrics,
    pub stream_metrics: StreamMetrics,
    pub free_space_probe: Arc<dyn FreeSpaceProbe>,
    /// `STREAM_DATA_DIR` root -- free space is checked here, not the
    /// per-pipeline subdirectory, since the whole mount is the shared spool
    /// budget.
    pub local_root: PathBuf,
    pub min_free_bytes: u64,
    pub poll_interval: Duration,
    pub retry_base_delay: Duration,
    pub retry_max_delay: Duration,
    pub max_attempts_per_cycle: u32,
}

struct SegmentFile {
    file_name: String,
    path: PathBuf,
    size_bytes: u64,
}

/// Runs until `stop_flag` is set (uploads the final in-progress segment
/// before returning) or the local spool's free space drops below
/// `ctx.min_free_bytes` (stops immediately, without uploading the
/// still-open segment -- that segment is left on disk for operator
/// recovery since the pipeline itself must also stop writing to it).
pub(super) async fn run(ctx: WatcherContext, stop_flag: Arc<AtomicBool>) {
    let mut uploaded: HashSet<String> = HashSet::new();

    loop {
        let stopping = stop_flag.load(Ordering::SeqCst);

        match ctx.free_space_probe.free_bytes(&ctx.local_root) {
            Ok(free) if free < ctx.min_free_bytes => {
                tracing::error!(
                    pipeline_id = %ctx.pipeline_id,
                    free_bytes = free,
                    min_free_bytes = ctx.min_free_bytes,
                    "recording spool free space exhausted, stopping recording"
                );
                ctx.metrics
                    .upload_failures_total
                    .with_label_values(&["spool_exhausted"])
                    .inc();
                return;
            }
            Ok(_) => {}
            Err(err) => {
                tracing::warn!(
                    pipeline_id = %ctx.pipeline_id,
                    error = %err,
                    "free space probe failed, continuing without a spool check this cycle"
                );
            }
        }

        let entries = match list_segment_files(&ctx.dir).await {
            Ok(entries) => entries,
            Err(err) => {
                tracing::warn!(
                    pipeline_id = %ctx.pipeline_id,
                    error = %err,
                    "failed to list recording segment directory"
                );
                Vec::new()
            }
        };

        report_spool_bytes(&ctx, &entries);

        // Only files before the newest one are guaranteed closed while
        // ffmpeg is still running (it's actively writing the last one). On
        // a graceful stop every remaining file is closed.
        let closed_count = if stopping {
            entries.len()
        } else {
            entries.len().saturating_sub(1)
        };

        for entry in &entries[..closed_count] {
            if uploaded.contains(&entry.file_name) {
                continue;
            }
            if upload_segment(&ctx, entry).await.is_ok() {
                uploaded.insert(entry.file_name.clone());
            }
            // On failure, `upload_segment` already logged/metriced the
            // cause and left the file in place -- the next poll cycle
            // retries it.
        }

        if stopping {
            return;
        }

        sleep(ctx.poll_interval).await;
    }
}

fn report_spool_bytes(ctx: &WatcherContext, entries: &[SegmentFile]) {
    let total: u64 = entries.iter().map(|entry| entry.size_bytes).sum();
    ctx.metrics
        .spool_bytes
        .set(i64::try_from(total).unwrap_or(i64::MAX));
}

async fn list_segment_files(dir: &Path) -> std::io::Result<Vec<SegmentFile>> {
    let mut read_dir = tokio::fs::read_dir(dir).await?;
    let mut out = Vec::new();
    while let Some(entry) = read_dir.next_entry().await? {
        let file_type = entry.file_type().await?;
        if !file_type.is_file() {
            continue;
        }
        let file_name = entry.file_name().to_string_lossy().into_owned();
        if !file_name.ends_with(".ts") {
            continue;
        }
        let metadata = entry.metadata().await?;
        out.push(SegmentFile {
            file_name,
            path: entry.path(),
            size_bytes: metadata.len(),
        });
    }
    out.sort_by(|a, b| a.file_name.cmp(&b.file_name));
    Ok(out)
}

/// Parses the `strftime`-formatted segment start time embedded in the
/// filename ffmpeg wrote (`%Y%m%d%H%M%S.ts`, see
/// `RecordSink::ffmpeg_output_args`). Falls back to `None` (caller uses
/// "now") for any name that doesn't match -- defensive, not expected in
/// normal operation.
fn parse_started_at(file_name: &str) -> Option<DateTime<Utc>> {
    let stem = file_name.strip_suffix(".ts")?;
    let naive = NaiveDateTime::parse_from_str(stem, "%Y%m%d%H%M%S").ok()?;
    Some(naive.and_utc())
}

async fn upload_segment(ctx: &WatcherContext, entry: &SegmentFile) -> Result<(), ()> {
    let key = format!("{}/{}", ctx.key_prefix, entry.file_name);
    let started_at = parse_started_at(&entry.file_name).unwrap_or_else(Utc::now);
    let span = tracing::info_span!(
        "recording_upload",
        pipeline_id = %ctx.pipeline_id,
        key = %key,
        size_bytes = entry.size_bytes,
    );

    async move {
        for attempt in 0..ctx.max_attempts_per_cycle {
            let attempt_started = Instant::now();
            match ctx
                .stream_metrics
                .time_external(
                    ExternalPeer::ObjectStore,
                    try_upload_once(ctx, entry, &key, started_at),
                )
                .await
            {
                Ok(()) => {
                    let uploaded_at = Utc::now();
                    ctx.metrics
                        .upload_duration_seconds
                        .with_label_values(&["success"])
                        .observe(attempt_started.elapsed().as_secs_f64());
                    if let Err(err) = tokio::fs::remove_file(&entry.path).await {
                        tracing::warn!(
                            pipeline_id = %ctx.pipeline_id,
                            key = %key,
                            error = %err,
                            "uploaded recording segment but failed to delete the local copy"
                        );
                    }
                    ctx.metrics.segments_uploaded_total.inc();
                    ctx.metrics.upload_bytes_total.inc_by(entry.size_bytes);
                    ctx.index.record(RecordingSegment {
                        pipeline_id: ctx.pipeline_id,
                        tenant: ctx.tenant.clone(),
                        community_id: ctx.community_id.clone(),
                        profile: ctx.profile.clone(),
                        filename: entry.file_name.clone(),
                        key: key.clone(),
                        started_at,
                        uploaded_at,
                        size_bytes: entry.size_bytes,
                    });
                    tracing::info!(
                        pipeline_id = %ctx.pipeline_id,
                        key = %key,
                        size_bytes = entry.size_bytes,
                        attempt,
                        "recording segment uploaded"
                    );
                    return Ok(());
                }
                Err(err) => {
                    ctx.metrics
                        .upload_duration_seconds
                        .with_label_values(&["failure"])
                        .observe(attempt_started.elapsed().as_secs_f64());
                    let (reason, cause) = classify_error(&err);
                    ctx.metrics
                        .upload_failures_total
                        .with_label_values(&[reason])
                        .inc();
                    tracing::warn!(
                        pipeline_id = %ctx.pipeline_id,
                        key = %key,
                        attempt,
                        cause,
                        error = %err,
                        "recording segment upload failed, keeping local file"
                    );
                    let last_attempt = attempt + 1 == ctx.max_attempts_per_cycle;
                    if !last_attempt {
                        sleep(backoff_delay(ctx, attempt)).await;
                    }
                }
            }
        }
        Err(())
    }
    .instrument(span)
    .await
}

fn backoff_delay(ctx: &WatcherContext, attempt: u32) -> Duration {
    let multiplier = 1u32.checked_shl(attempt.min(16)).unwrap_or(u32::MAX);
    ctx.retry_base_delay
        .saturating_mul(multiplier)
        .min(ctx.retry_max_delay)
}

fn build_attributes(ctx: &WatcherContext, started_at: DateTime<Utc>) -> Attributes {
    let mut attrs = Attributes::new();
    attrs.insert(
        Attribute::Metadata("pipeline-id".into()),
        AttributeValue::from(ctx.pipeline_id.to_string()),
    );
    attrs.insert(
        Attribute::Metadata("profile".into()),
        AttributeValue::from(ctx.profile.clone()),
    );
    attrs.insert(
        Attribute::Metadata("started-at".into()),
        AttributeValue::from(started_at.to_rfc3339()),
    );
    attrs
}

fn local_io_error(err: std::io::Error) -> object_store::Error {
    object_store::Error::Generic {
        store: "local-spool",
        source: Box::new(err),
    }
}

async fn try_upload_once(
    ctx: &WatcherContext,
    entry: &SegmentFile,
    key: &str,
    started_at: DateTime<Utc>,
) -> Result<(), object_store::Error> {
    let location = StorePath::from(key);
    let attributes = build_attributes(ctx, started_at);

    if entry.size_bytes >= MULTIPART_THRESHOLD_BYTES {
        upload_multipart(ctx, entry, &location, attributes).await
    } else {
        let bytes = tokio::fs::read(&entry.path).await.map_err(local_io_error)?;
        ctx.store
            .put_opts(
                &location,
                bytes.into(),
                PutOptions {
                    attributes,
                    ..Default::default()
                },
            )
            .await
            .map(|_| ())
    }
}

async fn upload_multipart(
    ctx: &WatcherContext,
    entry: &SegmentFile,
    location: &StorePath,
    attributes: Attributes,
) -> Result<(), object_store::Error> {
    use tokio::io::AsyncReadExt;

    let mut upload = ctx
        .store
        .put_multipart_opts(
            location,
            PutMultipartOptions {
                attributes,
                ..Default::default()
            },
        )
        .await?;

    let mut file = tokio::fs::File::open(&entry.path)
        .await
        .map_err(local_io_error)?;
    let mut buf = vec![0u8; MULTIPART_CHUNK_BYTES];
    loop {
        let n = file.read(&mut buf).await.map_err(local_io_error)?;
        if n == 0 {
            break;
        }
        upload.put_part(buf[..n].to_vec().into()).await?;
    }
    upload.complete().await.map(|_| ())
}

/// Classifies an `object_store::Error` into a stable metric-label `reason`
/// and the human-readable `cause` the S8 task scope requires WARN logs to
/// carry verbatim (`"s3 endpoint unreachable"`, `"access denied (403)"`,
/// `"bucket not found"`).
fn classify_error(err: &object_store::Error) -> (&'static str, &'static str) {
    match err {
        object_store::Error::NotFound { .. } => ("bucket_not_found", "bucket not found"),
        object_store::Error::PermissionDenied { .. }
        | object_store::Error::Unauthenticated { .. } => ("access_denied", "access denied (403)"),
        object_store::Error::Generic { source, .. } => {
            let msg = source.to_string().to_lowercase();
            const UNREACHABLE_MARKERS: [&str; 6] = [
                "connect",
                "connection",
                "dns",
                "timed out",
                "timeout",
                "unreachable",
            ];
            if UNREACHABLE_MARKERS
                .iter()
                .any(|needle| msg.contains(needle))
            {
                ("endpoint_unreachable", "s3 endpoint unreachable")
            } else {
                ("upload_failed", "upload failed")
            }
        }
        _ => ("upload_failed", "upload failed"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_started_at_reads_the_strftime_pattern() {
        let parsed = parse_started_at("20260911120000.ts").expect("must parse");
        assert_eq!(parsed.to_rfc3339(), "2026-09-11T12:00:00+00:00");
    }

    #[test]
    fn parse_started_at_rejects_non_matching_names() {
        assert!(parse_started_at("not-a-timestamp.ts").is_none());
        assert!(parse_started_at("20260911120000.mp4").is_none());
    }

    #[test]
    fn classify_error_maps_not_found_to_bucket_not_found() {
        let err = object_store::Error::NotFound {
            path: "x".into(),
            source: "missing".into(),
        };
        assert_eq!(
            classify_error(&err),
            ("bucket_not_found", "bucket not found")
        );
    }

    #[test]
    fn classify_error_maps_permission_denied_to_access_denied() {
        let err = object_store::Error::PermissionDenied {
            path: "x".into(),
            source: "denied".into(),
        };
        assert_eq!(
            classify_error(&err),
            ("access_denied", "access denied (403)")
        );
    }

    #[test]
    fn classify_error_maps_connection_refused_to_endpoint_unreachable() {
        let err = object_store::Error::Generic {
            store: "s3",
            source: "connection refused".into(),
        };
        assert_eq!(
            classify_error(&err),
            ("endpoint_unreachable", "s3 endpoint unreachable")
        );
    }

    #[test]
    fn classify_error_falls_back_to_upload_failed() {
        let err = object_store::Error::Generic {
            store: "s3",
            source: "500 internal server error".into(),
        };
        assert_eq!(classify_error(&err), ("upload_failed", "upload failed"));
    }

    #[test]
    fn backoff_delay_is_exponential_and_capped() {
        let ctx = test_ctx();
        assert_eq!(backoff_delay(&ctx, 0), Duration::from_millis(100));
        assert_eq!(backoff_delay(&ctx, 1), Duration::from_millis(200));
        assert_eq!(backoff_delay(&ctx, 2), Duration::from_millis(400));
        assert_eq!(backoff_delay(&ctx, 20), Duration::from_secs(1));
    }

    #[tokio::test]
    async fn try_upload_once_uses_multipart_for_segments_at_or_above_the_threshold() {
        use object_store::memory::InMemory;
        use object_store::ObjectStoreExt;

        let store: Arc<dyn ObjectStore> = Arc::new(InMemory::new());
        let dir = std::env::temp_dir().join(format!(
            "svc-streaming-watcher-multipart-{}",
            uuid::Uuid::new_v4()
        ));
        tokio::fs::create_dir_all(&dir)
            .await
            .expect("create test dir");
        let path = dir.join("large.ts");
        // Just over the multipart threshold, spanning two chunks (last
        // chunk under the 5 MiB minimum, which is fine since only
        // non-final parts have that floor).
        let size = MULTIPART_THRESHOLD_BYTES as usize + 1024;
        tokio::fs::write(&path, vec![0x42u8; size])
            .await
            .expect("write large fake segment");

        let entry = SegmentFile {
            file_name: "large.ts".to_string(),
            path: path.clone(),
            size_bytes: size as u64,
        };
        let mut ctx = test_ctx();
        ctx.store = store.clone();
        let key = "tenant-1/community-1/pipeline/large.ts";

        let result = try_upload_once(&ctx, &entry, key, Utc::now()).await;
        assert!(result.is_ok(), "multipart upload must succeed: {result:?}");

        let fetched = store
            .get(&StorePath::from(key))
            .await
            .expect("uploaded object must exist in the store");
        let bytes = fetched.bytes().await.expect("read uploaded bytes");
        assert_eq!(bytes.len(), size);

        tokio::fs::remove_dir_all(&dir).await.ok();
    }

    fn test_ctx() -> WatcherContext {
        use super::super::metrics::register_metrics;
        use super::super::spool::DfFreeSpaceProbe;
        use object_store::memory::InMemory;
        use uuid::Uuid;

        WatcherContext {
            dir: PathBuf::from("/tmp"),
            pipeline_id: Uuid::nil(),
            tenant: "tenant-1".into(),
            community_id: "community-1".into(),
            profile: "1080p60".into(),
            key_prefix: "tenant-1/community-1/00000000-0000-0000-0000-000000000000".into(),
            store: Arc::new(InMemory::new()),
            index: RecordingIndex::new(),
            metrics: register_metrics(&prometheus::Registry::new()),
            stream_metrics: StreamMetrics::shared(),
            free_space_probe: Arc::new(DfFreeSpaceProbe),
            local_root: PathBuf::from("/tmp"),
            min_free_bytes: 1024 * 1024 * 1024,
            poll_interval: Duration::from_millis(10),
            retry_base_delay: Duration::from_millis(100),
            retry_max_delay: Duration::from_secs(1),
            max_attempts_per_cycle: 3,
        }
    }
}
