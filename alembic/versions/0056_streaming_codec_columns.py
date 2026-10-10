"""Codec + protocol selection columns for svc_streaming's config and target tables.

`core/svc_streaming` could only ever encode H.264 (or copy) and push over
RTMP: the tables had nowhere to say "this community's live stream is H.265",
or "this destination is an SRT endpoint". This revision adds that, so the
Rust service (`src/spec_builder.rs`) can build HEVC/AV1 HLS outputs, SRT
pushes and per-target codec overrides straight from the database:

`streaming_configs`
- `video_codec`  `h264` | `h265` | `av1`   (default `h264`) -- the codec the
  HLS output, and every target without an override, is encoded with when
  `transcode_enabled`.
- `audio_codec`  `copy` | `aac` | `opus`   (default `copy`).

`streaming_targets`
- `protocol`     `rtmp` | `srt`            (default `rtmp`) -- picks the
  container (FLV vs MPEG-TS), and so which codecs the target can carry.
  `rtmps://` URLs are still `rtmp`: the scheme lives in the secret URL.
- `video_codec`, `audio_codec` -- nullable per-target overrides; `NULL`
  inherits (RTMP video defaults to H.264, the only codec FLV carries).

Every default reproduces the pre-migration behaviour exactly (H.264 / copy /
RTMP), so existing rows and clients are unaffected. Which combinations are
*valid* (HEVC/AV1 cannot ride RTMP; AV1 cannot ride SRT on the bundled
ffmpeg 5.1; any non-default codec needs `transcode_enabled`) is rule logic
that lives in one place -- `core/svc_streaming/src/spec_builder.rs` and
`pipeline/codec.rs` -- and is enforced at the API and again when a pipeline
is built; the CHECK constraints here only pin the vocabulary.

`ADD COLUMN IF NOT EXISTS` keeps the revision idempotent (a re-run, or an
environment where svc-streaming's own bootstrap already created a column,
is a no-op).

**Numbering note:** parallel migrations are landing on `release/v3.0.X`;
this is chained off `0053_audit_events_hash_chain` (the head when written).
Re-point `down_revision` (and renumber) at the then-current head if another
migration merges first.

Revision ID: 0056_streaming_codec_columns
Revises: 0053_audit_events_hash_chain
Create Date: 2026-10-10
"""

from __future__ import annotations

from alembic import op

revision = "0056_streaming_codec_columns"
down_revision = "0053_audit_events_hash_chain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE streaming_configs
          ADD COLUMN IF NOT EXISTS video_codec VARCHAR(8) NOT NULL DEFAULT 'h264'
            CHECK (video_codec IN ('h264', 'h265', 'av1')),
          ADD COLUMN IF NOT EXISTS audio_codec VARCHAR(8) NOT NULL DEFAULT 'copy'
            CHECK (audio_codec IN ('copy', 'aac', 'opus'))
        """
    )
    op.execute(
        """
        ALTER TABLE streaming_targets
          ADD COLUMN IF NOT EXISTS protocol VARCHAR(8) NOT NULL DEFAULT 'rtmp'
            CHECK (protocol IN ('rtmp', 'srt')),
          ADD COLUMN IF NOT EXISTS video_codec VARCHAR(8)
            CHECK (video_codec IN ('h264', 'h265', 'av1')),
          ADD COLUMN IF NOT EXISTS audio_codec VARCHAR(8)
            CHECK (audio_codec IN ('copy', 'aac', 'opus'))
        """
    )
    op.execute(
        "COMMENT ON COLUMN streaming_configs.video_codec IS "
        "'Output video codec when transcode_enabled (h264/h265/av1); "
        "pipeline/codec.rs::VideoFamily'"
    )
    op.execute(
        "COMMENT ON COLUMN streaming_configs.audio_codec IS "
        "'Output audio handling (copy/aac/opus); pipeline/codec.rs::AudioChoice'"
    )
    op.execute(
        "COMMENT ON COLUMN streaming_targets.protocol IS "
        "'Push protocol (rtmp/srt): picks the FLV vs MPEG-TS container; "
        "rtmps:// URLs are protocol=rtmp'"
    )
    op.execute(
        "COMMENT ON COLUMN streaming_targets.video_codec IS "
        "'Per-target video codec override; NULL = RTMP:h264, SRT:config video_codec'"
    )
    op.execute(
        "COMMENT ON COLUMN streaming_targets.audio_codec IS "
        "'Per-target audio codec override; NULL = config audio_codec'"
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE streaming_targets
          DROP COLUMN IF EXISTS audio_codec,
          DROP COLUMN IF EXISTS video_codec,
          DROP COLUMN IF EXISTS protocol
        """
    )
    op.execute(
        """
        ALTER TABLE streaming_configs
          DROP COLUMN IF EXISTS audio_codec,
          DROP COLUMN IF EXISTS video_codec
        """
    )
