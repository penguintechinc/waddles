# Town Hall: real audio/video validation plan (alpha, manual)

Unit tests run the WHIP/WHEP client against a fake `RTCPeerConnection`, so they prove signalling, state handling and cleanup but
**not** that audio and video really flow. That needs two real browsers against a deployed svc-streaming. This plan is for a
human (or a `test-runner` dispatch with cluster access) to run once the server is deployed to dal2alpha. It has NOT been run.

## Preconditions

| Need | How to check |
|---|---|
| hub-api and svc-streaming deployed with the rooms gRPC wiring (T4/T5) | `GET /api/v1/communities/<id>/town-hall` returns 200 with a `room_id` |
| `waddles.streaming.rtc` flag ON for the tenant | with it OFF the same call returns 402 `FEATURE_NOT_ENABLED` (also a valid thing to test, see step 9) |
| Media URL reachable by browsers: `whip_url` / `whep_url` in the join response resolve from the tester's network, UDP range open (alpha uses NodePort) | `curl -i -X OPTIONS <whip_url>` answers; see "Server gaps to confirm" for CORS |
| Two accounts that are members of the same community; a third that is a community admin | the admin sees moderator controls |
| Chromium with fake devices for automation: `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream` | not needed for a human run with real mics |

Secrets: credentials come from environment variables only (`SCREENSHOT_EMAIL` / `SCREENSHOT_PASSWORD` style); never put tokens
in command lines or files. Never run any of this against beta or prod.

## Manual steps (two browsers, real devices)

1. Browser A (member 1): sign in, open **Town Hall** from the community sidebar (`/community/<id>/town-hall`).
   Expect: lobby with "Share my camera", "Join with microphone", "Listen only"; status badge "Not connected".
2. A: tick "Share my camera", click **Join with microphone**, allow the permission prompt.
   Expect: badge goes `Waiting for microphone permission` to `Joining the room` to `Connecting audio and video` to `Connected`; a "You (you)" tile with live video.
3. Browser B (member 2, different profile or machine): open the same page, join with the microphone only.
   Expect on A: a second tile ("Participant 1") appears within one poll (about 5 s) and plays B's audio; head count "2 people, 2 speaking".
   Expect on B: a tile with A's live video and A's audio.
4. Speak on each side: audio is heard on the other side with no echo (headphones recommended). Record one-way latency by ear (target under about 1 s on a LAN).
5. A: **Mute microphone**. B no longer hears A; A's tile shows "Muted". **Unmute**: audio returns. A: **Turn camera off**: B's tile loses video (black or avatar); on again: video returns.
6. A: switch microphone and camera from the pickers: the new device is used without dropping the call.
7. B: **Raise hand**. Browser C (community admin) opens the Town Hall and joins: the hands queue lists B by name (position 1). C clicks **Acknowledge**: B's page shows "A moderator acknowledged your hand." and announces it (screen reader live region). B: **Lower hand**: the queue empties.
8. C: **Mute** A (moderator mute). A's audio stops for others; C: **Unmute**. C: **Remove** B: B's call ends with a visible error state (Connection lost / Reconnect), B can rejoin.
9. Failure states: (a) deny the microphone prompt: "Microphone access is blocked" with Try again and Join listen-only, listen-only connects and receives audio; (b) turn the flag OFF: the page shows "Calls are not enabled for this community" with no retry; (c) stop svc-streaming (or block its UDP): join shows the retryable media/unavailable state; restore and Try again connects.
10. Leave on both sides (**Leave room**) and close tabs. Expect on the server: sessions removed (no leaked `sessions_active{room_whip|room_whep}` after a minute), peers gone from the participants list.
11. Cross-check no secrets: DevTools network tab on the `/whip` and `/whep` POSTs shows the room token only in the `Authorization` header, never in a URL; console shows no token; page text shows no UUIDs.

Pass criteria: steps 3, 4, 5, 7, 8 audibly/visibly correct, step 9 states as described, step 10 clean teardown, step 11 clean.

## Automated two-browser smoke (Playwright, optional)

`tests/e2e/town-hall-two-browsers.manual.cjs` launches two Chromium contexts with fake media devices, joins both, and asserts the
remote tile of each side receives RTP (`inbound-rtp` bytes greater than 0) and that Leave tears the sessions down. It is not part of the
Playwright suite (name does not match `*.spec.js`) and has not been run.

```bash
cd tests/e2e
# credentials only via environment, never on the command line
export BASE_URL=https://<alpha webui host>      # alpha via the internal LB with a Host header, or a CF Access service token from env
export COMMUNITY_ID=<seeded community id>
export MEMBER_A_EMAIL=... MEMBER_A_PASSWORD=... MEMBER_B_EMAIL=... MEMBER_B_PASSWORD=...
export PLAYWRIGHT_OUTPUT_DIR=/tmp/playwright-waddles   # artifacts live here; delete after the run
node town-hall-two-browsers.manual.cjs
```

## Screenshots (marketing rule, `docs/screenshots/`)

`community-town-hall` is now in `tests/screenshots/pages.cjs`, so `make screenshots` captures the lobby. The connected, hands, failure
and moderator states need a scripted session; capture them with the same two-browser script by setting `SHOT_DIR`:

| Name | State | How |
|---|---|---|
| `community-town-hall` | lobby (member) | `make screenshots` against a seeded stack with the flag ON |
| `town-hall-connected` | two participants, tiles, controls | script, after both joined |
| `town-hall-hand-raised` | member's raised hand + moderator queue | script, after step 7 |
| `town-hall-moderator` | moderator view with hands queue and participant controls | script, admin context |
| `town-hall-feature-off` | "Calls are not enabled for this community" | flag OFF run |
| `town-hall-unavailable` | retryable 503 state | stop svc-streaming run |

Review every image for error text and PII (use seeded display names only) before it lands.

## Server gaps to confirm before the run (found while reading `core/svc_streaming/src/rooms/media.rs`)

1. **CORS on `/rtc/rooms/*`.** There is no CORS layer on the room media routes (only HLS has one). A browser on the webui origin POSTing
   `Authorization` + `Content-Type: application/sdp` to a different origin needs an `OPTIONS` preflight to pass and
   `Access-Control-Expose-Headers: Location` so the client can read the session URL for `DELETE`. Either serve the media routes on the webui origin
   (ingress path routing) or add the CORS layer. Without `Location` the client cannot DELETE on leave (the server still reaps the dead peer).
2. **No ICE restart / trickle.** The server is non-trickle and has no PATCH, so a dropped connection cannot be repaired in place; the client
   shows "Connection lost" and re-joins with a fresh publish. Acceptable for v3.0.x; note it for the TURN work in v3.3.x.
3. **Codec fan-out is copy-only.** Viewer tracks are fixed H264 + Opus and RTP is forwarded as is, so publishers must send H264 and Opus. The client
   restricts its offer to those; a browser without H264 shares audio only. Verify H264 profile interoperability across Chrome, Firefox and Safari in step 3.
4. **No participants route for members.** hub-api exposes names only on the admin route, so members see head counts and numbered tiles. Add a
   member `GET .../participants` (display names, same DTO as the admin one) to give members a roster.
5. **Session lifetime.** Sessions end at the join token's expiry (`expires_at`); the client does not refresh the token mid-call. A long meeting
   needs either a longer join TTL or a client re-join before expiry (follow-up).
6. **Subscribe-per-publisher scales linearly.** One WHEP peer connection per remote publisher; fine for a town hall of a few speakers, plan a single
   multi-source subscription (or simulcast) before large rooms.
