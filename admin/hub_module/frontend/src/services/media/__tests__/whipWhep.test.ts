/**
 * WHIP/WHEP client against the server's documented media model (see `whipWhep.ts`): offer/answer over a bearer-authenticated
 * POST, session DELETE on stop, non-trickle gathering, no ICE restart (reconnect = new session), H264/Opus-only publishing,
 * per-source WHEP subscription, and a token that never leaks into errors. All against a fake RTCPeerConnection.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  MediaError,
  WhepSubscriber,
  WhipPublisher,
  errorForStatus,
  serverCodecPreferences,
  waitForIceGathering,
  type MediaSessionState,
} from '../whipWhep';
import {
  CHROME_CAPABILITIES,
  FakePeerConnection,
  FakeStream,
  FakeTrack,
  answerResponse,
  fakeMediaDeps,
  resetFakeRtc,
} from '../../../test/fakeRtc';

const TOKEN = 'room-token-SECRET-value';
const WHIP = 'https://media.example/rtc/rooms/3f2b8c1e/whip';
const WHEP = 'https://media.example/rtc/rooms/3f2b8c1e/whep';
const SOURCE = '7a1d2c3b-5e6f-4a80-9b1c-2d3e4f5a6b7c';

function localStream(kinds: Array<'audio' | 'video'> = ['audio', 'video']): MediaStream {
  return new FakeStream(kinds.map((k) => new FakeTrack(k))) as unknown as MediaStream;
}

function pc(index = 0): FakePeerConnection {
  const found = FakePeerConnection.instances[index];
  if (found === undefined) throw new Error('no peer connection was created');
  return found;
}

beforeEach(() => resetFakeRtc());

describe('serverCodecPreferences', () => {
  it('prefers H264 packetization-mode 1, drops VP8, keeps RTX', () => {
    const prefs = serverCodecPreferences('video', [...CHROME_CAPABILITIES.video] as unknown as RTCRtpCodec[]);
    expect(prefs.map((c) => c.mimeType)).toEqual(['video/H264', 'video/H264', 'video/rtx']);
    expect(prefs[0]?.sdpFmtpLine).toContain('packetization-mode=1');
  });

  it('offers Opus only for audio', () => {
    const prefs = serverCodecPreferences('audio', [...CHROME_CAPABILITIES.audio] as unknown as RTCRtpCodec[]);
    expect(prefs.map((c) => c.mimeType)).toEqual(['audio/opus']);
  });

  it('returns nothing when the browser has no usable codec', () => {
    expect(serverCodecPreferences('video', [{ mimeType: 'video/VP8', clockRate: 90000 }] as RTCRtpCodec[])).toEqual([]);
    expect(serverCodecPreferences('audio', [{ mimeType: 'audio/PCMU', clockRate: 8000 }] as RTCRtpCodec[])).toEqual([]);
  });
});

describe('errorForStatus', () => {
  it.each([
    [401, 'unauthorized', false],
    [403, 'forbidden', false],
    [404, 'not-found', false],
    [405, 'unsupported', false],
    [503, 'unavailable', true],
    [418, 'bad-response', false],
  ])('maps HTTP %i to %s (retryable %s)', (status, kind, retryable) => {
    expect(errorForStatus(status)).toMatchObject({ kind, status, retryable });
  });
});

describe('waitForIceGathering', () => {
  it('resolves immediately when gathering is already complete', async () => {
    const p = new FakePeerConnection({});
    p.iceGatheringState = 'complete';
    await expect(waitForIceGathering(p as unknown as RTCPeerConnection, 50)).resolves.toBeUndefined();
  });

  it('resolves on the gathering-complete event', async () => {
    const p = new FakePeerConnection({});
    const waiting = waitForIceGathering(p as unknown as RTCPeerConnection, 1000);
    p.iceGatheringState = 'complete';
    p.dispatchEvent(new Event('icegatheringstatechange'));
    await expect(waiting).resolves.toBeUndefined();
  });

  it('ignores a gathering event that is not complete yet', async () => {
    const p = new FakePeerConnection({});
    p.localDescription = { type: 'offer', sdp: 'v=0\r\na=candidate:1\r\n' };
    const waiting = waitForIceGathering(p as unknown as RTCPeerConnection, 20);
    p.iceGatheringState = 'gathering';
    p.dispatchEvent(new Event('icegatheringstatechange'));
    await expect(waiting).resolves.toBeUndefined();
  });

  it('proceeds on timeout when at least one candidate exists, fails with ice-timeout otherwise', async () => {
    const withCandidate = new FakePeerConnection({});
    withCandidate.localDescription = { type: 'offer', sdp: 'v=0\r\na=candidate:1 1 UDP\r\n' };
    await expect(waitForIceGathering(withCandidate as unknown as RTCPeerConnection, 10)).resolves.toBeUndefined();
    const empty = new FakePeerConnection({});
    empty.localDescription = { type: 'offer', sdp: 'v=0\r\n' };
    await expect(waitForIceGathering(empty as unknown as RTCPeerConnection, 10)).rejects.toMatchObject({ kind: 'ice-timeout', retryable: true });
  });
});

describe('WhipPublisher', () => {
  it('POSTs the offer with a bearer token as application/sdp, applies the answer and reports connected', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(answerResponse());
    const publisher = new WhipPublisher({ url: WHIP, token: TOKEN, iceServers: [{ urls: 'stun:stun.example:3478' }], deps: fakeMediaDeps(fetchImpl) });
    const states: MediaSessionState[] = [];
    publisher.onStateChange((s) => states.push(s));

    const result = await publisher.publish(localStream());

    expect(result).toEqual({ audio: true, video: true });
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(WHIP);
    expect(init.method).toBe('POST');
    expect(init.headers).toMatchObject({ 'Content-Type': 'application/sdp', Authorization: `Bearer ${TOKEN}` });
    expect(String(init.body)).toContain('m=audio');
    expect(String(init.body)).toContain('m=video');
    expect(pc().config).toEqual({ iceServers: [{ urls: 'stun:stun.example:3478' }] });
    expect(pc().transceivers.map((t) => [t.kind, t.direction])).toEqual([['audio', 'sendonly'], ['video', 'sendonly']]);
    for (const t of pc().transceivers) expect(t.setCodecPreferences).toHaveBeenCalledTimes(1);
    expect(pc().remoteDescription).toEqual({ type: 'answer', sdp: 'v=0\r\nanswer\r\n' });

    pc().emitConnectionState('connected');
    expect(publisher.state).toBe('connected');
    expect(states).toEqual(['connecting', 'connected']);
  });

  it('never puts the token in the URL, in a thrown message, or on the fetch of a failed attempt', async () => {
    for (const status of [401, 403, 404, 405, 500, 418]) {
      resetFakeRtc();
      const fetchImpl = vi.fn().mockResolvedValue(new Response('nope', { status }));
      const publisher = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(fetchImpl) });
      const error = await publisher.publish(localStream()).catch((e: unknown) => e);
      expect(error).toBeInstanceOf(MediaError);
      expect(String((error as MediaError).message)).not.toContain(TOKEN);
      expect(String((error as MediaError).message)).not.toContain('media.example');
      expect(String(fetchImpl.mock.calls[0]?.[0])).not.toContain(TOKEN);
      expect(publisher.state).toBe('failed');
      expect(pc().closed).toBe(true);
    }
  });

  it('DELETEs the session from Location (resolved against the WHIP URL) with the token on stop, and is idempotent', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(answerResponse('/rtc/rooms/3f2b8c1e/sessions/abc')).mockResolvedValue(new Response(null, { status: 204 }));
    const publisher = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(fetchImpl) });
    await publisher.publish(localStream());

    await publisher.stop();
    await publisher.stop();

    expect(pc().closed).toBe(true);
    expect(publisher.state).toBe('closed');
    const deletes = fetchImpl.mock.calls.filter(([, init]) => (init as RequestInit).method === 'DELETE');
    expect(deletes).toHaveLength(1);
    expect(deletes[0]?.[0]).toBe('https://media.example/rtc/rooms/3f2b8c1e/sessions/abc');
    expect((deletes[0]?.[1] as RequestInit).headers).toMatchObject({ Authorization: `Bearer ${TOKEN}` });
    expect((deletes[0]?.[1] as RequestInit).keepalive).toBe(true);
  });

  it('skips the DELETE when the server did not expose a Location, and swallows a failing DELETE', async () => {
    const noLocation = vi.fn().mockResolvedValue(answerResponse(null));
    const a = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(noLocation) });
    await a.publish(localStream());
    await a.stop();
    expect(noLocation).toHaveBeenCalledTimes(1);

    resetFakeRtc();
    const failingDelete = vi.fn().mockResolvedValueOnce(answerResponse()).mockRejectedValueOnce(new Error('offline'));
    const b = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(failingDelete) });
    await b.publish(localStream());
    await expect(b.stop()).resolves.toBeUndefined();
  });

  it('never uses PATCH (the server is non-trickle, there is no ICE restart): only POST and DELETE', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(answerResponse()).mockResolvedValue(new Response(null, { status: 204 }));
    const publisher = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(fetchImpl) });
    await publisher.publish(localStream());
    pc().emitConnectionState('disconnected');
    await publisher.stop();
    expect(new Set(fetchImpl.mock.calls.map(([, init]) => (init as RequestInit).method))).toEqual(new Set(['POST', 'DELETE']));
  });

  it('reports a failed/disconnected connection and lets listeners unsubscribe', async () => {
    const publisher = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(vi.fn().mockResolvedValue(answerResponse())) });
    const seen: MediaSessionState[] = [];
    const off = publisher.onStateChange((s) => seen.push(s));
    await publisher.publish(localStream());
    pc().emitConnectionState('failed');
    expect(publisher.state).toBe('failed');
    off();
    pc().emitConnectionState('connected');
    expect(seen).toEqual(['connecting', 'failed']);
  });

  it('publishes audio only when the browser cannot send H264, and fails when it can send neither', async () => {
    const noH264 = { video: [{ mimeType: 'video/VP8', clockRate: 90000 }], audio: CHROME_CAPABILITIES.audio };
    const a = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(vi.fn().mockResolvedValue(answerResponse()), noH264) });
    await expect(a.publish(localStream())).resolves.toEqual({ audio: true, video: false });
    expect(pc().transceivers.find((t) => t.kind === 'video')?.sender.track).toBeNull();

    resetFakeRtc();
    const none = { video: [{ mimeType: 'video/VP8', clockRate: 90000 }], audio: [{ mimeType: 'audio/PCMU', clockRate: 8000 }] };
    const b = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(vi.fn(), none) });
    await expect(b.publish(localStream())).rejects.toMatchObject({ kind: 'unsupported' });
    expect(pc().closed).toBe(true);
  });

  it('classifies network failures as retryable unavailable, and a bad answer as bad-response', async () => {
    const offline = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))) });
    await expect(offline.publish(localStream())).rejects.toMatchObject({ kind: 'unavailable', retryable: true });
    resetFakeRtc();
    const garbage = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(vi.fn().mockResolvedValue(answerResponse('/s', '<html>')) ) });
    await expect(garbage.publish(localStream())).rejects.toMatchObject({ kind: 'bad-response' });
  });

  it('fails with ice-timeout when no candidate is ever gathered', async () => {
    FakePeerConnection.gather = { mode: 'stall-empty' };
    const publisher = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(vi.fn()) });
    await expect(publisher.publish(localStream())).rejects.toMatchObject({ kind: 'ice-timeout' });
  });

  it('continues with the gathered candidates after the gather timeout', async () => {
    FakePeerConnection.gather = { mode: 'stall-with-candidate' };
    const fetchImpl = vi.fn().mockResolvedValue(answerResponse());
    const publisher = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(fetchImpl) });
    await expect(publisher.publish(localStream(['audio']))).resolves.toEqual({ audio: true, video: false });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('refuses a second publish and wraps unexpected errors', async () => {
    const publisher = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(vi.fn().mockResolvedValue(answerResponse())) });
    await publisher.publish(localStream());
    await expect(publisher.publish(localStream())).rejects.toMatchObject({ kind: 'aborted' });

    resetFakeRtc();
    const deps = fakeMediaDeps(vi.fn().mockResolvedValue(answerResponse()));
    const broken = new WhipPublisher({ url: WHIP, token: TOKEN, deps: { ...deps, createPeerConnection: (c) => { const p = new FakePeerConnection(c); p.createOffer = async () => { throw new Error('boom'); }; return p as unknown as RTCPeerConnection; } } });
    await expect(broken.publish(localStream())).rejects.toMatchObject({ kind: 'bad-response', message: 'Could not start publishing.' });
  });

  it('aborts when stopped while the offer is in flight', async () => {
    let release: (r: Response) => void = () => undefined;
    const fetchImpl = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { release = resolve; })).mockResolvedValue(new Response(null, { status: 204 }));
    const publisher = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(fetchImpl) });
    const publishing = publisher.publish(localStream());
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalled());
    await publisher.stop();
    release(answerResponse());
    await expect(publishing).rejects.toMatchObject({ kind: 'aborted' });
    // The server did create a session in the meantime: it must be ended, not orphaned.
    expect(fetchImpl).toHaveBeenLastCalledWith('https://media.example/rtc/rooms/room/sessions/sess', expect.objectContaining({ method: 'DELETE' }));
  });

  it('replaces a track without renegotiating, and refuses when that kind is not being sent', async () => {
    const publisher = new WhipPublisher({ url: WHIP, token: TOKEN, deps: fakeMediaDeps(vi.fn().mockResolvedValue(answerResponse())) });
    await publisher.publish(localStream(['audio']));
    const next = new FakeTrack('audio', 'usb mic');
    await publisher.replaceTrack('audio', next as unknown as MediaStreamTrack);
    expect(pc().transceivers[0]?.sender.replaceTrack).toHaveBeenCalledWith(next);
    await expect(publisher.replaceTrack('video', null)).rejects.toMatchObject({ kind: 'unsupported' });
  });
});

describe('WhepSubscriber', () => {
  it('subscribes to one source: recvonly audio+video, ?source=<uuid>, bearer token, answer applied', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(answerResponse('/rtc/rooms/3f2b8c1e/sessions/v1'));
    const subscriber = new WhepSubscriber({ url: WHEP, token: TOKEN, sourceId: SOURCE, deps: fakeMediaDeps(fetchImpl) });

    const { stream } = await subscriber.subscribe();

    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${WHEP}?source=${SOURCE}`);
    expect(init.headers).toMatchObject({ Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/sdp' });
    expect(pc().transceivers.map((t) => [t.kind, t.direction])).toEqual([['audio', 'recvonly'], ['video', 'recvonly']]);
    expect(pc().remoteDescription?.type).toBe('answer');
    expect(subscriber.sourceId).toBe(SOURCE);

    const audio = new FakeTrack('audio');
    pc().emitTrack(audio);
    expect((stream as unknown as FakeStream).getTracks()).toEqual([audio]);

    pc().emitConnectionState('connected');
    expect(subscriber.state).toBe('connected');
  });

  it('keeps an existing query string on the WHEP URL', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(answerResponse());
    const subscriber = new WhepSubscriber({ url: `${WHEP}?x=1`, token: TOKEN, sourceId: SOURCE, deps: fakeMediaDeps(fetchImpl) });
    await subscriber.subscribe();
    expect(String(fetchImpl.mock.calls[0]?.[0])).toBe(`${WHEP}?x=1&source=${SOURCE}`);
  });

  it('DELETEs its session on stop and reports closed', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(answerResponse('/rtc/rooms/3f2b8c1e/sessions/v1')).mockResolvedValue(new Response(null, { status: 204 }));
    const subscriber = new WhepSubscriber({ url: WHEP, token: TOKEN, sourceId: SOURCE, deps: fakeMediaDeps(fetchImpl) });
    await subscriber.subscribe();
    await subscriber.stop();
    expect(pc().closed).toBe(true);
    expect(subscriber.state).toBe('closed');
    expect(fetchImpl).toHaveBeenLastCalledWith('https://media.example/rtc/rooms/3f2b8c1e/sessions/v1', expect.objectContaining({ method: 'DELETE' }));
  });

  it('maps a source that is not publishing (404) and other statuses to typed errors without the token', async () => {
    const gone = new WhepSubscriber({ url: WHEP, token: TOKEN, sourceId: SOURCE, deps: fakeMediaDeps(vi.fn().mockResolvedValue(new Response('', { status: 404 }))) });
    const error = await gone.subscribe().catch((e: unknown) => e);
    expect(error).toMatchObject({ kind: 'not-found' });
    expect((error as MediaError).message).not.toContain(TOKEN);
    expect(gone.state).toBe('failed');
    resetFakeRtc();
    const forbidden = new WhepSubscriber({ url: WHEP, token: TOKEN, sourceId: SOURCE, deps: fakeMediaDeps(vi.fn().mockResolvedValue(new Response('', { status: 403 }))) });
    await expect(forbidden.subscribe()).rejects.toMatchObject({ kind: 'forbidden' });
  });

  it('refuses a second subscribe, reports failure on disconnect, supports unsubscribe, and wraps unexpected errors', async () => {
    const subscriber = new WhepSubscriber({ url: WHEP, token: TOKEN, sourceId: SOURCE, deps: fakeMediaDeps(vi.fn().mockResolvedValue(answerResponse())) });
    const seen: MediaSessionState[] = [];
    const off = subscriber.onStateChange((s) => seen.push(s));
    await subscriber.subscribe();
    await expect(subscriber.subscribe()).rejects.toMatchObject({ kind: 'aborted' });
    pc().emitConnectionState('disconnected');
    expect(subscriber.state).toBe('failed');
    off();
    pc().emitConnectionState('connected');
    expect(seen).toEqual(['connecting', 'failed']);

    resetFakeRtc();
    const deps = fakeMediaDeps(vi.fn().mockResolvedValue(answerResponse()));
    const broken = new WhepSubscriber({ url: WHEP, token: TOKEN, sourceId: SOURCE, deps: { ...deps, createPeerConnection: (c) => { const p = new FakePeerConnection(c); p.createOffer = async () => { throw new Error('boom'); }; return p as unknown as RTCPeerConnection; } } });
    await expect(broken.subscribe()).rejects.toMatchObject({ kind: 'bad-response', message: 'Could not start receiving.' });
  });

  it('aborts when stopped while the offer is in flight', async () => {
    let release: (r: Response) => void = () => undefined;
    const fetchImpl = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { release = resolve; })).mockResolvedValue(new Response(null, { status: 204 }));
    const subscriber = new WhepSubscriber({ url: WHEP, token: TOKEN, sourceId: SOURCE, deps: fakeMediaDeps(fetchImpl) });
    const subscribing = subscriber.subscribe();
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalled());
    await subscriber.stop();
    release(answerResponse());
    await expect(subscribing).rejects.toMatchObject({ kind: 'aborted' });
    expect(fetchImpl).toHaveBeenLastCalledWith('https://media.example/rtc/rooms/room/sessions/sess', expect.objectContaining({ method: 'DELETE' }));
  });
});
