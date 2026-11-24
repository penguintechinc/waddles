/**
 * Browser WHIP publish / WHEP subscribe for svc-streaming room media.
 *
 * Mirrors the server's actual media model (`core/svc_streaming/src/rooms/media.rs`):
 *   - `POST <whip_url>`  SDP offer + `Authorization: Bearer <room token>` -> `201`, SDP answer, `Location` of the session.
 *   - `POST <whep_url>?source=<participant uuid>` SDP offer (recvonly audio + video) -> `201`, SDP answer, `Location`.
 *   - `DELETE <Location>` ends the session (same bearer token).
 * Constraints the server imposes, handled here:
 *   - Non-trickle ICE only (no PATCH): the offer is POSTed after gathering completes, so there is no ICE restart;
 *     recovery from a lost connection is "tear down and publish again" (see `CallSession.reconnect`).
 *   - The SFU forwards RTP without transcoding and its viewer tracks are H264 + Opus, so a publisher MUST offer
 *     H264 (packetization-mode 1) and Opus only. A browser without H264 publishes audio only.
 *   - The room token is a bearer secret: it lives only in a private field, is never put in a URL, an error message
 *     or a log line, and is never exposed to the UI.
 */

/** STUN/TURN entry as returned in the join response (empty for now; TURN is v3.3.x). */
export interface IceServerConfig {
  urls: string | string[];
  username?: string;
  credential?: string;
}

/** What went wrong talking to the media endpoint. */
export type MediaErrorKind =
  | 'unauthorized'
  | 'forbidden'
  | 'not-found'
  | 'unsupported'
  | 'unavailable'
  | 'ice-timeout'
  | 'bad-response'
  | 'aborted';

/** A media signalling failure; `message` never contains the token or the URL. */
export class MediaError extends Error {
  readonly kind: MediaErrorKind;
  readonly status: number;
  readonly retryable: boolean;

  constructor(kind: MediaErrorKind, message: string, status = 0) {
    super(message);
    this.name = 'MediaError';
    this.kind = kind;
    this.status = status;
    this.retryable = kind === 'unavailable' || kind === 'ice-timeout';
  }
}

/** Seams for tests and non-browser environments. */
export interface MediaDeps {
  createPeerConnection: (config: RTCConfiguration) => RTCPeerConnection;
  createStream: () => MediaStream;
  getCapabilities: (kind: 'audio' | 'video') => RTCRtpCapabilities | null;
  fetch: typeof fetch;
  /** How long to wait for ICE gathering to finish before using the candidates gathered so far. */
  gatherTimeoutMs: number;
}

/** Real browser dependencies, resolved lazily so importing this module never touches `window`. */
export function browserDeps(): MediaDeps {
  return {
    createPeerConnection: (config) => new RTCPeerConnection(config),
    createStream: () => new MediaStream(),
    getCapabilities: (kind) => RTCRtpSender.getCapabilities(kind),
    fetch: (input, init) => globalThis.fetch(input, init),
    gatherTimeoutMs: 4000,
  };
}

/** Connection state of one publish/subscribe session. */
export type MediaSessionState = 'idle' | 'connecting' | 'connected' | 'failed' | 'closed';

/** Maps an HTTP status from the media endpoint onto a typed error. */
export function errorForStatus(status: number): MediaError {
  if (status === 401) return new MediaError('unauthorized', 'The room token was rejected. Join again.', status);
  if (status === 403) return new MediaError('forbidden', 'You are not allowed to do that in this room.', status);
  if (status === 404) return new MediaError('not-found', 'The room or the stream is no longer available.', status);
  if (status === 405 || status === 501) return new MediaError('unsupported', 'The media server does not support this request.', status);
  if (status >= 500) return new MediaError('unavailable', 'The media server is unavailable. Try again.', status);
  return new MediaError('bad-response', `The media server answered with an unexpected status (HTTP ${status}).`, status);
}

/** Resolves once ICE gathering is complete, or after `timeoutMs` if at least one candidate was gathered. */
export function waitForIceGathering(pc: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const finish = (): void => {
      clearTimeout(timer);
      pc.removeEventListener('icegatheringstatechange', onChange);
    };
    const onChange = (): void => {
      if (pc.iceGatheringState === 'complete') {
        finish();
        resolve();
      }
    };
    const timer = setTimeout(() => {
      finish();
      if (pc.localDescription?.sdp.includes('a=candidate')) resolve();
      else reject(new MediaError('ice-timeout', 'Could not gather any network candidates. Check your connection.'));
    }, timeoutMs);
    pc.addEventListener('icegatheringstatechange', onChange);
  });
}

/** Result of a successful offer POST. */
interface OfferResult {
  answerSdp: string;
  /** Absolute URL of the session (from `Location`), or `null` when the server/CORS did not expose it. */
  sessionUrl: string | null;
}

/** POSTs an SDP offer with the bearer token and returns the answer; never throws a message containing the token. */
async function postOffer(deps: MediaDeps, url: string, token: string, offerSdp: string): Promise<OfferResult> {
  let response: Response;
  try {
    response = await deps.fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/sdp', Accept: 'application/sdp', Authorization: `Bearer ${token}` },
      body: offerSdp,
    });
  } catch {
    throw new MediaError('unavailable', 'Could not reach the media server. Check your connection and try again.');
  }
  if (response.status !== 201 && response.status !== 200) throw errorForStatus(response.status);
  const answerSdp = await response.text();
  if (!answerSdp.startsWith('v=0')) throw new MediaError('bad-response', 'The media server returned an invalid answer.', response.status);
  const location = response.headers.get('Location');
  return { answerSdp, sessionUrl: location ? new URL(location, url).toString() : null };
}

/** Ends a session; best effort (the server also reaps a dead peer connection), never throws. */
async function deleteSession(deps: MediaDeps, sessionUrl: string | null, token: string): Promise<void> {
  if (sessionUrl === null) return;
  try {
    await deps.fetch(sessionUrl, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` }, keepalive: true });
  } catch {
    // The peer connection is already closed locally; the server reaps the session when its side dies.
  }
}

/** True for codecs that are not media (RTX/RED/FEC), kept so loss recovery still works. */
function isAuxiliaryCodec(codec: RTCRtpCodec): boolean {
  return /\/(rtx|red|ulpfec|flexfec-03)$/i.test(codec.mimeType);
}

/**
 * Codec preference list for a sender: H264 (packetization-mode 1 first) for video, Opus for audio, plus RTX/RED.
 * Returns `[]` when the browser offers no usable codec of that kind.
 */
export function serverCodecPreferences(kind: 'audio' | 'video', capabilities: RTCRtpCodec[]): RTCRtpCodec[] {
  const wanted = kind === 'video' ? /^video\/h264$/i : /^audio\/opus$/i;
  const media = capabilities.filter((codec) => wanted.test(codec.mimeType));
  if (media.length === 0) return [];
  const rank = (codec: RTCRtpCodec): number => {
    const fmtp = codec.sdpFmtpLine ?? '';
    if (kind === 'video') return (fmtp.includes('packetization-mode=1') ? 0 : 2) + (/profile-level-id=42/i.test(fmtp) ? 0 : 1);
    return 0;
  };
  const sorted = [...media].sort((a, b) => rank(a) - rank(b));
  return [...sorted, ...capabilities.filter(isAuxiliaryCodec)];
}

/** Applies `serverCodecPreferences` to a transceiver; returns false when no usable codec exists (nothing is applied). */
function applyServerCodecs(transceiver: RTCRtpTransceiver, kind: 'audio' | 'video', getCapabilities: MediaDeps['getCapabilities']): boolean {
  const capabilities = getCapabilities(kind)?.codecs ?? [];
  const preferences = serverCodecPreferences(kind, capabilities);
  if (preferences.length === 0) return false;
  transceiver.setCodecPreferences(preferences);
  return true;
}

/** Options shared by publisher and subscriber. */
interface EndpointOptions {
  url: string;
  token: string;
  iceServers?: IceServerConfig[];
  deps?: Partial<MediaDeps>;
}

/** Result of `WhipPublisher.publish`. */
export interface PublishResult {
  /** True when a video track is being sent (false for audio-only or when the browser lacks H264). */
  video: boolean;
  audio: boolean;
}

/** One WHIP publish session: the local microphone/camera sent to the room's SFU. */
export class WhipPublisher {
  readonly #token: string;
  readonly #url: string;
  readonly #iceServers: IceServerConfig[];
  readonly #deps: MediaDeps;
  #pc: RTCPeerConnection | null = null;
  #sessionUrl: string | null = null;
  #state: MediaSessionState = 'idle';
  readonly #listeners = new Set<(state: MediaSessionState) => void>();

  constructor(options: EndpointOptions) {
    this.#token = options.token;
    this.#url = options.url;
    this.#iceServers = options.iceServers ?? [];
    this.#deps = { ...browserDeps(), ...options.deps };
  }

  get state(): MediaSessionState {
    return this.#state;
  }

  /** Subscribes to connection-state changes; returns the unsubscribe function. */
  onStateChange(listener: (state: MediaSessionState) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #setState(state: MediaSessionState): void {
    if (this.#state === state) return;
    this.#state = state;
    for (const listener of this.#listeners) listener(state);
  }

  /** Offers the stream's tracks (H264 + Opus only) and completes the WHIP handshake. */
  async publish(stream: MediaStream): Promise<PublishResult> {
    if (this.#pc !== null) throw new MediaError('aborted', 'Already publishing.');
    const pc = this.#deps.createPeerConnection({ iceServers: this.#iceServers });
    this.#pc = pc;
    this.#setState('connecting');
    try {
      const result: PublishResult = { video: false, audio: false };
      for (const track of stream.getTracks()) {
        if (track.kind !== 'audio' && track.kind !== 'video') continue;
        const transceiver = pc.addTransceiver(track, { direction: 'sendonly', streams: [stream] });
        if (applyServerCodecs(transceiver, track.kind, this.#deps.getCapabilities)) result[track.kind] = true;
        else pc.removeTrack(transceiver.sender);
      }
      if (!result.audio && !result.video) throw new MediaError('unsupported', 'This browser cannot send H264 video or Opus audio.');
      pc.addEventListener('connectionstatechange', () => this.#onConnectionState(pc));
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitForIceGathering(pc, this.#deps.gatherTimeoutMs);
      const sdp = pc.localDescription?.sdp;
      if (sdp === undefined) throw new MediaError('bad-response', 'No local description after gathering.');
      const posted = await postOffer(this.#deps, this.#url, this.#token, sdp);
      this.#sessionUrl = posted.sessionUrl;
      if (this.#pc !== pc) throw new MediaError('aborted', 'Stopped while connecting.');
      await pc.setRemoteDescription({ type: 'answer', sdp: posted.answerSdp });
      this.#onConnectionState(pc);
      return result;
    } catch (error) {
      await this.#teardown();
      this.#setState('failed');
      throw error instanceof MediaError ? error : new MediaError('bad-response', 'Could not start publishing.');
    }
  }

  #onConnectionState(pc: RTCPeerConnection): void {
    if (this.#pc !== pc) return;
    if (pc.connectionState === 'connected') this.#setState('connected');
    else if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') this.#setState('failed');
  }

  /** Swaps the sent track of one kind (device switch) without renegotiating. */
  async replaceTrack(kind: 'audio' | 'video', track: MediaStreamTrack | null): Promise<void> {
    const sender = this.#pc?.getSenders().find((candidate) => candidate.track?.kind === kind);
    if (sender === undefined) throw new MediaError('unsupported', `Not sending ${kind}.`);
    await sender.replaceTrack(track);
  }

  async #teardown(): Promise<void> {
    const pc = this.#pc;
    const sessionUrl = this.#sessionUrl;
    this.#pc = null;
    this.#sessionUrl = null;
    pc?.close();
    await deleteSession(this.#deps, sessionUrl, this.#token);
  }

  /** Closes the peer connection and DELETEs the session. Idempotent. */
  async stop(): Promise<void> {
    await this.#teardown();
    this.#setState('closed');
  }
}

/** Result of `WhepSubscriber.subscribe`. */
export interface SubscribeResult {
  /** Live stream; tracks are added as they arrive, so attach it to a media element immediately. */
  stream: MediaStream;
}

/** One WHEP subscribe session: a single remote participant's audio and video. */
export class WhepSubscriber {
  readonly #token: string;
  readonly #url: string;
  readonly #sourceId: string;
  readonly #iceServers: IceServerConfig[];
  readonly #deps: MediaDeps;
  #pc: RTCPeerConnection | null = null;
  #sessionUrl: string | null = null;
  #state: MediaSessionState = 'idle';
  readonly #listeners = new Set<(state: MediaSessionState) => void>();

  constructor(options: EndpointOptions & { sourceId: string }) {
    this.#token = options.token;
    this.#url = options.url;
    this.#sourceId = options.sourceId;
    this.#iceServers = options.iceServers ?? [];
    this.#deps = { ...browserDeps(), ...options.deps };
  }

  get state(): MediaSessionState {
    return this.#state;
  }

  get sourceId(): string {
    return this.#sourceId;
  }

  onStateChange(listener: (state: MediaSessionState) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #setState(state: MediaSessionState): void {
    if (this.#state === state) return;
    this.#state = state;
    for (const listener of this.#listeners) listener(state);
  }

  /** Completes the WHEP handshake for this source (`?source=<participant uuid>`). */
  async subscribe(): Promise<SubscribeResult> {
    if (this.#pc !== null) throw new MediaError('aborted', 'Already subscribed.');
    const pc = this.#deps.createPeerConnection({ iceServers: this.#iceServers });
    this.#pc = pc;
    this.#setState('connecting');
    const stream = this.#deps.createStream();
    try {
      pc.addEventListener('track', (event) => {
        stream.addTrack(event.track);
      });
      pc.addEventListener('connectionstatechange', () => this.#onConnectionState(pc));
      pc.addTransceiver('audio', { direction: 'recvonly' });
      pc.addTransceiver('video', { direction: 'recvonly' });
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitForIceGathering(pc, this.#deps.gatherTimeoutMs);
      const sdp = pc.localDescription?.sdp;
      if (sdp === undefined) throw new MediaError('bad-response', 'No local description after gathering.');
      const target = new URL(this.#url);
      target.searchParams.set('source', this.#sourceId);
      const posted = await postOffer(this.#deps, target.toString(), this.#token, sdp);
      this.#sessionUrl = posted.sessionUrl;
      if (this.#pc !== pc) throw new MediaError('aborted', 'Stopped while connecting.');
      await pc.setRemoteDescription({ type: 'answer', sdp: posted.answerSdp });
      this.#onConnectionState(pc);
      return { stream };
    } catch (error) {
      await this.#teardown();
      this.#setState('failed');
      throw error instanceof MediaError ? error : new MediaError('bad-response', 'Could not start receiving.');
    }
  }

  #onConnectionState(pc: RTCPeerConnection): void {
    if (this.#pc !== pc) return;
    if (pc.connectionState === 'connected') this.#setState('connected');
    else if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') this.#setState('failed');
  }

  async #teardown(): Promise<void> {
    const pc = this.#pc;
    const sessionUrl = this.#sessionUrl;
    this.#pc = null;
    this.#sessionUrl = null;
    pc?.close();
    await deleteSession(this.#deps, sessionUrl, this.#token);
  }

  /** Closes the peer connection and DELETEs the session. Idempotent. */
  async stop(): Promise<void> {
    await this.#teardown();
    this.#setState('closed');
  }
}
