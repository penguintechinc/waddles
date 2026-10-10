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

(Alembic `0059_streaming_codec_columns`; API: `POST/PUT .../configs`,
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
  plugin) **and** a user-space driver in the image. The `cpu` image ships
  neither; the `amd` image adds the VA-API driver, the `nvidia` image asks
  the NVIDIA runtime for the host-matched NVENC library (see Container). The
  trial encode is what keeps a mismatch safe -- on `penguin-frame` (AMD
  Phoenix1, `renderD128` present) VA-API fails with *"Failed to initialise
  VAAPI connection"* on the `cpu` image and the service correctly stays on
  the CPU.
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
`debian:bookworm-slim` `runtime-base` (ffmpeg 5.1.9) -> one of three
variant targets. Named `Dockerfile.rust` (not `Dockerfile`) until the Python
alpha (`Dockerfile`, `app.py`, ...) is retired.

```
builder ──► runtime-base ──┬──► cpu     svc-streaming:<tag>-cpu
 (rust)     (ffmpeg,      ├──► amd     svc-streaming:<tag>-amd
            appuser)      └──► nvidia  svc-streaming:<tag>-nvidia
```

| Target | Tag | Adds to `runtime-base` | GPU encoders compiled + usable with | AV1 |
|---|---|---|---|---|
| `cpu` | `-cpu` | nothing | none | CPU (`libsvtav1`) |
| `amd` | `-amd` | `libva2`, `libva-drm2`, `mesa-va-drivers` (radeonsi) | `h264_vaapi`, `hevc_vaapi` with `/dev/dri/renderD*` | CPU |
| `nvidia` | `-nvidia` | `NVIDIA_DRIVER_CAPABILITIES=compute,utility,video` | `h264_nvenc`, `hevc_nvenc` with the NVIDIA runtime | CPU |

- **NVIDIA driver libraries are not baked in.** `libnvidia-encode` and
  `libcuda` must match the host kernel module; nvidia-container-toolkit
  injects them when the pod runs with the NVIDIA runtime and the `video`
  capability.
- Build one variant: `make -C core/svc_streaming docker-build VARIANT=amd`.
  Without `--target` Docker builds the last stage, which is `cpu`.
- Smoke-test a built variant: `make -C core/svc_streaming image-smoke
  VARIANT=amd IMAGE=<ref>` -- asserts the image label, non-root uid, the
  encoders the variant claims, the driver wiring, and a libx264 trial encode.
  It prints `PASSED n/m checks`; zero checks is a failure.
- CI: `.github/workflows/build-svc-streaming.yml` builds all three from the
  same context, pushes `ghcr.io/<org>/<repo>/svc-streaming:<version><suffix>-<variant>`
  (`latest-<variant>` on a release tag), smoke-tests the pushed digest, and
  gates on HIGH/CRITICAL Trivy findings. amd64 only (same toggle as the other
  `build-*.yml` workflows).
- Non-root `appuser` (uid 10001), binary `HEALTHCHECK` (`svc-streaming
  --healthcheck`, no `curl`), `EXPOSE 8208 9090 1935 9000/udp`.

## Choosing a variant

Set `streaming.gpuVariant` (`values.yaml`, default `cpu`) per deploy or per
node pool. The chart appends the variant to the tag and wires the pod.

| Node hardware | `gpuVariant` | Needed on the node | Chart wiring |
|---|---|---|---|
| No GPU, or unknown | `cpu` | nothing | none |
| AMD GPU with VA-API (`/dev/dri/renderD*`) | `amd` | AMD k8s-device-plugin (`amd.com/gpu`); render GID: `getent group render` | `amd.com/gpu: 1` limit + `supplementalGroups` |
| NVIDIA GPU | `nvidia` | driver, nvidia-container-toolkit, `RuntimeClass nvidia`, device plugin | `runtimeClassName: nvidia`, `nvidia.com/gpu: 1`, GPU toleration |

Set `streaming.amd.supplementalGroups` to the host's render GID(s) when
`gpuVariant=amd`; the render fails when the list is empty. Confirm the
choice on a running pod: `encoder selected` log lines (and the
`encoder_backend{codec,backend,encoder}` gauge) show `backend=software`,
`vaapi` or `nvenc` per codec family.

What a mismatch does:

| Mismatch | Result | Fix |
|---|---|---|
| `cpu` image on a GPU node | CPU encoders | intended -- switch `gpuVariant` to use the GPU |
| `amd` image, node without `amd.com/gpu` | pod Pending (`Insufficient amd.com/gpu`) | run it on a GPU pool, or `streaming.amd.deviceResource=""` + `devDri=false` for CPU-only |
| `amd` image, `/dev/dri` present but wrong GID | trial encode fails (permission), CPU encoders, warn log | set `supplementalGroups` |
| `nvidia` image on a CPU node, default values | pod Pending (`nvidia.com/gpu`) | `streaming.nvidia.runtimeClassName=""` and `gpuLimit=0` -- the pod starts and runs CPU-only |
| `nvidia` image, GPU present, no NVIDIA runtime | no device nodes, CPU encoders | set the runtimeClass |

The in-container trial encode degrades to CPU in every case where the pod
starts; the scheduler gates (`/dev/dri` hostPath, `nvidia.com/gpu`,
`runtimeClassName`) are what can stop a pod before it starts.

Why the amd default is a device-plugin resource, not a hostPath: a bind mount
of `/dev/dri` alone is denied by the container's device cgroup -- opening
`renderD128` fails with `EPERM` for a non-privileged pod (verified: `docker run
-v /dev/dri:/dev/dri` fails, `--device /dev/dri/renderD128` works). The device
plugin declares the devices, so the runtime allows them. The hostPath is also
rejected by PodSecurity `restricted`; it stays as an explicit fallback only
(`streaming.amd.deviceResource=""`).

Verify a GPU node by hand (CI has no GPU):

```bash
docker run --rm --read-only --tmpfs /tmp --device /dev/dri/renderD128 \
  --group-add <render-gid> --user 10001:10001 --entrypoint /usr/bin/ffmpeg \
  <repo>/svc-streaming:<tag>-amd -hide_banner -loglevel error \
  -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va \
  -f lavfi -i testsrc=size=640x360:rate=1 -vf format=nv12,hwupload \
  -frames:v 10 -c:v h264_vaapi -f null -
```

## Follow-up: AV1-GPU and enhanced-RTMP (ffmpeg 6.1)

Not in any variant. Debian bookworm's ffmpeg 5.1.9 has no `av1_nvenc` or
`av1_vaapi`, and it cannot carry enhanced-RTMP (HEVC/AV1 in FLV). Both need
the separate ffmpeg 6.1 build, tracked as its own PR. Until it lands, AV1 is
CPU-only on every variant, and the encoder selection detects that rather than
assuming it.

Intel iGPUs have no variant: Debian's `mesa-va-drivers` carries no Intel (iHD)
VA driver. Covering them needs `intel-media-va-driver-non-free`, a non-free
package -- a decision for the owner, not made here.
