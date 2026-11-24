//! In-memory index of uploaded recording segments.
//!
//! Exposed so a later chunk (S2, the `/api/v1/*` control plane) can surface
//! a "recordings for this community" endpoint without this sink owning any
//! HTTP route itself -- per the S8 task scope, `RecordingIndex::list` is the
//! `GET`-able contract S2 builds against.

use std::sync::{Arc, Mutex};

use time::OffsetDateTime;

use crate::pipeline::model::PipelineId;

/// One uploaded recording segment.
#[derive(Debug, Clone, PartialEq)]
pub struct RecordingSegment {
    pub pipeline_id: PipelineId,
    pub tenant: String,
    pub community_id: String,
    pub profile: String,
    /// Segment file name only, e.g. `20260911120000.ts`.
    pub filename: String,
    /// Full object-store key, e.g.
    /// `tenant-1/community-1/<pipeline-id>/20260911120000.ts`.
    pub key: String,
    /// Segment start time, parsed from the `strftime`-formatted filename
    /// ffmpeg wrote (see `RecordSink::ffmpeg_output_args`), not the upload
    /// time.
    pub started_at: OffsetDateTime,
    pub uploaded_at: OffsetDateTime,
    pub size_bytes: u64,
}

/// Thread-safe, process-local index of every segment this instance has
/// uploaded. Cloning shares the same backing store (`Arc<Mutex<...>>`) --
/// cheap to hand a clone to each pipeline's watcher task.
#[derive(Debug, Clone, Default)]
pub struct RecordingIndex {
    segments: Arc<Mutex<Vec<RecordingSegment>>>,
}

impl RecordingIndex {
    /// Builds an empty index.
    pub fn new() -> Self {
        Self::default()
    }

    /// Records a newly uploaded segment.
    pub fn record(&self, segment: RecordingSegment) {
        // A poisoned mutex means a prior holder panicked mid-update; that
        // panic is the real problem and recovering the inner `Vec` (rather
        // than propagating a second panic here) keeps the index usable for
        // every other pipeline's watcher task instead of taking the whole
        // index down with one bad update.
        let mut guard = self
            .segments
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        guard.push(segment);
    }

    /// Returns every recorded segment for `community_id`, oldest first.
    pub fn list(&self, community_id: &str) -> Vec<RecordingSegment> {
        let guard = self
            .segments
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        guard
            .iter()
            .filter(|segment| segment.community_id == community_id)
            .cloned()
            .collect()
    }

    /// Total number of segments recorded across all communities/pipelines.
    /// Test/diagnostic helper.
    pub fn len(&self) -> usize {
        let guard = self
            .segments
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        guard.len()
    }

    /// True when no segment has been recorded yet.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;

    fn sample(community_id: &str) -> RecordingSegment {
        RecordingSegment {
            pipeline_id: Uuid::nil(),
            tenant: "tenant-1".into(),
            community_id: community_id.into(),
            profile: "1080p60".into(),
            filename: "20260911120000.ts".into(),
            key: format!("tenant-1/{community_id}/{}/20260911120000.ts", Uuid::nil()),
            started_at: OffsetDateTime::now_utc(),
            uploaded_at: OffsetDateTime::now_utc(),
            size_bytes: 1024,
        }
    }

    #[test]
    fn list_filters_by_community_id() {
        let index = RecordingIndex::new();
        index.record(sample("community-1"));
        index.record(sample("community-2"));
        index.record(sample("community-1"));

        assert_eq!(index.list("community-1").len(), 2);
        assert_eq!(index.list("community-2").len(), 1);
        assert!(index.list("community-3").is_empty());
    }

    #[test]
    fn new_index_is_empty() {
        let index = RecordingIndex::new();
        assert!(index.is_empty());
        assert_eq!(index.len(), 0);
    }

    #[test]
    fn clone_shares_the_same_backing_store() {
        let index = RecordingIndex::new();
        let clone = index.clone();
        index.record(sample("community-1"));
        assert_eq!(clone.len(), 1);
    }
}
