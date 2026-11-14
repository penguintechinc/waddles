/**
 * Fakes for the browser media stack (jsdom has no WebRTC / MediaStream): a fake `RTCPeerConnection` whose SDP
 * handshake, ICE gathering and connection state are scriptable, fake tracks/streams, and a fake fetch for the
 * WHIP/WHEP endpoints. Real behaviour (codec negotiation, RTP) is validated against a deployed server, see
 * `docs/rtc-town-hall-validation.md`.
 */
import { vi } from 'vitest';
import type { DeviceDeps } from '../services/media/devices';
import type { MediaDeps } from '../services/media/whipWhep';

/** A media track with the bits the app touches. */
export class FakeTrack {
  enabled = true;
  readyState: 'live' | 'ended' = 'live';
  constructor(readonly kind: 'audio' | 'video', readonly label = `${kind} device`) {}
  stop(): void {
    this.readyState = 'ended';
  }
}

/** A media stream: a track list plus the `addtrack` / `removetrack` events. */
export class FakeStream extends EventTarget {
  readonly tracks: FakeTrack[] = [];
  constructor(tracks: FakeTrack[] = []) {
    super();
    this.tracks.push(...tracks);
  }
  addTrack(track: FakeTrack): void {
    this.tracks.push(track);
    this.dispatchEvent(new Event('addtrack'));
  }
  getTracks(): FakeTrack[] {
    return [...this.tracks];
  }
  getAudioTracks(): FakeTrack[] {
    return this.tracks.filter((t) => t.kind === 'audio');
  }
  getVideoTracks(): FakeTrack[] {
    return this.tracks.filter((t) => t.kind === 'video');
  }
}

/** Sender half of a transceiver. */
export class FakeSender {
  replaceTrack = vi.fn(async (track: FakeTrack | null) => {
    this.track = track;
  });
  constructor(public track: FakeTrack | null) {}
}

/** Transceiver with scriptable codec preferences. */
export class FakeTransceiver {
  readonly sender: FakeSender;
  setCodecPreferences = vi.fn();
  constructor(readonly kind: string, track: FakeTrack | null, readonly direction: string) {
    this.sender = new FakeSender(track);
  }
}

/** How the fake gathers ICE candidates. */
export interface GatherScript {
  /** `complete` finishes gathering right away; `stall-with-candidate` / `stall-empty` never emit `complete`. */
  mode: 'complete' | 'stall-with-candidate' | 'stall-empty';
}

/** Scriptable RTCPeerConnection. Every instance is recorded in `FakePeerConnection.instances`. */
export class FakePeerConnection extends EventTarget {
  static instances: FakePeerConnection[] = [];
  static gather: GatherScript = { mode: 'complete' };

  iceGatheringState: 'new' | 'gathering' | 'complete' = 'new';
  connectionState: string = 'new';
  localDescription: { type: string; sdp: string } | null = null;
  remoteDescription: { type: string; sdp: string } | null = null;
  closed = false;
  readonly transceivers: FakeTransceiver[] = [];

  constructor(readonly config: unknown) {
    super();
    FakePeerConnection.instances.push(this);
  }

  addTransceiver(source: FakeTrack | string, init: { direction: string }): FakeTransceiver {
    const kind = typeof source === 'string' ? source : source.kind;
    const transceiver = new FakeTransceiver(kind, typeof source === 'string' ? null : source, init.direction);
    this.transceivers.push(transceiver);
    return transceiver;
  }

  removeTrack(sender: FakeSender): void {
    sender.track = null;
  }

  getSenders(): FakeSender[] {
    return this.transceivers.map((t) => t.sender);
  }

  async createOffer(): Promise<{ type: string; sdp: string }> {
    const media = this.transceivers.map((t) => `m=${t.kind} 9 UDP/TLS/RTP/SAVPF 96\r\na=${t.direction}\r\n`).join('');
    return { type: 'offer', sdp: `v=0\r\n${media}` };
  }

  async setLocalDescription(description: { type: string; sdp: string }): Promise<void> {
    const mode = FakePeerConnection.gather.mode;
    this.localDescription = { ...description, sdp: mode === 'stall-empty' ? description.sdp : `${description.sdp}a=candidate:1 1 UDP 1 192.0.2.1 5000 typ host\r\n` };
    if (mode === 'complete') {
      queueMicrotask(() => {
        this.iceGatheringState = 'complete';
        this.dispatchEvent(new Event('icegatheringstatechange'));
      });
    } else {
      this.iceGatheringState = 'gathering';
    }
  }

  async setRemoteDescription(description: { type: string; sdp: string }): Promise<void> {
    this.remoteDescription = description;
  }

  close(): void {
    this.closed = true;
    this.connectionState = 'closed';
  }

  /** Test helper: move the connection state and fire the event. */
  emitConnectionState(state: string): void {
    this.connectionState = state;
    this.dispatchEvent(new Event('connectionstatechange'));
  }

  /** Test helper: deliver a remote track. */
  emitTrack(track: FakeTrack): void {
    this.dispatchEvent(Object.assign(new Event('track'), { track }));
  }
}

/** H264 (mode 1 and 0), VP8, Opus and RTX, as a Chrome-like capability list. */
export const CHROME_CAPABILITIES = {
  video: [
    { mimeType: 'video/VP8', clockRate: 90000 },
    { mimeType: 'video/H264', clockRate: 90000, sdpFmtpLine: 'level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42e01f' },
    { mimeType: 'video/H264', clockRate: 90000, sdpFmtpLine: 'level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f' },
    { mimeType: 'video/rtx', clockRate: 90000 },
  ],
  audio: [
    { mimeType: 'audio/PCMU', clockRate: 8000 },
    { mimeType: 'audio/opus', clockRate: 48000, channels: 2 },
  ],
} as const;

/** Builds `MediaDeps` wired to the fakes; `fetchImpl` scripts the WHIP/WHEP server. */
export function fakeMediaDeps(fetchImpl: ReturnType<typeof vi.fn>, capabilities: { video: readonly object[]; audio: readonly object[] } = CHROME_CAPABILITIES): MediaDeps {
  return {
    createPeerConnection: (config) => new FakePeerConnection(config) as unknown as RTCPeerConnection,
    createStream: () => new FakeStream() as unknown as MediaStream,
    getCapabilities: (kind) => ({ codecs: [...capabilities[kind]], headerExtensions: [] }) as unknown as RTCRtpCapabilities,
    fetch: fetchImpl as unknown as typeof fetch,
    gatherTimeoutMs: 25,
  };
}

/** A 201 WHIP/WHEP answer with a `Location` for the session. */
export function answerResponse(location: string | null = '/rtc/rooms/room/sessions/sess', sdp = 'v=0\r\nanswer\r\n'): Response {
  return new Response(sdp, { status: 201, headers: location === null ? {} : { Location: location, 'Content-Type': 'application/sdp' } });
}

/** Device deps backed by fake streams; `deviceLists` feeds `enumerateDevices`. */
export function fakeDevices(options: { audio?: boolean; video?: boolean; failWith?: unknown; devices?: Array<Partial<MediaDeviceInfo>> } = {}): DeviceDeps & { getUserMedia: ReturnType<typeof vi.fn> } {
  const getUserMedia = vi.fn(async (constraints: MediaStreamConstraints) => {
    if (options.failWith !== undefined) throw options.failWith;
    const tracks: FakeTrack[] = [];
    if (constraints.audio) tracks.push(new FakeTrack('audio'));
    if (constraints.video && options.video !== false) tracks.push(new FakeTrack('video'));
    return new FakeStream(tracks) as unknown as MediaStream;
  });
  return {
    getUserMedia,
    enumerateDevices: async () => (options.devices ?? []) as MediaDeviceInfo[],
  };
}

/** Resets the shared fake state between tests. */
export function resetFakeRtc(): void {
  FakePeerConnection.instances = [];
  FakePeerConnection.gather = { mode: 'complete' };
}
