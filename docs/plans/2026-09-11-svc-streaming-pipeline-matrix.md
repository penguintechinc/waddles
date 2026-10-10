# svc-streaming — Pipeline Transform Matrix (Rust ffmpeg orchestration)

Status: **DRAFT — spec for S3 implementation, not itself an implementation.** Extends
`docs/plans/2026-08-31-svc-streaming-design.md` §8.1's "build" fork: svc-streaming (Rust/tokio)
orchestrates the Debian-packaged system `ffmpeg` (libx264/libx265/libsvtav1/libaom/aac/libopus)
via `ffmpeg-sidecar`, plus Rust-native listeners (`rml_rtmp`, `srt-tokio`, `webrtc-rs`). Supersedes
the Python alpha's `core/svc_streaming/services/ffmpeg_engine.py` argv builder (pull-only,
`-c copy`/`libx264` fixed pair, per-output re-encode) — its `-f flv` fan-out + `-f mp4` record
pattern and `FFmpegSupervisor` SIGTERM→SIGKILL lifecycle are reused where correct (§4, §6).

**Product-owner scope (2026-09-11, overrides the MVP tags below where they say P2):** "transform
to/from x264, x265, rtmp, av1, srt, etc. to one or many inputs / outputs — along with
WHIP/WHEP/RTC too" — **WHIP ingest and WHEP egress are MVP (chunk S6)**; AV1 encode is MVP where the
container supports it (HLS/CMAF, record); multi-input compositing (golden case #10) stays P2.
Defaults taken for the open questions in §9 unless overridden: 9.1 PCM→songbird encoder; 9.2 degraded
native LL-HLS is acceptable for MVP; 9.3 RTMP push is H264-only until Enhanced RTMP matures;
9.4 **SFU model** (one encode, RTP forwarded to N PeerConnections); 9.5 AV1-over-WebRTC deferred.

## 1. Pipeline model

`PipelineSpec { id, inputs: Vec<InputSpec>, profiles: Vec<TranscodeProfile>, outputs: Vec<OutputSpec> }`

| Type | Variants / fields |
|---|---|
| `InputSpec` | `Rtmp{listen_port,stream_key}` · `Srt{listen_port,streamid,latency_ms}` · `Whip{endpoint_token}` · `Pull{url,reconnect}` |
| `TranscodeProfile` | `name`, `video: VideoCodec(Copy\|H264{bitrate_kbps,preset}\|H265{bitrate_kbps,preset}\|Av1Svt{bitrate_kbps,preset})`, `audio: AudioCodec(Copy\|Aac{bitrate_kbps}\|Opus{bitrate_kbps})`, `resolution: Option<(u32,u32)>`, `fps: Option<u32>` |
| `OutputSpec` | `RtmpPush{url,profile}` · `SrtPush{url,streamid,latency_ms,profile}` · `Hls{mode:Ll\|Std,path,profile}` · `Whep{session_id,profile}` · `Record{path,segment_seconds,profile}` · `DiscordVoice{channel_id,profile}` |

**Process model.** One `ffmpeg` process per `PipelineSpec`. Outputs sharing one profile fan out
from a single encode via `-f tee` (`[f=…:onfail=ignore]` per slave). Outputs on different
profiles use `-filter_complex split` — **only branches that need decode+re-encode go through
`split`; a `Copy` branch always maps the source stream directly** (`-map 0:v`), never through the
filter graph (copy = no decode at all; mixing a direct-map copy branch with filtered branches in
one command is valid ffmpeg). `Whip`/`Whep` legs never appear inside `-f tee`'s slave-URL list
(tee only muxes byte-stream URLs); they are separate `-f rtp` output blocks or
`-protocol_whitelist … -i input.sdp` input blocks handled by `webrtc-rs` outside ffmpeg's process
boundary — a pure-copy WHIP→WHEP leg needs **no ffmpeg process** (RTP forwarded SFU-style).

## 2. Input recipes

| Input | Wire → ffmpeg | argv fragment | Note |
|---|---|---|---|
| RTMP | `rml_rtmp` (tokio TCP :1935) demuxes handshake/chunks → FLV tag bytes on a pipe | `-f flv -i pipe:0` | listener owns the port; ffmpeg never binds it |
| SRT | `srt-tokio` (`SrtSocket::builder().listen(9000)`) → MPEG-TS payload bytes | `-f mpegts -i pipe:0` | SRT contribution convention is TS-in-SRT |
| WHIP | `webrtc-rs` PeerConnection (Axum `POST /whip` signaling) negotiates ICE/DTLS, depacketizes RTP to local UDP | `-protocol_whitelist file,rtp,udp -i input.sdp` | SDP file + ports written by the WHIP handler before spawn |
| Pull | direct URL fetch | `-i <url> -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5` | `-reconnect*` are HTTP-only AVOptions; `rtmp://` pull sources rely on supervisor restart-on-exit instead |
| Multi-input (N-in) | — | **MVP = separate pipelines, one per source.** `-filter_complex overlay`/`amix` compositing (picture-in-picture, audio mix) is **P2** — golden case #10 (§7) |

## 3. Codec profile recipes

| Codec | argv fragment |
|---|---|
| Copy | `-c copy` (or `-c:v copy -c:a copy` per branch) |
| x264 | `-c:v libx264 -preset veryfast -tune zerolatency -profile:v high -g <2*fps> -sc_threshold 0 -b:v <kbps>k -maxrate <kbps>k -bufsize <2*kbps>k` |
| x265 | `-c:v libx265 -preset fast -x265-params keyint=<2*fps>:min-keyint=<2*fps>:scenecut=0 -b:v <kbps>k -maxrate <kbps>k -bufsize <2*kbps>k` |
| AV1 (SVT) | `-c:v libsvtav1 -preset 8..12 -svtav1-params tune=0:fast-decode=1 -g <2*fps> -b:v <kbps>k` — preset 8-12 for live (lower=slower/better); several-x realtime cost even at fast presets vs x264 veryfast — **the CPU risk** |
| AAC | `-c:a aac -b:a <kbps>k -ar 48000` |
| Opus | `-c:a libopus -b:a <kbps>k -ar 48000 -ac 2` |
| Ladder (source/720p30/480p30) | one `split=2[v720][v480]` feeding two `scale` chains; copy rung maps `0:v` directly (§1, §7 case 7) |

Hardware accel (VAAPI/NVENC) is **out of scope** for this spec — CPU-only encode paths only.
AV1 carriage: **HLS/CMAF (fMP4) — yes**, spec-supported. **RTMP/FLV — only via Enhanced RTMP**
(Veovera E-RTMP, ffmpeg flv-muxer + destination-platform support both emerging as of the pinned
ffmpeg version — verify before enabling, treat non-MVP). **WebRTC via `webrtc-rs` — AV1 RTP
payloader maturity unconfirmed** in the crate as of this writing (browsers support AV1-over-WebRTC
already; the gap is server-side Rust) — deferred (open question §9.5).

## 4. Output recipes

| Output | argv fragment | Note |
|---|---|---|
| RTMP push | `-f flv rtmp://host/app/key` | H264+AAC only until Enhanced RTMP lands |
| SRT push | `-f mpegts "srt://host:port?streamid=…&latency=200"` | |
| HLS std | `-f hls -hls_time 4 -hls_list_size 6 -hls_flags delete_segments+independent_segments -hls_segment_type fmp4` | |
| LL-HLS | `-f hls -hls_playlist_type event -hls_time 4 -hls_part_time 0.5 -hls_flags independent_segments+program_date_time -hls_segment_type fmp4 -lhls 1` | ffmpeg's native LL-HLS is partial (no blocking playlist reload / preload hints per current spec) — degraded LL-HLS accepted for MVP (§9.2) |
| WHEP | ffmpeg `-f rtp` → local UDP → `webrtc-rs`; **SFU model**: one encode, RTP forwarded to N PeerConnections (§9.4) | MVP |
| Record | `-f segment -segment_time 60 -reset_timestamps 1 -strftime 1 "%Y%m%d%H%M%S.ts"` (or `-f mp4 -movflags +frag_keyframe+empty_moov+default_base_moof` for a single fMP4 file), closed segments uploaded async to MinIO | |
| Discord voice | `-vn -c:a pcm_s16le -ar 48000 -ac 2 -f s16le pipe:1` → songbird's own Opus encoder (default, §9.1) | audio-only sink |

## 5. Transform matrix

Legend: **C** = copy-possible when source codec already matches target, **T** = transcode
required. MVP tag per the product-owner scope above (WHIP/WHEP = MVP).

| Input \ Output | RTMP push | SRT push | HLS std | LL-HLS | WHEP | Record | Discord voice |
|---|---|---|---|---|---|---|---|
| RTMP (H264+AAC) | C·MVP | C·MVP | C·MVP | C·MVP(degraded) | T (H264 profile, AAC→Opus)·MVP | C·MVP | T (AAC→Opus)·P2 |
| SRT (H264+AAC) | C·MVP | C·MVP | C·MVP | C·MVP(degraded) | T·MVP | C·MVP | T·P2 |
| WHIP (H264/VP8+Opus) | T (FLV remux, Opus→AAC)·MVP | T·P2 | T·MVP | T·P2 | C·MVP (no ffmpeg) | C·MVP | C (if 48k stereo)·P2 |
| Pull (varies) | C if H264+AAC else T·MVP | C if matched else T·P2 | C if matched else T·MVP | T (repackage)·P2 | T·MVP | C·MVP | T·P2 |

## 6. Supervision & observability

| Lifecycle stage | Mechanism |
|---|---|
| Spawn | `ffmpeg-sidecar::FfmpegCommand` against the **system** binary (PATH), not the crate's auto-download |
| Progress | `.iter()` → `FfmpegEvent::Progress{frame,fps,bitrate,speed,…}`, parsed from ffmpeg's default stderr stats line; `-progress pipe:2 -nostats` is a more robust machine-parseable alternative — implementation choice |
| Restart/backoff | exponential (1s→2s→4s…cap 30s) on unexpected exit; cap retries, then mark pipeline `failed` |
| Stall detection | no `frame=` advance for N seconds (e.g. 10s) → force-restart |
| Graceful stop | **close stdin (EOF on `pipe:0`)** → grace → `SIGTERM` to the process group → grace → `SIGKILL` to the group → sweep + bounded reap. (Originally `q\n` on stdin; superseded 2026-10-10 — stdin *is* the media pipe, so `q` was just media bytes ffmpeg could never read as a command. See `core/svc_streaming/README.md` Teardown.) |

| OTel signal | Name | Note |
|---|---|---|
| Histogram | `pipeline_start_ms` | spawn → first progress event |
| Histogram | `encode_speed` | parsed `speed=` (e.g. `1.02x`) |
| Histogram | `output_bitrate_kbps` | ffmpeg's default stats line is **aggregate across all mapped outputs**; tee muxer doesn't report per-slave bitrate — accuracy caveat |
| Gauge | `active_pipelines` | |
| Counter | `restarts_total{pipeline_id,reason}` | |
| Histogram | `teardown_duration_ms` | one attempt's sweep + reap + event-thread join |
| Counter | `teardown_failures_total{stage,signal}` | stage ∈ `signal`/`reap`/`join`/`stop_deadline`; any non-zero value is a teardown that did not complete cleanly |
| Counter | `output_failures_total{pipeline_id,output_kind,reason}` | |
| Span | one per pipeline lifecycle, child span per restart attempt | |

## 7. Golden test cases (spec → expected argv)

| # | Spec | Expected argv (key fragments) |
|---|---|---|
| 1 | RTMP in → RTMP push, Copy | `-f flv -i pipe:0 -map 0:v -map 0:a -c copy -f flv rtmp://push/key1` |
| 2 | Pull(HLS) → HLS std, H264 720p30 | `-i src.m3u8 -reconnect 1 … -c:v libx264 -preset veryfast … -vf scale=1280:720 -r 30 -b:v 2500k … -c:a aac -b:a 128k -f hls -hls_time 4 -hls_segment_type fmp4 …` |
| 3 | SRT in → SRT push, x265 | `-f mpegts -i pipe:0 -c:v libx265 -preset fast -x265-params keyint=60:min-keyint=60:scenecut=0 -b:v 4000k … -c:a aac -f mpegts "srt://push:9000?streamid=…"` |
| 4 | RTMP in → (RTMP push + Record), same Copy profile | `-f flv -i pipe:0 -c copy -f tee "[f=flv:onfail=ignore]rtmp://push/key1\|[f=mp4:onfail=ignore]/records/x.mp4"` |
| 5 | Pull → HLS, AV1 SVT | `-i src.m3u8 -c:v libsvtav1 -preset 10 -svtav1-params tune=0:fast-decode=1 -c:a opus -f hls -hls_segment_type fmp4 …` |
| 6 | WHIP in → WHEP out, Copy | **no ffmpeg process** — `webrtc-rs` forwards RTP directly (SFU) |
| 7 | RTMP in → **3 outputs mixed**: Copy→RTMP + 720p H264→HLS + 480p H264→HLS | `-f flv -i pipe:0 -filter_complex "[0:v]split=2[v720][v480];[v720]scale=1280:720[v720s];[v480]scale=854:480[v480s]" -map 0:v -map 0:a -c:v copy -c:a copy -f flv rtmp://push/key1 -map "[v720s]" -map 0:a -c:v libx264 -b:v 2500k -c:a aac -f hls …/720/… -map "[v480s]" -map 0:a -c:v libx264 -b:v 1000k -c:a aac -f hls …/480/…` |
| 8 | RTMP in → Discord voice | `-f flv -i pipe:0 -map 0:a -vn -c:a pcm_s16le -ar 48000 -ac 2 -f s16le pipe:1` |
| 9 | Pull → Record only | `-i src.m3u8 -c copy -f segment -segment_time 60 -reset_timestamps 1 -strftime 1 /records/%Y%m%d%H%M%S.ts` |
| 10 | **2× RTMP in → 1 HLS out**, overlay composite | `-f flv -i pipe:0 -f flv -i pipe:1 -filter_complex "[0:v][1:v]overlay=W-w-20:H-h-20[vout]" -map "[vout]" -map 0:a -c:v libx264 -b:v 3000k -c:a aac -f hls …` — **P2**, MVP treats each input as its own pipeline (§2) |

## 8. Ports / infra

| Item | Value | Note |
|---|---|---|
| RTMP listener | 1935/tcp (`rml_rtmp`) | unprivileged; LB with TCP passthrough; alpha = NodePort |
| SRT listener | 9000/udp (`srt-tokio`) | needs UDP-capable Gateway/LB; alpha = NodePort/udp |
| WHIP/WHEP media | UDP range 40000–40100 + ICE | host candidates behind Gateway LB; TURN out of scope |
| Control REST / gRPC | **8208 / 50208** (chart `pipeline.svcStreaming`) + `/metrics` 9090 | |
| HLS segments | tmpfs (`emptyDir: medium: Memory`) default; PVC only if segment survival across restart is required | LL-HLS I/O is latency-sensitive, disposable |
| Record uploads | MinIO, async multipart post-segment-close | at-rest encryption remains design doc §8.5 open item |
| Resource tier (ballpark, per concurrent pipeline) | copy/remux ~0.1 core; x264 720p30 veryfast ~1 core; x265 720p30 ~1.5–2 cores; AV1 SVT preset 10 720p30 ~2–4 cores | unbenchmarked estimates for node-pool sizing only |

## 9. Open questions (defaults taken — see header)

1. **Discord voice sink format** — raw PCM to songbird's own Opus encoder vs raw Opus-packet extraction from ffmpeg — confirm against songbird's actual API before committing.
2. **LL-HLS fidelity** — ffmpeg's native `-lhls 1` muxer lacks blocking playlist reload/preload hints; is degraded LL-HLS acceptable for MVP or is a dedicated packager required?
3. **Enhanced RTMP (HEVC/AV1 over FLV)** — is Twitch/YouTube AV1/HEVC ingest a launch requirement, or is RTMP output H264-only until E-RTMP matures?
4. **WHEP fan-out architecture** — one `-f rtp` output per viewer vs a real SFU (one encode, N PeerConnections via RTP forward) — SFU chosen.
5. **AV1-over-WebRTC** — `webrtc-rs` AV1 RTP payloader maturity is unconfirmed; deferred with HEVC-over-WebRTC.
