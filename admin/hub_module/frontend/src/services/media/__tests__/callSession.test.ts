/**
 * CallSession state machine: join/leave phases, device and join failures, listen-only fallbacks, remote subscription
 * reconciliation, connection loss and reconnect, device switching, and cleanup on leave/dispose. Real fakes for the
 * peer connection, devices and the WHIP/WHEP server; nothing about the session itself is mocked.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CallsApiError, type JoinedRoom } from '../../callsApi';
import { CallSession, type CallPhase, type JoinOptions } from '../callSession';
import { FakePeerConnection, FakeTrack, answerResponse, fakeDevices, fakeMediaDeps, resetFakeRtc } from '../../../test/fakeRtc';

const TOKEN = 'room-token-SECRET-value';
const SELF = '11111111-1111-4111-8111-111111111111';
const ADA = '22222222-2222-4222-8222-222222222222';
const GRACE = '33333333-3333-4333-8333-333333333333';

const joined = (over: Partial<JoinedRoom> = {}): JoinedRoom => ({
  token: TOKEN,
  iceServers: [],
  url: 'https://media.example/rtc/rooms/r',
  whipUrl: 'https://media.example/rtc/rooms/r/whip',
  whepUrl: 'https://media.example/rtc/rooms/r/whep',
  tokenId: 'k',
  participantId: SELF,
  expiresAt: 4_000_000_000,
  ...over,
});

/** Scriptable media server: `rules` can fail specific requests. */
interface Rules {
  whip?: number;
  whep?: (source: string) => number;
}

function mediaServer(rules: Rules = {}) {
  return vi.fn(async (url: string, init: RequestInit) => {
    if (init.method === 'DELETE') return new Response(null, { status: 204 });
    if (url.includes('/whip')) return rules.whip && rules.whip !== 201 ? new Response('', { status: rules.whip }) : answerResponse('/rtc/rooms/r/sessions/pub');
    const source = new URL(url).searchParams.get('source') ?? '';
    const status = rules.whep?.(source) ?? 201;
    return status === 201 ? answerResponse(`/rtc/rooms/r/sessions/sub-${source}`) : new Response('', { status });
  });
}

function setup(options: { rules?: Rules; devices?: ReturnType<typeof fakeDevices>; join?: () => Promise<JoinedRoom>; leave?: () => Promise<void> } = {}) {
  const fetchImpl = mediaServer(options.rules);
  const devices = options.devices ?? fakeDevices();
  const join = vi.fn(options.join ?? (async () => joined()));
  const leave = vi.fn(options.leave ?? (async () => undefined));
  const session = new CallSession({ join, leave, devices, media: fakeMediaDeps(fetchImpl as unknown as ReturnType<typeof vi.fn>) });
  const phases: CallPhase[] = [];
  session.subscribe(() => {
    const phase = session.getSnapshot().phase;
    if (phases[phases.length - 1] !== phase) phases.push(phase);
  });
  return { session, fetchImpl, devices, join, leave, phases };
}

const MIC: JoinOptions = { camera: false, listenOnly: false };
const CAM: JoinOptions = { camera: true, listenOnly: false };
const LISTEN: JoinOptions = { camera: false, listenOnly: true };

function methods(fetchImpl: ReturnType<typeof mediaServer>, method: string): string[] {
  return fetchImpl.mock.calls.filter(([, init]) => init.method === method).map(([url]) => String(url));
}

beforeEach(() => resetFakeRtc());

describe('join', () => {
  it('walks requesting-media, joining, connecting, connected and publishes audio', async () => {
    const { session, phases, join } = setup();
    await session.join(MIC);
    const snap = session.getSnapshot();
    expect(phases).toEqual(['requesting-media', 'joining', 'connecting', 'connected']);
    expect(join).toHaveBeenCalledTimes(1);
    expect(snap).toMatchObject({ phase: 'connected', selfParticipantId: SELF, publishing: { audio: true, video: false }, listenOnly: false, failure: null, micMuted: false });
    expect(snap.localStream).not.toBeNull();
    expect(JSON.stringify(snap)).not.toContain(TOKEN);
  });

  it('publishes video when the camera was requested and lets the user toggle camera and microphone', async () => {
    const { session } = setup();
    await session.join(CAM);
    expect(session.getSnapshot()).toMatchObject({ publishing: { audio: true, video: true }, cameraOn: true });
    const stream = session.getSnapshot().localStream;
    session.setCameraEnabled(false);
    expect(session.getSnapshot().cameraOn).toBe(false);
    expect(stream?.getVideoTracks()[0]?.enabled).toBe(false);
    session.setMicMuted(true);
    expect(session.getSnapshot().micMuted).toBe(true);
    expect(stream?.getAudioTracks()[0]?.enabled).toBe(false);
    session.setMicMuted(false);
    expect(stream?.getAudioTracks()[0]?.enabled).toBe(true);
  });

  it('ignores the camera toggle when no video is being sent', async () => {
    const { session } = setup();
    await session.join(MIC);
    session.setCameraEnabled(true);
    expect(session.getSnapshot().cameraOn).toBe(false);
  });

  it('listen-only skips device access and publishing but registers and connects', async () => {
    const { session, devices, fetchImpl, join } = setup();
    await session.join(LISTEN);
    expect(devices.getUserMedia).not.toHaveBeenCalled();
    expect(methods(fetchImpl, 'POST')).toEqual([]);
    expect(join).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot()).toMatchObject({ phase: 'connected', listenOnly: true, publishing: null, localStream: null });
  });

  it('is a no-op when already connected', async () => {
    const { session, join } = setup();
    await session.join(MIC);
    await session.join(MIC);
    expect(join).toHaveBeenCalledTimes(1);
  });

  it('shares audio only, with a notice, when the browser cannot send video', async () => {
    const fetchImpl = mediaServer();
    const noH264 = { video: [{ mimeType: 'video/VP8', clockRate: 90000 }], audio: [{ mimeType: 'audio/opus', clockRate: 48000, channels: 2 }] };
    const session = new CallSession({ join: async () => joined(), leave: async () => undefined, devices: fakeDevices(), media: fakeMediaDeps(fetchImpl as unknown as ReturnType<typeof vi.fn>, noH264) });
    await session.join(CAM);
    expect(session.getSnapshot()).toMatchObject({ publishing: { audio: true, video: false }, cameraOn: false });
    expect(session.getSnapshot().notice).toContain('audio only');
  });
});

describe('failures', () => {
  it('device permission denied fails with a device failure, never calls the join API, and offers listen-only', async () => {
    const { session, join, phases } = setup({ devices: fakeDevices({ failWith: { name: 'NotAllowedError' } }) });
    await session.join(MIC);
    expect(session.getSnapshot()).toMatchObject({ phase: 'failed', failure: { kind: 'device', deviceKind: 'permission-denied', retryable: true } });
    expect(join).not.toHaveBeenCalled();
    expect(phases).toEqual(['requesting-media', 'failed']);

    await session.joinListenOnly();
    expect(session.getSnapshot()).toMatchObject({ phase: 'connected', listenOnly: true, failure: null });
    expect(join).toHaveBeenCalledTimes(1);
  });

  it('a failed join (flag off, 402) is a join failure carrying the API error, with local media released', async () => {
    const error = new CallsApiError('Calls / RTC is not enabled for this plan', 402, 'FEATURE_NOT_ENABLED', false);
    const devices = fakeDevices();
    const { session, fetchImpl, leave } = setup({ devices, join: async () => { throw error; } });
    await session.join(MIC);
    const snap = session.getSnapshot();
    expect(snap).toMatchObject({ phase: 'failed', failure: { kind: 'join', message: error.message, cause: error } });
    expect(methods(fetchImpl, 'POST')).toEqual([]);
    expect(leave).not.toHaveBeenCalled();
    const opened = (await devices.getUserMedia.mock.results[0]?.value) as { getTracks: () => FakeTrack[] };
    expect(opened.getTracks().every((t) => t.readyState === 'ended')).toBe(true);
  });

  it('retry after a failed join runs the whole flow again', async () => {
    let attempts = 0;
    const { session, join } = setup({ join: async () => { attempts += 1; if (attempts === 1) throw new CallsApiError('busy', 503, 'UNAVAILABLE', true); return joined(); } });
    await session.join(MIC);
    expect(session.getSnapshot().phase).toBe('failed');
    await session.reconnect();
    expect(session.getSnapshot().phase).toBe('connected');
    expect(join).toHaveBeenCalledTimes(2);
  });

  it('a room token without a publish grant (403) falls back to listening with a notice', async () => {
    const { session, devices } = setup({ rules: { whip: 403 } });
    await session.join(MIC);
    expect(session.getSnapshot()).toMatchObject({ phase: 'connected', listenOnly: true, publishing: null, localStream: null });
    expect(session.getSnapshot().notice).toContain('listen');
    const opened = (await devices.getUserMedia.mock.results[0]?.value) as { getTracks: () => FakeTrack[] };
    expect(opened.getTracks().every((t) => t.readyState === 'ended')).toBe(true);
  });

  it('a media server outage (503) fails as retryable, unregisters from the room and releases the microphone', async () => {
    const { session, leave } = setup({ rules: { whip: 503 } });
    await session.join(MIC);
    expect(session.getSnapshot()).toMatchObject({ phase: 'failed', failure: { kind: 'media', retryable: true } });
    expect(leave).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot().localStream).toBeNull();
  });

  it('a rejected room token (401) is retryable by joining again; a non-retryable media failure is not', async () => {
    const unauthorized = setup({ rules: { whip: 401 } });
    await unauthorized.session.join(MIC);
    expect(unauthorized.session.getSnapshot().failure).toMatchObject({ kind: 'media', retryable: true });
    const notFound = setup({ rules: { whip: 404 } });
    await notFound.session.join(MIC);
    expect(notFound.session.getSnapshot().failure).toMatchObject({ kind: 'media', retryable: false });
  });

  it('losing the publish connection mid-call fails with connection-lost and reconnect recovers', async () => {
    const { session, leave, join } = setup();
    await session.join(MIC);
    FakePeerConnection.instances[0]?.emitConnectionState('connected');
    FakePeerConnection.instances[0]?.emitConnectionState('failed');
    await vi.waitFor(() => expect(session.getSnapshot().phase).toBe('failed'));
    expect(session.getSnapshot().failure).toMatchObject({ kind: 'connection-lost', retryable: true });
    expect(leave).toHaveBeenCalledTimes(1);

    await session.reconnect();
    expect(session.getSnapshot()).toMatchObject({ phase: 'connected', failure: null });
    expect(join).toHaveBeenCalledTimes(2);
  });

  it('reconnect and listen-only retry do nothing unless the session has failed', async () => {
    const { session, join } = setup();
    await session.reconnect();
    await session.joinListenOnly();
    expect(join).not.toHaveBeenCalled();
  });
});

describe('remote subscriptions', () => {
  it('subscribes to every other publisher, ignoring self, and returns streams per participant', async () => {
    const { session, fetchImpl } = setup();
    await session.join(MIC);
    await session.syncRemotes([SELF, ADA, GRACE]);
    expect(methods(fetchImpl, 'POST').filter((u) => u.includes('/whep')).sort()).toEqual([
      `https://media.example/rtc/rooms/r/whep?source=${ADA}`,
      `https://media.example/rtc/rooms/r/whep?source=${GRACE}`,
    ]);
    expect(session.getSnapshot().remotes.map((r) => r.participantId).sort()).toEqual([ADA, GRACE]);
    expect(session.getSnapshot().remotes.every((r) => r.stream !== null)).toBe(true);
    expect(session.getSnapshot().remoteProblems).toBe(0);
  });

  it('does nothing before the call is connected and does not re-subscribe known sources', async () => {
    const { session, fetchImpl } = setup();
    await session.syncRemotes([ADA]);
    expect(fetchImpl).not.toHaveBeenCalled();
    await session.join(MIC);
    await session.syncRemotes([ADA]);
    await session.syncRemotes([ADA]);
    expect(methods(fetchImpl, 'POST').filter((u) => u.includes('/whep'))).toHaveLength(1);
  });

  it('drops (and DELETEs) subscriptions for participants who stopped publishing', async () => {
    const { session, fetchImpl } = setup();
    await session.join(MIC);
    await session.syncRemotes([ADA, GRACE]);
    await session.syncRemotes([GRACE]);
    expect(session.getSnapshot().remotes.map((r) => r.participantId)).toEqual([GRACE]);
    await vi.waitFor(() => expect(methods(fetchImpl, 'DELETE')).toContain('https://media.example/rtc/rooms/r/sessions/sub-' + ADA));
  });

  it('a source that is not publishing yet (404) is normal churn: no problem counted, retried on the next sync', async () => {
    let ready = false;
    const { session } = setup({ rules: { whep: () => (ready ? 201 : 404) } });
    await session.join(MIC);
    await session.syncRemotes([ADA]);
    expect(session.getSnapshot()).toMatchObject({ remotes: [], remoteProblems: 0 });
    ready = true;
    await session.syncRemotes([ADA]);
    expect(session.getSnapshot().remotes.map((r) => r.participantId)).toEqual([ADA]);
  });

  it('counts a failing source as a problem and recovers on the next sync', async () => {
    let healthy = false;
    const { session } = setup({ rules: { whep: () => (healthy ? 201 : 503) } });
    await session.join(MIC);
    await session.syncRemotes([ADA]);
    expect(session.getSnapshot()).toMatchObject({ remotes: [], remoteProblems: 1 });
    healthy = true;
    await session.syncRemotes([ADA]);
    expect(session.getSnapshot()).toMatchObject({ remoteProblems: 0 });
    expect(session.getSnapshot().remotes).toHaveLength(1);
  });

  it('removes a remote whose connection fails so the next sync re-subscribes it', async () => {
    const { session, fetchImpl } = setup();
    await session.join(MIC);
    await session.syncRemotes([ADA]);
    const remotePc = FakePeerConnection.instances[1];
    remotePc?.emitConnectionState('connected');
    expect(session.getSnapshot().remotes[0]?.state).toBe('connected');
    remotePc?.emitConnectionState('failed');
    expect(session.getSnapshot().remotes).toEqual([]);
    await session.syncRemotes([ADA]);
    expect(session.getSnapshot().remotes).toHaveLength(1);
    expect(methods(fetchImpl, 'POST').filter((u) => u.includes('/whep'))).toHaveLength(2);
  });
});

describe('leave and dispose', () => {
  it('leave tears down publisher, subscribers and local tracks, calls the leave API once, and returns to idle', async () => {
    const { session, fetchImpl, leave, phases } = setup();
    await session.join(MIC);
    await session.syncRemotes([ADA]);
    const local = session.getSnapshot().localStream;
    await session.leave();
    expect(phases.slice(-2)).toEqual(['leaving', 'idle']);
    expect(session.getSnapshot()).toMatchObject({ phase: 'idle', failure: null, remotes: [], localStream: null, selfParticipantId: null });
    expect(local?.getTracks().every((t) => t.readyState === 'ended')).toBe(true);
    expect(FakePeerConnection.instances.every((p) => p.closed)).toBe(true);
    expect(methods(fetchImpl, 'DELETE')).toHaveLength(2);
    expect(leave).toHaveBeenCalledTimes(1);
    await session.leave();
    expect(leave).toHaveBeenCalledTimes(1);
  });

  it('a failing server-side leave is surfaced, but media is still released', async () => {
    const { session } = setup({ leave: async () => { throw new Error('hub-api unavailable'); } });
    await session.join(MIC);
    await session.leave();
    expect(session.getSnapshot()).toMatchObject({ phase: 'idle', failure: { kind: 'leave', message: 'hub-api unavailable', retryable: false }, localStream: null });
  });

  it('leaving while the join request is still in flight cleans up when it lands', async () => {
    let finish: (value: JoinedRoom) => void = () => undefined;
    const { session, leave, fetchImpl } = setup({ join: () => new Promise<JoinedRoom>((resolve) => { finish = resolve; }) });
    const joining = session.join(MIC);
    await vi.waitFor(() => expect(session.getSnapshot().phase).toBe('joining'));
    await session.leave();
    finish(joined());
    await joining;
    expect(session.getSnapshot().phase).toBe('idle');
    expect(leave).toHaveBeenCalledTimes(1);
    expect(methods(fetchImpl, 'POST')).toEqual([]);
  });

  it('leaving while the microphone prompt is open releases the stream once it resolves', async () => {
    let resolveMedia: (stream: unknown) => void = () => undefined;
    const devices = fakeDevices();
    devices.getUserMedia.mockImplementationOnce(() => new Promise((resolve) => { resolveMedia = resolve; }));
    const { session, join } = setup({ devices });
    const joining = session.join(MIC);
    await vi.waitFor(() => expect(session.getSnapshot().phase).toBe('requesting-media'));
    await session.leave();
    const track = new FakeTrack('audio');
    resolveMedia({ getTracks: () => [track] });
    await joining;
    expect(track.readyState).toBe('ended');
    expect(join).not.toHaveBeenCalled();
  });

  it('leaving while connecting stops the publisher that completes afterwards', async () => {
    let releasePost: (r: Response) => void = () => undefined;
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) =>
      init.method === 'DELETE' ? new Response(null, { status: 204 }) : new Promise<Response>((resolve) => { releasePost = resolve; }),
    );
    const leave = vi.fn(async () => undefined);
    const session = new CallSession({ join: async () => joined(), leave, devices: fakeDevices(), media: fakeMediaDeps(fetchImpl as unknown as ReturnType<typeof vi.fn>) });
    const joining = session.join(MIC);
    await vi.waitFor(() => expect(session.getSnapshot().phase).toBe('connecting'));
    await session.leave();
    releasePost(answerResponse());
    await joining;
    expect(session.getSnapshot().phase).toBe('idle');
    expect(FakePeerConnection.instances.every((p) => p.closed)).toBe(true);
    expect(leave).toHaveBeenCalledTimes(1);
  });

  it('dispose stops everything, unregisters, and stops notifying', async () => {
    const { session, leave, fetchImpl } = setup();
    await session.join(MIC);
    await session.syncRemotes([ADA]);
    const local = session.getSnapshot().localStream;
    const listener = vi.fn();
    session.subscribe(listener);
    session.dispose();
    session.dispose();
    await vi.waitFor(() => expect(leave).toHaveBeenCalledTimes(1));
    expect(local?.getTracks().every((t) => t.readyState === 'ended')).toBe(true);
    expect(FakePeerConnection.instances.every((p) => p.closed)).toBe(true);
    await vi.waitFor(() => expect(methods(fetchImpl, 'DELETE')).toHaveLength(2));
    expect(listener).not.toHaveBeenCalled();
  });

  it('unsubscribes listeners', async () => {
    const { session } = setup();
    const listener = vi.fn();
    const off = session.subscribe(listener);
    off();
    await session.join(LISTEN);
    expect(listener).not.toHaveBeenCalled();
  });
});

describe('switchDevice', () => {
  it('swaps the microphone without renegotiating, stops the old track, and keeps the mute state', async () => {
    const { session } = setup();
    await session.join(MIC);
    session.setMicMuted(true);
    const old = session.getSnapshot().localStream?.getAudioTracks()[0];
    await session.switchDevice('audio', 'usb-mic');
    const next = session.getSnapshot().localStream?.getAudioTracks()[0];
    expect(next).not.toBe(old);
    expect(old?.readyState).toBe('ended');
    expect(next?.enabled).toBe(false);
    expect(FakePeerConnection.instances[0]?.transceivers[0]?.sender.replaceTrack).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot().notice).toBeNull();
  });

  it('swaps the camera and keeps the audio track', async () => {
    const { session } = setup();
    await session.join(CAM);
    const audio = session.getSnapshot().localStream?.getAudioTracks()[0];
    await session.switchDevice('video', 'cam-2');
    expect(session.getSnapshot().localStream?.getAudioTracks()[0]).toBe(audio);
    expect(session.getSnapshot().localStream?.getVideoTracks()).toHaveLength(1);
  });

  it('reports a failed switch as a notice and keeps the current device', async () => {
    const devices = fakeDevices();
    const { session } = setup({ devices });
    await session.join(MIC);
    const current = session.getSnapshot().localStream?.getAudioTracks()[0];
    devices.getUserMedia.mockRejectedValueOnce({ name: 'NotReadableError' });
    await session.switchDevice('audio', 'busy-mic');
    expect(session.getSnapshot().notice).toContain('Could not switch device');
    expect(session.getSnapshot().localStream?.getAudioTracks()[0]).toBe(current);
    expect(current?.readyState).toBe('live');
  });

  it('does nothing when not publishing', async () => {
    const { session, devices } = setup();
    await session.join(LISTEN);
    await session.switchDevice('audio', 'x');
    expect(devices.getUserMedia).not.toHaveBeenCalled();
  });
});
