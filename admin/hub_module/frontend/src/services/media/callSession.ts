/**
 * Join/leave state machine for one room call: open local media, register with hub-api, publish over WHIP,
 * subscribe (WHEP) to every remote publisher, expose mute/camera/device controls, and tear everything down.
 *
 *   idle -> requesting-media -> joining -> connecting -> connected -> leaving -> idle
 *                 \-> failed (device) \-> failed (join) \-> failed (media / connection lost) -> (retry|reconnect)
 *
 * It is a plain class (no React) so the state machine is unit-testable; `useCallSession` adapts it to React.
 * The room token returned by the join API is held only in a private field and handed straight to the media
 * endpoints; it is never part of a snapshot, a message or a log line.
 */
import type { JoinedRoom } from '../callsApi';
import {
  DeviceError,
  browserDevices,
  openLocalMedia,
  stopStream,
  type DeviceDeps,
  type DeviceErrorKind,
  type LocalMediaRequest,
} from './devices';
import {
  MediaError,
  WhepSubscriber,
  WhipPublisher,
  browserDeps,
  type MediaDeps,
} from './whipWhep';

/** Where the call is in its lifecycle. */
export type CallPhase = 'idle' | 'requesting-media' | 'joining' | 'connecting' | 'connected' | 'leaving' | 'failed';

/** Why the call is not (or no longer) running. */
export interface CallFailure {
  kind: 'device' | 'join' | 'media' | 'connection-lost' | 'leave';
  message: string;
  retryable: boolean;
  /** Set for `device` failures so the UI can offer listen-only. */
  deviceKind?: DeviceErrorKind;
  /** The original error (a `CallsApiError` for `join`), for kind-specific rendering such as flag-off (402). */
  cause?: unknown;
}

/** One remote participant's media. */
export interface RemoteMedia {
  participantId: string;
  stream: MediaStream | null;
  state: 'connecting' | 'connected';
}

/** Immutable view of the session for rendering. */
export interface CallSnapshot {
  phase: CallPhase;
  failure: CallFailure | null;
  /** Non-fatal information, e.g. "listen-only because the room token has no publish grant". */
  notice: string | null;
  selfParticipantId: string | null;
  micMuted: boolean;
  cameraOn: boolean;
  /** What is actually being sent; `null` while not publishing. */
  publishing: { audio: boolean; video: boolean } | null;
  listenOnly: boolean;
  localStream: MediaStream | null;
  remotes: readonly RemoteMedia[];
  /** Remote participants whose media could not be opened on the last attempt (retried on the next sync). */
  remoteProblems: number;
}

/** What the user picked in the lobby. */
export interface JoinOptions {
  camera: boolean;
  listenOnly: boolean;
  audioDeviceId?: string;
  videoDeviceId?: string;
}

/** Collaborators; the API calls are injected so the session knows nothing about React Query or axios. */
export interface SessionDeps {
  join: () => Promise<JoinedRoom>;
  leave: () => Promise<void>;
  devices?: DeviceDeps;
  media?: Partial<MediaDeps>;
}

/** The snapshot of a session that has not started (also used before a session object exists). */
export const IDLE_SNAPSHOT: CallSnapshot = {
  phase: 'idle',
  failure: null,
  notice: null,
  selfParticipantId: null,
  micMuted: false,
  cameraOn: false,
  publishing: null,
  listenOnly: false,
  localStream: null,
  remotes: [],
  remoteProblems: 0,
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message !== '' ? error.message : fallback;
}

/** One call session; create one per room view and `dispose()` it on unmount. */
export class CallSession {
  readonly #deps: SessionDeps;
  readonly #mediaDeps: MediaDeps;
  readonly #deviceDeps: DeviceDeps;
  readonly #listeners = new Set<() => void>();
  #snapshot: CallSnapshot = IDLE_SNAPSHOT;
  #token: string | null = null;
  #joined: JoinedRoom | null = null;
  #registered = false;
  #publisher: WhipPublisher | null = null;
  readonly #subscribers = new Map<string, WhepSubscriber>();
  readonly #pending = new Set<string>();
  #generation = 0;
  #disposed = false;
  #lastOptions: JoinOptions | null = null;

  constructor(deps: SessionDeps) {
    this.#deps = deps;
    this.#mediaDeps = { ...browserDeps(), ...deps.media };
    this.#deviceDeps = deps.devices ?? browserDevices();
  }

  getSnapshot = (): CallSnapshot => this.#snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  #emit(patch: Partial<CallSnapshot>): void {
    if (this.#disposed) return;
    this.#snapshot = { ...this.#snapshot, ...patch };
    for (const listener of this.#listeners) listener();
  }

  /** Runs the join flow. Valid from `idle` or `failed`; a stale flow (left/disposed meanwhile) cleans up after itself. */
  async join(options: JoinOptions): Promise<void> {
    const phase = this.#snapshot.phase;
    if (phase !== 'idle' && phase !== 'failed') return;
    this.#lastOptions = options;
    const generation = ++this.#generation;
    const stale = (): boolean => generation !== this.#generation || this.#disposed;
    this.#emit({ ...IDLE_SNAPSHOT, phase: options.listenOnly ? 'joining' : 'requesting-media', listenOnly: options.listenOnly, cameraOn: false });

    let local: MediaStream | null = null;
    if (!options.listenOnly) {
      const request: LocalMediaRequest = { camera: options.camera };
      if (options.audioDeviceId !== undefined) request.audioDeviceId = options.audioDeviceId;
      if (options.videoDeviceId !== undefined) request.videoDeviceId = options.videoDeviceId;
      try {
        local = await openLocalMedia(this.#deviceDeps, request);
      } catch (error) {
        if (stale()) return;
        const device = error instanceof DeviceError ? error : new DeviceError('unknown', 'Could not open the microphone or camera.');
        this.#emit({ phase: 'failed', failure: { kind: 'device', message: device.message, retryable: true, deviceKind: device.kind, cause: error } });
        return;
      }
      if (stale()) {
        stopStream(local);
        return;
      }
      this.#emit({ phase: 'joining' });
    }

    let joined: JoinedRoom;
    try {
      joined = await this.#deps.join();
    } catch (error) {
      stopStream(local);
      if (stale()) return;
      this.#emit({ phase: 'failed', failure: { kind: 'join', message: errorMessage(error, 'Could not join the room.'), retryable: true, cause: error } });
      return;
    }
    this.#registered = true;
    if (stale()) {
      stopStream(local);
      await this.#leaveServer();
      return;
    }
    this.#joined = joined;
    this.#token = joined.token;
    this.#emit({ phase: 'connecting', selfParticipantId: joined.participantId });

    let publishing: CallSnapshot['publishing'] = null;
    let notice: string | null = null;
    if (!joined.publish && local !== null) {
      // The room policy did not grant publishing: honour it without even trying the WHIP endpoint.
      stopStream(local);
      local = null;
      notice = 'You can listen in this room but not speak or share video.';
    }
    if (!joined.subscribe) notice = 'You are not allowed to listen in this room.';
    if (local !== null) {
      const publisher = new WhipPublisher({ url: joined.whipUrl, token: joined.token, iceServers: joined.iceServers, deps: this.#mediaDeps });
      try {
        const result = await publisher.publish(local);
        if (stale()) {
          await publisher.stop();
          stopStream(local);
          await this.#leaveServer();
          return;
        }
        this.#publisher = publisher;
        publishing = result;
        if (!result.video && options.camera) notice = 'Your browser cannot send H264 video here, so you are sharing audio only.';
        publisher.onStateChange((state) => {
          if (state === 'failed' && this.#snapshot.phase === 'connected') void this.#connectionLost();
        });
      } catch (error) {
        stopStream(local);
        local = null;
        if (stale()) {
          await this.#leaveServer();
          return;
        }
        if (error instanceof MediaError && error.kind === 'forbidden') {
          notice = 'You can listen in this room but not speak or share video.';
        } else {
          await this.#releaseMedia();
          await this.#leaveServer();
          this.#emit({ phase: 'failed', failure: { kind: 'media', message: errorMessage(error, 'Could not connect audio and video.'), retryable: !(error instanceof MediaError) || error.retryable || error.kind === 'unauthorized', cause: error } });
          return;
        }
      }
    }
    this.#emit({
      phase: 'connected',
      publishing,
      notice,
      localStream: local,
      cameraOn: publishing?.video === true,
      micMuted: false,
      listenOnly: publishing === null,
    });
  }

  /** Reconciles remote subscriptions with the participants currently publishing (self is ignored). */
  async syncRemotes(publisherIds: readonly string[]): Promise<void> {
    if (this.#snapshot.phase !== 'connected' || this.#joined === null || this.#token === null || !this.#joined.subscribe) return;
    const wanted = new Set(publisherIds.filter((id) => id !== this.#snapshot.selfParticipantId));
    for (const [id, subscriber] of [...this.#subscribers]) {
      if (!wanted.has(id)) {
        this.#subscribers.delete(id);
        void subscriber.stop();
      }
    }
    const fresh = [...wanted].filter((id) => !this.#subscribers.has(id) && !this.#pending.has(id));
    this.#emitRemotes();
    await Promise.all(fresh.map((id) => this.#subscribe(id)));
  }

  #emitRemotes(extra: Partial<CallSnapshot> = {}): void {
    const remotes: RemoteMedia[] = [...this.#subscribers].map(([participantId, subscriber]) => ({
      participantId,
      stream: this.#streams.get(participantId) ?? null,
      state: subscriber.state === 'connected' ? 'connected' : 'connecting',
    }));
    this.#emit({ remotes, ...extra });
  }

  readonly #streams = new Map<string, MediaStream>();
  #problems = new Set<string>();

  async #subscribe(participantId: string): Promise<void> {
    const joined = this.#joined;
    const token = this.#token;
    if (joined === null || token === null) return;
    const generation = this.#generation;
    this.#pending.add(participantId);
    const subscriber = new WhepSubscriber({
      url: joined.whepUrl,
      token,
      sourceId: participantId,
      iceServers: joined.iceServers,
      deps: this.#mediaDeps,
    });
    this.#subscribers.set(participantId, subscriber);
    this.#emitRemotes();
    try {
      const { stream } = await subscriber.subscribe();
      if (generation !== this.#generation || this.#subscribers.get(participantId) !== subscriber) {
        await subscriber.stop();
        return;
      }
      this.#streams.set(participantId, stream);
      this.#problems.delete(participantId);
      subscriber.onStateChange((state) => {
        if (state === 'failed' && this.#subscribers.get(participantId) === subscriber) {
          this.#drop(participantId);
          void subscriber.stop();
          this.#emitRemotes({ remoteProblems: this.#problems.size });
        } else {
          this.#emitRemotes();
        }
      });
      this.#emitRemotes({ remoteProblems: this.#problems.size });
    } catch (error) {
      if (this.#subscribers.get(participantId) === subscriber) this.#drop(participantId);
      // A source that is not publishing (yet) is normal churn; anything else is reported and retried on the next sync.
      if (!(error instanceof MediaError && error.kind === 'not-found')) this.#problems.add(participantId);
      this.#emitRemotes({ remoteProblems: this.#problems.size });
    } finally {
      this.#pending.delete(participantId);
    }
  }

  #drop(participantId: string): void {
    this.#subscribers.delete(participantId);
    this.#streams.delete(participantId);
  }

  /** Mutes or unmutes the local microphone (the track keeps its device; no renegotiation). */
  setMicMuted(muted: boolean): void {
    this.#snapshot.localStream?.getAudioTracks().forEach((track) => {
      track.enabled = !muted;
    });
    this.#emit({ micMuted: muted });
  }

  /** Turns the local camera on or off (only when video is being sent). */
  setCameraEnabled(on: boolean): void {
    if (this.#snapshot.publishing?.video !== true) return;
    this.#snapshot.localStream?.getVideoTracks().forEach((track) => {
      track.enabled = on;
    });
    this.#emit({ cameraOn: on });
  }

  /** Switches the microphone or camera while connected; the old track is stopped only after the swap succeeded. */
  async switchDevice(kind: 'audio' | 'video', deviceId: string): Promise<void> {
    const publisher = this.#publisher;
    const local = this.#snapshot.localStream;
    if (publisher === null || local === null) return;
    const request: LocalMediaRequest =
      kind === 'audio' ? { camera: false, audioDeviceId: deviceId } : { camera: true, audio: false, videoDeviceId: deviceId };
    try {
      const opened = await openLocalMedia(this.#deviceDeps, request);
      const next = kind === 'audio' ? opened.getAudioTracks()[0] : opened.getVideoTracks()[0];
      if (next === undefined) throw new DeviceError('no-device', 'The selected device returned no media.');
      await publisher.replaceTrack(kind, next);
      const previous = (kind === 'audio' ? local.getAudioTracks() : local.getVideoTracks())[0];
      if (kind === 'audio') next.enabled = !this.#snapshot.micMuted;
      else next.enabled = this.#snapshot.cameraOn;
      const stream = this.#mediaDeps.createStream();
      for (const track of local.getTracks()) if (track !== previous) stream.addTrack(track);
      stream.addTrack(next);
      previous?.stop();
      this.#emit({ localStream: stream, notice: null });
    } catch (error) {
      const message = errorMessage(error, 'Could not switch devices.');
      this.#emit({ notice: `Could not switch device: ${message}` });
    }
  }

  async #connectionLost(): Promise<void> {
    await this.#releaseMedia();
    await this.#leaveServer();
    this.#emit({ phase: 'failed', failure: { kind: 'connection-lost', message: 'The connection to the room was lost.', retryable: true } });
  }

  /** Tears down every media session and local track (no server call). */
  async #releaseMedia(): Promise<void> {
    const publisher = this.#publisher;
    const subscribers = [...this.#subscribers.values()];
    const local = this.#snapshot.localStream;
    this.#publisher = null;
    this.#subscribers.clear();
    this.#streams.clear();
    this.#pending.clear();
    this.#problems = new Set();
    this.#token = null;
    this.#joined = null;
    stopStream(local);
    await Promise.all([publisher?.stop(), ...subscribers.map((s) => s.stop())]);
    this.#emit({ localStream: null, remotes: [], publishing: null, remoteProblems: 0 });
  }

  /** Unregisters from hub-api if registered; resolves with the error (or null) instead of throwing. */
  async #leaveServer(): Promise<unknown> {
    if (!this.#registered) return null;
    this.#registered = false;
    try {
      await this.#deps.leave();
      return null;
    } catch (error) {
      return error;
    }
  }

  /** Leaves the room. Media is always torn down; a failed server-side leave is surfaced as a `leave` failure. */
  async leave(): Promise<void> {
    if (this.#snapshot.phase === 'idle' || this.#snapshot.phase === 'leaving') return;
    this.#generation += 1;
    this.#emit({ phase: 'leaving', notice: null });
    await this.#releaseMedia();
    const error = await this.#leaveServer();
    this.#emit({
      ...IDLE_SNAPSHOT,
      failure: error === null ? null : { kind: 'leave', message: errorMessage(error, 'The server did not confirm that you left.'), retryable: false, cause: error },
    });
  }

  /** Retries after a failure: from `failed` runs the same join again; from a lost connection releases and re-joins. */
  async reconnect(): Promise<void> {
    const options = this.#lastOptions;
    if (options === null || this.#snapshot.phase !== 'failed') return;
    await this.#releaseMedia();
    await this.#leaveServer();
    await this.join(options);
  }

  /** After a failure (typically blocked microphone), joins again without publishing anything. */
  async joinListenOnly(): Promise<void> {
    if (this.#snapshot.phase !== 'failed') return;
    await this.#releaseMedia();
    await this.#leaveServer();
    await this.join({ camera: false, listenOnly: true });
  }

  /** Unmount cleanup: stops media and unregisters (best effort); no further notifications. */
  dispose(): void {
    if (this.#disposed) return;
    this.#generation += 1;
    const local = this.#snapshot.localStream;
    const publisher = this.#publisher;
    const subscribers = [...this.#subscribers.values()];
    this.#disposed = true;
    this.#publisher = null;
    this.#subscribers.clear();
    this.#listeners.clear();
    this.#token = null;
    stopStream(local);
    void publisher?.stop();
    for (const subscriber of subscribers) void subscriber.stop();
    void this.#leaveServer();
  }
}
