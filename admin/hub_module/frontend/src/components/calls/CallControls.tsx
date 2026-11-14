import { useEffect, useState } from 'react';
import { MicrophoneIcon, PhoneXMarkIcon, VideoCameraIcon, VideoCameraSlashIcon } from '@heroicons/react/24/outline';
import { Button, SelectField } from '../ui';
import { browserDevices, listDevices, type DeviceDeps, type DeviceLists } from '../../services/media/devices';
import type { CallSession, CallSnapshot } from '../../services/media/callSession';

export interface CallControlsProps {
  session: CallSession;
  snapshot: CallSnapshot;
  onLeave: () => void;
  leaving: boolean;
  /** Test seam; defaults to the browser's media devices. */
  devices?: DeviceDeps | undefined;
}

/** Microphone / camera pickers, populated once connected (device labels are only exposed after permission). */
function useDeviceLists(enabled: boolean, devices: DeviceDeps | undefined): { lists: DeviceLists; error: string | null } {
  const [lists, setLists] = useState<DeviceLists>({ microphones: [], cameras: [] });
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    listDevices(devices ?? browserDevices())
      .then((found) => {
        if (!cancelled) setLists(found);
      })
      .catch(() => {
        if (!cancelled) setError('Could not list your microphones and cameras.');
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, devices]);
  return { lists, error };
}

/** Self controls for a connected call: mute, camera, device selection and leave. */
export function CallControls({ session, snapshot, onLeave, leaving, devices }: CallControlsProps) {
  const sending = snapshot.publishing !== null;
  const { lists, error } = useDeviceLists(sending, devices);
  const video = snapshot.publishing?.video === true;

  return (
    <div className="flex flex-col gap-4" data-testid="call-controls">
      <div className="flex flex-wrap items-center gap-3">
        {sending ? (
          <Button
            variant={snapshot.micMuted ? 'danger' : 'secondary'}
            aria-pressed={snapshot.micMuted}
            leadingIcon={<MicrophoneIcon className="h-4 w-4" />}
            onClick={() => session.setMicMuted(!snapshot.micMuted)}
          >
            {snapshot.micMuted ? 'Unmute microphone' : 'Mute microphone'}
          </Button>
        ) : null}
        {video ? (
          <Button
            variant="secondary"
            aria-pressed={!snapshot.cameraOn}
            leadingIcon={snapshot.cameraOn ? <VideoCameraIcon className="h-4 w-4" /> : <VideoCameraSlashIcon className="h-4 w-4" />}
            onClick={() => session.setCameraEnabled(!snapshot.cameraOn)}
          >
            {snapshot.cameraOn ? 'Turn camera off' : 'Turn camera on'}
          </Button>
        ) : null}
        <Button variant="danger" loading={leaving} leadingIcon={<PhoneXMarkIcon className="h-4 w-4" />} onClick={onLeave}>
          Leave room
        </Button>
      </div>
      {sending ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <SelectField
            label="Microphone"
            disabled={lists.microphones.length === 0}
            onChange={(event) => void session.switchDevice('audio', event.target.value)}
            defaultValue=""
          >
            <option value="" disabled>
              {lists.microphones.length === 0 ? 'No microphones found' : 'Switch microphone'}
            </option>
            {lists.microphones.map((device) => (
              <option key={device.deviceId} value={device.deviceId}>
                {device.label}
              </option>
            ))}
          </SelectField>
          {video ? (
            <SelectField
              label="Camera"
              disabled={lists.cameras.length === 0}
              onChange={(event) => void session.switchDevice('video', event.target.value)}
              defaultValue=""
            >
              <option value="" disabled>
                {lists.cameras.length === 0 ? 'No cameras found' : 'Switch camera'}
              </option>
              {lists.cameras.map((device) => (
                <option key={device.deviceId} value={device.deviceId}>
                  {device.label}
                </option>
              ))}
            </SelectField>
          ) : null}
        </div>
      ) : null}
      {error ? <p className="text-sm text-danger-fg">{error}</p> : null}
    </div>
  );
}
