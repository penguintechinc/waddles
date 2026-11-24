import { useState } from 'react';
import { Button, CheckboxField } from '../ui';
import type { JoinOptions } from '../../services/media/callSession';

export interface CallLobbyProps {
  title: string;
  onJoin: (options: JoinOptions) => void;
}

/** Pre-join choices: camera on/off, join with the microphone or listen only. The browser asks for permission on join. */
export function CallLobby({ title, onJoin }: CallLobbyProps) {
  const [camera, setCamera] = useState(false);
  return (
    <div className="mx-auto flex max-w-md flex-col gap-4 py-8" data-testid="call-lobby">
      <h2 className="section-title">{title}</h2>
      <p className="text-sm text-fg-secondary">
        Join with your microphone to talk, or listen only. Your browser will ask for permission to use your microphone
        {camera ? ' and camera' : ''}.
      </p>
      <CheckboxField label="Share my camera" checked={camera} onChange={(event) => setCamera(event.target.checked)} />
      <div className="flex flex-wrap gap-3">
        <Button variant="primary" onClick={() => onJoin({ camera, listenOnly: false })}>
          Join with microphone
        </Button>
        <Button onClick={() => onJoin({ camera: false, listenOnly: true })}>Listen only</Button>
      </div>
    </div>
  );
}
