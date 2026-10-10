# svc-streaming (Rust)

The Waddles A/V data plane: a single Rust service that ingests RTMP/SRT/WHIP
streams, transcodes via `ffmpeg`, and serves/forwards/records the output.
Replaces the Python + ffmpeg alpha (`app.py`, `blueprints/`, `services/` in
this same directory) once the media pipeline chunks below land; both builds
coexist in-tree during the transition per issue #287.

Never call any output-forwarding feature "restream"/"Restream" — trademarked
competitor product name. Use "streaming proxy" / "forward to targets".

## Transform Matrix

```
Input                Profile (transcode)              Output
-----                ----------------------           ------
Rtmp{stream_key}  \                               / RtmpPush{url_secret_ref, profile?}
Srt{stream_id}     \  video: Copy|H264|H265|Av1Svt /  SrtPush{url_secret_ref, profile?}
Whip{token}        --  audio: Copy|Aac|Opus       --  Hls{Ll|Std, profile}
Pull{url}          /   resolution, fps             \  Whep{profile}
                                                     \  Record{profile, ObjectStoreRef}
                                                      \ DiscordVoice{guild_id, channel_id}
```

One `PipelineSpec` = N inputs -> N named `TranscodeProfile`s -> N outputs.
Full model: `src/pipeline/model.rs`. A push output's optional `profile` names
the profile it encodes with (`None` = `profiles[0]`, the legacy binding).

## Codec Selection (H.264 / H.265 / AV1)

The output codec is chosen in the database and the API, not hard-coded:

| Where | Column / field | Values | Default |
|---|---|---|---|
| `streaming_configs` | `video_codec` | `h264` `h265` `av1` | `h264` |
| `streaming_configs` | `audio_codec` | `copy` `aac` `opus` | `copy` |
| `streaming_targets` | `protocol` | `rtmp` (also `rtmps://` URLs) `srt` | `rtmp` |
| `streaming_targets` | `video_codec`, `audio_codec` | as above, or `NULL` = inherit | `NULL` |

(Alembic `0056_streaming_codec_columns`; API: `POST/PUT .../configs`,
`POST .../targets`.) The config's codec is the **default encode**: HLS and
every target without an override use it. An RTMP target with no video
override uses H.264 (the only codec FLV carries); an SRT target inherits the
config's. Any non-default codec needs `transcode_enabled` (re-encoding is the
token-gated transcode feature).

What each container can carry with the bundled ffmpeg (Debian 5.1.x):

| Output | H.264 | H.265 | AV1 | AAC | Opus |
|---|---|---|---|---|---|
| RTMP push (FLV) | yes | **no** | **no** | yes | **no** |
| SRT push (MPEG-TS) | yes | yes | **no** | yes | yes |
| HLS (fMP4) | yes | yes (`hvc1`) | yes | yes | yes |

Anything marked **no** is rejected **loudly**: `400` from the API write
handlers (e.g. *"FLV/RTMP cannot carry HEVC/AV1 (needs enhanced-RTMP); use
HLS/SRT or H.264"*), `400` from `/start`, an error log + dropped session on
ingest, and `InvalidSpec` from `build_argv`. `ffmpeg -f tee` cannot be
relied on to refuse it: a slave whose header write fails is skipped with the
process still exiting 0, even with the default `onfail=abort` (verified on the
runtime image). Enhanced-RTMP (H.265/AV1 over RTMP) and AV1-in-MPEG-TS need
an ffmpeg 6.1+ build; when that image lands, flip the matching arms in
`pipeline/codec.rs::check_video` -- nothing else changes.

Targets that need a *different* codec than the default (an RTMP push beside
H.265 HLS) become extra profiles, encoded off one shared decode
(`-filter_complex split`, `null` filters when no resolution is set):

```
H.265 config, RTMP + SRT targets:
  decode -> split -> [default: libx265] -> tee: HLS (fMP4/hvc1) | SRT (MPEG-TS) | -f null guard
                  \-> [h264-copy: libx264] -> tee: RTMP (FLV) | -f null guard
```

## Encoders: GPU-preferred, CPU-fallback

At startup (`pipeline::encoder::detect_encoders`) the service picks, **per
codec family**, a hardware encoder only if (1) a device node exists
(`/dev/nvidiactl`/`/dev/nvidia0`, or `/dev/dri/renderD*`), (2) the encoder is
compiled into `FFMPEG_PATH`, and (3) a one-second **trial encode succeeds**.
Anything else -- no GPU, driver missing from the image, encoder not built in,
trial timeout -- falls back to `libx264`/`libx265`/`libsvtav1` with a log line
saying why. The CPU path needs nothing and works everywhere.

| Env | Default | Meaning |
|---|---|---|
| `STREAM_ENCODER` | `auto` | `auto` = GPU when proven usable else CPU; `cpu` = never use a GPU; `nvenc`/`vaapi` = prefer only that backend (still falls back to CPU) |
| `STREAM_VAAPI_DEVICE` | auto (`/dev/dri/renderD*`) | VA-API render node |

Selection is exposed as the `encoder_backend{codec,backend,encoder}` gauge and
`encodes_started_total{codec,backend}` counter (OTel), and as `pipeline
encode` log lines per started pipeline. Notes:

- The runtime image's ffmpeg 5.1 has `h264/hevc_nvenc` and `h264/hevc_vaapi`
  but **no** `av1_nvenc`/`av1_vaapi`, so AV1 is CPU-only there; it is
  detected, not assumed (a newer ffmpeg picks AV1 hardware up automatically).
- A GPU needs more than this code: the container needs the device
  (`/dev/dri/renderD128` + `supplementalGroups`, or the NVIDIA device
  plugin) **and** a user-space driver (Mesa `va-driver` / NVIDIA runtime) in
  the image. The base image ships neither, which is why the trial encode
  exists -- on `penguin-frame` (AMD Phoenix1, `renderD128` present) VA-API
  fails with *"Failed to initialise VAAPI connection"* and the service
  correctly stays on the CPU.
- NVENC on consumer cards caps concurrent sessions; a session that fails to
  open at runtime is a pipeline failure the supervisor restarts, not a
  per-start CPU re-fallback.

## Fan-out (one ingest -> HLS + N pushes)

One ingest feeds HLS plus up to the tier cap of RTMP/SRT pushes through
`-f tee`, sharing one encode per distinct codec choice.

- **Destination cap** -- `RelayPolicy` (Free tier: 3 destinations) is enforced
  in the ffmpeg argv itself (`build_argv` -> `InvalidSpec: relay limit: max 3
  destinations on this tier`), not only in `RelaySink`. HLS and recording do
  not count. License-tier resolution is not wired, so Free is the cap for
  everyone for now.
- **`rtmps://`** is accepted for RTMP targets (same FLV muxer, TLS ingest).
- **Full HLS options in the tee** -- an HLS slave carries the complete
  `HlsOutputTarget` option set (fMP4 init/segment names, `master.m3u8`),
  generated from the same list as the standalone output so they cannot drift.
- **Failure isolation** -- pushes are `onfail=ignore`: a dead destination is
  dropped without stopping HLS or its siblings. A push-only group beside
  local outputs gets a `[f=null]-` guard slave, because a tee whose slaves
  have *all* failed aborts the whole process. HLS has no `onfail` (abort):
  a write failure restarts ffmpeg via the supervisor instead of serving a
  frozen playlist. A dropped destination stays dropped until the pipeline
  restarts (ffmpeg's tee does not retry).
- Tee slave values containing `\ ' |` or control characters (and `: [ ]`
  inside options) are refused -- a `|` in a secret URL would otherwise inject
  an extra slave.

## Ports

| Port | Protocol | Purpose |
|---|---|---|
| 8208 | HTTP | Control plane: `/health`, `/readyz`, `/api/v1/*` (community JWT + internal ServiceKey), OpenAPI, Swagger UI, `/live/*` (HLS), `/whip/*`, `/whep/*` |
| 9090 | HTTP | Prometheus `/metrics` (secondary scrape surface, separate listener) |
| 1935 | TCP | RTMP ingest (`ingest::rtmp`, started by `run_with_shutdown`) |
| 9000 | UDP | SRT ingest (`ingest::srt`, started by `run_with_shutdown`) |
| 40000-40100 | UDP | WebRTC/WHIP-WHEP ICE candidate range (bound per-session by `rtc::PeerConnectionFactory`, not at startup) |
| 50208 | gRPC | Reserved by the Helm chart; not yet implemented (no Tonic service in this scaffold) |

## Environment

| Var | Default | Notes |
|---|---|---|
| `MODULE_PORT` | `8208` | HTTP control-plane port |
| `METRICS_PORT` | `9090` | Prometheus port |
| `BIND_ADDR` | `0.0.0.0` | Listener bind address |
| `RTMP_PORT` | `1935` | |
| `SRT_PORT` | `9000` | |
| `WEBRTC_UDP_RANGE` | `40000-40100` | `"<start>-<end>"`, validated at startup |
| `STREAM_DATA_DIR` | `/var/lib/svc-streaming` | Local recordings/segments root |
| `FFMPEG_PATH` | `/usr/bin/ffmpeg` | |
| `STREAM_ENCODER` | `auto` | `auto`/`cpu`/`nvenc`/`vaapi` -- see Encoders |
| `STREAM_VAAPI_DEVICE` | auto-discovered | VA-API render node |
| `PUBLIC_BASE_URL` | `http://localhost:8208` | |
| `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER` | `localhost`/`5432`/`waddlebot`/`svc_streaming` | Per-service DB account |
| `DB_PASSWORD` | *(required, env-only)* | Never a CLI flag |
| `CACHE_HOST`/`CACHE_PORT` | `localhost`/`6379` | |
| `CACHE_PASSWORD` | *(optional, env-only)* | |
| `SERVICE_API_KEY` | *(required, env-only)* | `X-Service-Key` value for `/api/v1/internal/*` |
| `JWT_ISSUER`/`JWT_AUDIENCE` | `https://auth.penguintech.io`/`svc-streaming` | |
| `JWT_HMAC_SECRET` | *(optional, env-only)* | HS256 verification key; no key configured = 401 on every authenticated route |
| `JWT_JWKS_URL` | *(unset)* | Declared, RS256/JWKS verification not implemented yet |
| `OTEL_EXPORTER_OTLP_ENDPOINT`/`_PROTOCOL`/`_HEADERS`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | unset | Standard OTLP env config; unset endpoint = tracing-only (no OTLP export attempted) |

All secrets are env-only, never accepted as a CLI flag (`config.rs`).

## Make Targets

```
make build          # cargo build --all-targets
make test            # cargo test
make lint             # fmt-check + clippy -D warnings
make test-security  # cargo deny check
make coverage        # cargo llvm-cov --fail-under-lines 90
make docker-build    # build Dockerfile.rust, tag for localhost:32000
```

## Module Ownership

Each module directory started as one chunk's contract; `src/orchestrator.rs`
(S12) is the integration layer that wires every chunk's implementation
together into one running service -- see Runtime Wiring below.

| Module | Owns | Status |
|---|---|---|
| `src/main.rs`, `src/lib.rs` | Bootstrap: config, telemetry, orchestrator + ingest listeners, router, graceful shutdown | Implemented |
| `src/orchestrator.rs` | Ingest -> pipeline -> egress wiring: `Orchestrator`, `PipelineRegistry`, `DbIngestAuth`, `RefreshingSrtAuth` | **S12** -- Implemented |
| `src/config.rs` | Env-driven `Config`/`CliConfig`, secrets never as CLI args | Implemented |
| `src/telemetry.rs` | `tracing` + OTLP traces/metrics + Prometheus registry | Implemented (OTel *logs* export not wired -- no `opentelemetry-appender-tracing` in the approved dependency set yet) |
| `src/http/` | Router (community JWT + internal ServiceKey + HLS + WHIP/WHEP mounts), `/health`, `/readyz`, two-document OpenAPI split | Implemented |
| `src/api/` | Control-plane `/api/v1/*` routes | Implemented |
| `src/pipeline/` | `PipelineSpec`/model types, `ffmpeg` argv builder, `FfmpegSupervisor` lifecycle | Implemented |
| `src/pipeline/codec.rs` | Codec vocabulary (`h264/h265/av1`, `copy/aac/opus`, `rtmp/srt`) + container compatibility matrix | Implemented |
| `src/pipeline/encoder.rs` | GPU-preferred / CPU-fallback encoder probe (`detect_encoders`) + NVENC/VA-API argv | Implemented |
| `src/spec_builder.rs` | `streaming_configs` + `streaming_targets` -> profiles + push outputs; shared by the API validators, `/start` and the orchestrator | Implemented |
| `src/ingest/rtmp.rs` | RTMP push ingest (`rml_rtmp`) | Implemented |
| `src/ingest/srt.rs` | SRT ingest (`srt-tokio`) | Implemented |
| `src/ingest/whip.rs`, `src/egress/whep.rs` | WHIP/WHEP WebRTC ingest+egress (`webrtc`) | Implemented (transcode-bridge teardown-on-disconnect not wired, see Runtime Wiring) |
| `src/egress/hls.rs` | HLS egress (`hls_m3u8`) | Implemented |
| `src/egress/record.rs` | Recording to `object_store` | Implemented |
| `src/egress/relay.rs` | RTMP/SRT relay (stream-forwarding) | Implemented |
| `src/egress/discord_voice.rs` | Discord voice-channel bridge | **S11** -- stub, needs a Discord voice/gateway crate not yet declared |
| `src/store/` | `SecretRef` resolution (env/file) | Implemented |
| `src/db/` | `sea_orm` connection factory + entities | Implemented |

## Runtime Wiring

Ports: `8208` http (control API + `/live/*` HLS + `/whip/*` + `/whep/*`),
`9090` metrics, `1935` rtmp, `9000/udp` srt, `WEBRTC_UDP_RANGE` (default
`40000-40100`, per-session ICE/RTP, not bound at startup). Env vars: see
Environment above -- no new ones for this wiring.

```
RTMP:1935 ┐                                    ┌ HlsSink   -> /live/{cid}/...
SRT:9000  ┼-> ingest_tx -> Orchestrator -> FfmpegSupervisor ┼ RelaySink -> RtmpPush/SrtPush
WHIP POST ┘   (streaming_configs lookup   (spawn ffmpeg /   └ RecordSink -> SeaweedFS/S3 (if S3_*)
 /whip/{tok}   by source_url == key)       start_with_whip_sdp)
```

`run_with_shutdown` builds one `mpsc::channel<IngestSession>`; RTMP/SRT
listeners and the WHIP router all push onto it, `Orchestrator::run` is its
sole reader. HLS is always an output; `streaming_targets` rows become
`RtmpPush`/`SrtPush` (by `protocol`) via `spec_builder::plan_push_outputs`;
`record_enabled` adds `Record` on its own always-`Copy` profile
(never shares a name with the transcoded default -- avoids a filter_complex
ladder bug, see `RECORD_PROFILE`'s doc comment).

**Not wired (documented, not silent):** no WHIP teardown-on-disconnect;
`Record` sharing a `-f tee` group writes a `.mp4` the upload watcher never
scans (`pipeline::ffmpeg::tee_slave`); no graceful shutdown for RTMP/SRT
listeners or the dispatch loop (dropped on process exit).

## Container

`Dockerfile.rust` -- multi-stage `rust:1.97-slim-bookworm` builder ->
`debian:bookworm-slim` runtime + `ffmpeg`. Named `Dockerfile.rust` (not
`Dockerfile`) until the Python alpha (`Dockerfile`, `app.py`, ...) is
retired; the reusable `build-container.yml` workflow currently only builds
`./<module_path>/Dockerfile`, so CI wiring for this Rust build lives in
`.github/workflows/rust-svc-streaming.yml` (lint/test) --
`build-svc-streaming.yml`'s container job will need `build-container.yml`
extended with a `dockerfile` input (or this file renamed) before it builds
the Rust image in CI; that is a known follow-up, not done in this change.

Non-root `appuser` (uid 10001), binary `HEALTHCHECK` (`svc-streaming
--healthcheck`, no `curl`), `EXPOSE 8208 9090 1935 9000/udp`.
