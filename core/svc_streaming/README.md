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
| `PUBLIC_BASE_URL` | `http://localhost:8208` | Externally-reachable base URL for playback URLs / WebRTC ICE host. The Helm chart sets it from `pipeline.svcStreaming.publicBaseUrl`, or (alpha) derives `http://<node hostIP>:<httpPort>` at runtime via the downward API (`publicBaseUrlFromNodeIP`) -- never a hardcoded LAN IP |
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

## Teardown

How an ffmpeg process ends -- on `stop()` (e.g. the RTMP/SRT publisher
disconnected), on a stall, or on its own exit. One path, `teardown_attempt` in
`src/pipeline/supervisor.rs`, built on `src/pipeline/process_group.rs`; no
shell-outs, no blocking call on a tokio worker, every wait bounded.

```
publisher EOF -> Orchestrator::stop_pipeline -> FfmpegSupervisor::stop
  1. close ffmpeg stdin           EOF on `-i pipe:0` = "input ended": ffmpeg drains,
                                  writes muxer trailers (HLS ENDLIST), exits   [stop_grace_timeout]
  2. SIGTERM -> process group     only if still alive                            [term_grace_timeout]
  3. SIGKILL -> process group     only if still alive                            [reap_timeout]
  4. sweep   SIGKILL -> group     always, BEFORE reaping (PGID still reserved), so leaked helpers die
  5. reap leader (try_wait poll)  verified; failure -> error log + `teardown_failures_total`,
                                  child handed to a detached blocking reaper, and (if not
                                  stopping) pipeline `Failed` instead of respawning on top of it
  6. join stderr event thread     on the blocking pool, bounded
```

* **Signals are direct syscalls** (`nix::killpg`, `waitid(WNOWAIT)`), not a
  `kill` binary: the `debian:bookworm-slim` runtime image has none, which made
  the previous shell-out a silent no-op (the 2026-10-10 alpha hang: ffmpeg
  survived teardown, a tokio worker parked joining its stderr thread, `/health`
  and `/readyz` timed out, liveness restarted the pod ~10s after the
  disconnect). Delivery results are logged and counted, never ignored; a
  failed SIGKILL to the group falls back to killing the direct child.
* **No `q\n`.** stdin is the media pipe, so a quit command written there is
  just more media. Closing it (EOF) is the only quit that cannot collide with
  the data.
* **Safe by construction:** `killpg` refuses pid <= 1, this service's own
  group, and any child that is not its own group leader; exit is observed with
  `waitid(WNOWAIT)` so the group can be swept before the PID is released.
* **Bounded:** `SupervisorConfig::stop_deadline()` = `stop_grace` + `term_grace`
  + 3 x `reap_timeout` (default 3s + 3s + 3 x 5s). `stop()` waits at most that
  long, deregisters the pipeline regardless, and leaves a still-running
  teardown to finish detached (it is bounded too). A restart backoff is
  interrupted by `stop()` instead of being waited out.
* **ffmpeg argv** carries a global `-nostdin` first. It is the honest
  statement of how ffmpeg is driven (stdin is media or unused, never a
  console) and stops `ffmpeg-sidecar::spawn()` from appending its own stray
  trailing `-n` after the output path.

Telemetry: `teardown_duration_ms` (histogram) and `teardown_failures_total
{stage,signal}` (counter; non-zero = a teardown that did not finish cleanly).
Regression tests: `tests/pipeline_teardown.rs` (process-group sweep, EOF quit,
parked ingest writer, stall restarts, expired deadline) and
`tests/pipeline_teardown_no_kill_binary.rs` (the production failure: empty
`PATH`, single-worker runtime, publisher-disconnect `stop()` must kill and
reap the whole group and never starve the runtime).

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
