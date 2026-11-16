/** Device access: error classification for the permission UI, constraints, enumeration labels, stream cleanup. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  DeviceError,
  browserDevices,
  classifyDeviceError,
  constraintsFor,
  listDevices,
  openLocalMedia,
  stopStream,
} from '../devices';
import { FakeStream, FakeTrack, fakeDevices } from '../../../test/fakeRtc';

afterEach(() => vi.unstubAllGlobals());

describe('classifyDeviceError', () => {
  it.each([
    ['NotAllowedError', 'permission-denied'],
    ['SecurityError', 'permission-denied'],
    ['NotFoundError', 'no-device'],
    ['OverconstrainedError', 'no-device'],
    ['NotReadableError', 'in-use'],
    ['AbortError', 'in-use'],
    ['TypeError', 'unknown'],
  ])('maps %s to %s', (name, kind) => {
    expect(classifyDeviceError({ name })).toMatchObject({ kind });
  });

  it('passes a DeviceError through and handles non-objects', () => {
    const same = new DeviceError('in-use', 'busy');
    expect(classifyDeviceError(same)).toBe(same);
    expect(classifyDeviceError('weird')).toMatchObject({ kind: 'unknown' });
  });
});

describe('constraintsFor / openLocalMedia', () => {
  it('asks for audio only by default and exact device ids when picked', () => {
    expect(constraintsFor({ camera: false })).toMatchObject({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
    const picked = constraintsFor({ camera: true, audioDeviceId: 'mic-1', videoDeviceId: 'cam-1' });
    expect(picked.audio).toMatchObject({ deviceId: { exact: 'mic-1' } });
    expect(picked.video).toMatchObject({ deviceId: { exact: 'cam-1' } });
    expect(constraintsFor({ camera: true, audio: false }).audio).toBe(false);
  });

  it('opens media through the injected getUserMedia', async () => {
    const devices = fakeDevices();
    const stream = await openLocalMedia(devices, { camera: true });
    expect(stream.getTracks()).toHaveLength(2);
    expect(devices.getUserMedia).toHaveBeenCalledTimes(1);
  });

  it('rejects with a typed DeviceError', async () => {
    const devices = fakeDevices({ failWith: { name: 'NotAllowedError' } });
    await expect(openLocalMedia(devices, { camera: false })).rejects.toMatchObject({ name: 'DeviceError', kind: 'permission-denied' });
  });
});

describe('listDevices / stopStream', () => {
  it('lists microphones and cameras, with numbered labels before permission and without placeholder ids', async () => {
    const devices = fakeDevices({
      devices: [
        { kind: 'audioinput', deviceId: 'a1', label: 'USB Mic' },
        { kind: 'audioinput', deviceId: 'a2', label: '' },
        { kind: 'videoinput', deviceId: 'v1', label: 'Webcam' },
        { kind: 'audiooutput', deviceId: 'o1', label: 'Speakers' },
        { kind: 'audioinput', deviceId: '', label: 'hidden' },
      ],
    });
    expect(await listDevices(devices)).toEqual({
      microphones: [
        { deviceId: 'a1', label: 'USB Mic' },
        { deviceId: 'a2', label: 'Microphone 2' },
      ],
      cameras: [{ deviceId: 'v1', label: 'Webcam' }],
    });
  });

  it('stops every track and tolerates null', () => {
    const tracks = [new FakeTrack('audio'), new FakeTrack('video')];
    stopStream(new FakeStream(tracks) as unknown as MediaStream);
    expect(tracks.map((t) => t.readyState)).toEqual(['ended', 'ended']);
    expect(() => stopStream(null)).not.toThrow();
  });
});

describe('browserDevices', () => {
  it('fails with a typed error when the media API is unavailable (insecure context or unsupported browser)', async () => {
    vi.stubGlobal('navigator', {});
    await expect(browserDevices().getUserMedia({ audio: true })).rejects.toBeInstanceOf(DeviceError);
    vi.stubGlobal('isSecureContext', false);
    await expect(browserDevices().enumerateDevices()).rejects.toMatchObject({ kind: expect.stringMatching(/insecure-context|unsupported/) });
  });

  it('delegates to navigator.mediaDevices when present', async () => {
    const stream = new FakeStream() as unknown as MediaStream;
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn().mockResolvedValue(stream), enumerateDevices: vi.fn().mockResolvedValue([]) } });
    const deps = browserDevices();
    await expect(deps.getUserMedia({ audio: true })).resolves.toBe(stream);
    await expect(deps.enumerateDevices()).resolves.toEqual([]);
  });
});
