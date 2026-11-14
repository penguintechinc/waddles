/**
 * Microphone / camera access: getUserMedia with permission handling, and device enumeration for the pickers.
 * Everything goes through `DeviceDeps` so tests (and non-browser runs) can supply fakes.
 */

/** Why local media could not be opened. */
export type DeviceErrorKind = 'permission-denied' | 'no-device' | 'in-use' | 'insecure-context' | 'unsupported' | 'unknown';

/** A local-media failure with a user-presentable message. */
export class DeviceError extends Error {
  readonly kind: DeviceErrorKind;

  constructor(kind: DeviceErrorKind, message: string) {
    super(message);
    this.name = 'DeviceError';
    this.kind = kind;
  }
}

/** Seams for the browser media APIs. */
export interface DeviceDeps {
  getUserMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  enumerateDevices: () => Promise<MediaDeviceInfo[]>;
}

/** Browser implementation; every call fails with a typed `DeviceError` when the API is unavailable. */
export function browserDevices(): DeviceDeps {
  const devices = (): MediaDevices => {
    if (typeof navigator === 'undefined' || navigator.mediaDevices === undefined) {
      throw new DeviceError(
        typeof window !== 'undefined' && window.isSecureContext === false ? 'insecure-context' : 'unsupported',
        'Camera and microphone access needs a secure (HTTPS) page in a browser that supports it.',
      );
    }
    return navigator.mediaDevices;
  };
  return {
    getUserMedia: async (constraints) => devices().getUserMedia(constraints),
    enumerateDevices: async () => devices().enumerateDevices(),
  };
}

/** Maps a getUserMedia rejection onto a typed, presentable `DeviceError`. */
export function classifyDeviceError(error: unknown): DeviceError {
  if (error instanceof DeviceError) return error;
  const name = typeof error === 'object' && error !== null && 'name' in error ? String((error as { name: unknown }).name) : '';
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
    case 'PermissionDeniedError':
      return new DeviceError('permission-denied', 'Microphone or camera access was blocked. Allow it in your browser, or join listen-only.');
    case 'NotFoundError':
    case 'DevicesNotFoundError':
    case 'OverconstrainedError':
      return new DeviceError('no-device', 'No matching microphone or camera was found. Plug one in, pick another device, or join listen-only.');
    case 'NotReadableError':
    case 'TrackStartError':
    case 'AbortError':
      return new DeviceError('in-use', 'The microphone or camera is in use by another application.');
    default:
      return new DeviceError('unknown', 'Could not open the microphone or camera.');
  }
}

/** What to capture. A missing `*DeviceId` means "the browser default". */
export interface LocalMediaRequest {
  audioDeviceId?: string;
  videoDeviceId?: string;
  camera: boolean;
  /** Set false to open only the camera (device switch); defaults to true. */
  audio?: boolean;
}

/** Builds the getUserMedia constraints for a request (echo cancellation and noise suppression on). */
export function constraintsFor(request: LocalMediaRequest): MediaStreamConstraints {
  const audio: MediaTrackConstraints | false =
    request.audio === false ? false : { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  if (audio !== false && request.audioDeviceId !== undefined) audio.deviceId = { exact: request.audioDeviceId };
  if (!request.camera) return { audio, video: false };
  const video: MediaTrackConstraints = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } };
  if (request.videoDeviceId !== undefined) video.deviceId = { exact: request.videoDeviceId };
  return { audio, video };
}

/** Opens the microphone (and camera when requested); rejects with a `DeviceError`. */
export async function openLocalMedia(deps: DeviceDeps, request: LocalMediaRequest): Promise<MediaStream> {
  try {
    return await deps.getUserMedia(constraintsFor(request));
  } catch (error) {
    throw classifyDeviceError(error);
  }
}

/** Stops every track of a stream (releases the hardware / camera light). */
export function stopStream(stream: MediaStream | null | undefined): void {
  stream?.getTracks().forEach((track) => track.stop());
}

/** One selectable device. */
export interface DeviceOption {
  deviceId: string;
  label: string;
}

/** Microphones and cameras, with a numbered fallback label when the browser hides names before permission. */
export interface DeviceLists {
  microphones: DeviceOption[];
  cameras: DeviceOption[];
}

/** Enumerates devices; call after permission is granted so labels are populated. */
export async function listDevices(deps: DeviceDeps): Promise<DeviceLists> {
  const all = await deps.enumerateDevices();
  const pick = (kind: MediaDeviceKind, noun: string): DeviceOption[] =>
    all
      .filter((device) => device.kind === kind && device.deviceId !== '')
      .map((device, index) => ({ deviceId: device.deviceId, label: device.label.trim() !== '' ? device.label : `${noun} ${index + 1}` }));
  return { microphones: pick('audioinput', 'Microphone'), cameras: pick('videoinput', 'Camera') };
}
