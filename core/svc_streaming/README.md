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
Rtmp{stream_key}  \                               / RtmpPush{url_secret_ref}
Srt{stream_id}     \  video: Copy|H264|H265|Av1Svt /  SrtPush{url_secret_ref}
Whip{token}        --  audio: Copy|Aac|Opus       --  Hls{Ll|Std, profile}
Pull{url}          /   resolution, fps             \  Whep{profile}
                                                     \  Record{profile, ObjectStoreRef}
                                                      \ DiscordVoice{guild_id, channel_id}
```

One `PipelineSpec` = N inputs -> N named `TranscodeProfile`s -> N outputs.
Full model: `src/pipeline/model.rs`.

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
| `src/telemetry.rs`, `src/telemetry/` | `tracing` + OTLP traces/metrics + Prometheus registry; `stream.rs` (stream latency/stage/external-call instruments), `trace_context.rs` (W3C propagation, route-template request spans) | Implemented (OTel *logs* export not wired -- no `opentelemetry-appender-tracing` in the approved dependency set yet) |
| `src/http/` | Router (community JWT + internal ServiceKey + HLS + WHIP/WHEP mounts), `/health`, `/readyz`, two-document OpenAPI split | Implemented |
| `src/api/` | Control-plane `/api/v1/*` routes | Implemented |
| `src/pipeline/` | `PipelineSpec`/model types, `ffmpeg` argv builder, `FfmpegSupervisor` lifecycle | Implemented |
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
`RtmpPush`; `record_enabled` adds `Record` on its own always-`Copy` profile
(never shares a name with the transcoded default -- avoids a filter_complex
ladder bug, see `RECORD_PROFILE`'s doc comment).

**Not wired (documented, not silent):** no WHIP teardown-on-disconnect;
`Record` sharing a `-f tee` group writes a `.mp4` the upload watcher never
scans (`pipeline::ffmpeg::tee_slave`); no graceful shutdown for RTMP/SRT
listeners or the dispatch loop (dropped on process exit).

## Observability (OTLP)

Destination is always env-configured (`OTEL_EXPORTER_OTLP_*`, see
Environment) -- unset means no export, never a hardcoded collector. Every
instrument below is emitted over OTLP; recording is infallible and a dead
collector never blocks the data plane (exporters run on their own threads
behind bounded queues that drop on overflow; shutdown flush is time-boxed).
Attributes are bounded enums only -- never a stream key, WHIP token, peer
address, or per-stream id.

| Instrument | Kind | Attributes | What it measures |
|---|---|---|---|
| `stream_ingest_handoff_seconds` | histogram | `protocol` | one ingest chunk read -> written to ffmpeg stdin (rises under ffmpeg backpressure) |
| `stream_time_to_first_egress_seconds` | histogram | `protocol`, `egress` | ingest accepted -> first HLS segment published (segment mtime vs. acceptance) |
| `stream_fanout_latency_seconds` | histogram | `kind` | RTP packet entering the SFU fanout -> a WHEP viewer receiving it (1-in-16 sampled) |
| `stream_segment_duration_seconds` / `stream_segment_size_bytes` | histogram | `variant` | each completed HLS segment (`#EXTINF`, on-disk size) |
| `stream_hls_playlist_age_seconds` | histogram | `variant` | media playlist age, sampled every poll |
| `stream_stage_duration_seconds` | histogram | `stage` | `db_connect`, `config_lookup`, `tenant_resolve`, `spec_build`, `egress_start`, `engine_start`, `ffmpeg_spawn`, `ffmpeg_first_progress`, `ffmpeg_stop`, `teardown` |
| `stream_external_call_duration_seconds` | histogram | `peer`, `outcome` | `token_ledger`, `ingest_auth`, `object_store` calls (`outcome=error` = transport failure) |
| `stream_relay_session_seconds` | histogram | `kind` | RTMP/SRT relay target lifetime (recorded on stop) |
| `stream_session_duration_seconds` | histogram | `protocol` | pumped RTMP/SRT ingest session lifetime |
| `stream_sessions_total`, `stream_session_failures_total`, `stream_ingest_bytes_total`, `stream_segments_total` | counter | `protocol`/`outcome`/`reason`/`variant` | events; failures carry a bounded `reason` (`config_not_found`, `tenant_unresolved`, `spec_build_failed`, `engine_start_failed`, `whip_sdp_missing`, `stdin_unavailable`, `no_database`) |
| `stream_active_sessions` | up-down counter | `protocol` | pumped ingest sessions live now |

Traces: one trace covers a publish end to end. The listener's connection span
(`rtmp_connection` / `srt_connection` / the WHIP request span) is carried on
`IngestSession::span`, so `orchestrator.handle_session` and its
`pipeline.<stage>` / `ingest.pump` / `egress.hls_poller` children join it
across the channel and the `tokio::spawn`. Inbound `traceparent` parents the
HTTP request span and outbound calls (token ledger, loopback ingest-auth)
inject it. HTTP spans and the `/metrics` `path` label use the matched route
template (`/whip/{token}`), never the raw URI (tokens ride in the path).

Telemetry-emission gate: `tests/e2e_rtmp_to_hls_fake.rs` (RTMP -> HLS),
`tests/rtc_whip_whep_roundtrip.rs` (WHIP -> WHEP), `tests/e2e_session_failure_telemetry.rs`,
`tests/telemetry_external_calls.rs` and `tests/http_trace_labels.rs` read the
in-memory OTel sink (`tests/otel_common`) and fail on any histogram with zero
data points.

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
