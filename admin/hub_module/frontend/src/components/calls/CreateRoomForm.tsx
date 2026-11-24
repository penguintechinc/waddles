import { useState, type FormEvent } from 'react';
import { Button, Card, CardBody, TextField } from '../ui';

export interface CreateRoomFormProps {
  submitting: boolean;
  onSubmit: (input: { roomName: string; maxParticipants?: number }) => void;
  onCancel: () => void;
}

/** Inline "new room" form: the name becomes the room's display label; capacity is optional. */
export function CreateRoomForm({ submitting, onSubmit, onCancel }: CreateRoomFormProps) {
  const [name, setName] = useState('');
  const [capacity, setCapacity] = useState('');

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const roomName = name.trim();
    if (roomName === '') return;
    const max = Number(capacity);
    onSubmit(capacity.trim() !== '' && Number.isInteger(max) ? { roomName, maxParticipants: max } : { roomName });
  };

  return (
    <Card as="section" aria-label="Create a room" className="mb-6">
      <CardBody>
        <form onSubmit={submit} className="grid gap-4 sm:grid-cols-[2fr_1fr_auto] sm:items-end">
          <TextField label="Room name" value={name} onChange={(e) => setName(e.target.value)} required maxLength={64} placeholder="Weekly community call" autoFocus />
          <TextField
            label="Max participants"
            type="number"
            min={2}
            max={1000}
            value={capacity}
            onChange={(e) => setCapacity(e.target.value)}
            hint="Optional, 2 to 1000"
          />
          <div className="flex gap-2">
            <Button type="submit" variant="primary" loading={submitting} disabled={name.trim() === ''}>
              Create room
            </Button>
            <Button onClick={onCancel}>Cancel</Button>
          </div>
        </form>
      </CardBody>
    </Card>
  );
}
