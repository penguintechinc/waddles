import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { LockClosedIcon, LockOpenIcon, PhoneIcon, PlusIcon, TrashIcon, UserGroupIcon } from '@heroicons/react/24/outline';
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, ErrorState, IconButton, LoadingState, PageHeader } from '../../components/ui';
import { CallsActionError, CallsErrorPanel } from '../../components/calls/CallsErrors';
import { CreateRoomForm } from '../../components/calls/CreateRoomForm';
import { RoomDetail } from '../../components/calls/RoomDetail';
import { useCallRooms, useCreateRoom, useDeleteRoom, useSetLocked } from '../../hooks/useCalls';
import { roomTitle, type CallRoom, type CallsApiError } from '../../services/callsApi';

/** People currently in a room: speakers (publishers) plus listeners (subscribers). */
function headcount(room: CallRoom): number {
  return room.publisherIds.length + room.subscriberCount;
}

/** Admin console for community call rooms: room list by label, plus the moderator pane for the selected room. */
function CommunityCalls({ communityId }: { communityId: number }) {
  const rooms = useCallRooms('admin', communityId);
  const create = useCreateRoom('admin', communityId);
  const remove = useDeleteRoom(communityId);
  const lock = useSetLocked(communityId);
  const [showCreate, setShowCreate] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<CallsApiError | null>(null);

  const selected = rooms.data?.find((room) => room.roomId === selectedId) ?? null;
  const fail = (error: CallsApiError) => {
    setNotice(null);
    setActionError(error);
  };
  const succeed = (text: string) => {
    setActionError(null);
    setNotice(text);
  };

  const onDelete = (room: CallRoom) => {
    if (!window.confirm(`Delete the room "${roomTitle(room)}"? Everyone in it is disconnected.`)) return;
    remove.mutate(room.roomId, {
      onSuccess: () => {
        if (selectedId === room.roomId) setSelectedId(null);
        succeed('Room deleted.');
      },
      onError: fail,
    });
  };

  const onToggleLock = (room: CallRoom) =>
    lock.mutate(
      { roomId: room.roomId, locked: !room.locked },
      { onSuccess: () => succeed(room.locked ? 'Room unlocked.' : 'Room locked.'), onError: fail },
    );

  const flagOff = rooms.isError && rooms.error.kind === 'feature-disabled';

  return (
    <div>
      <PageHeader
        title="Community Calls"
        description="Voice rooms for your community: who is in them and who wants to speak."
        actions={
          flagOff ? null : (
            <Button variant="primary" leadingIcon={<PlusIcon className="h-4 w-4" />} onClick={() => setShowCreate(true)} disabled={showCreate}>
              Create room
            </Button>
          )
        }
      />

      {actionError ? <CallsActionError error={actionError} onDismiss={() => setActionError(null)} /> : null}
      {notice ? (
        <Alert tone="success" className="mb-4" onDismiss={() => setNotice(null)}>
          {notice}
        </Alert>
      ) : null}

      {showCreate && !flagOff ? (
        <CreateRoomForm
          submitting={create.isPending}
          onCancel={() => setShowCreate(false)}
          onSubmit={(input) =>
            create.mutate(input, {
              onSuccess: (room) => {
                setShowCreate(false);
                setSelectedId(room.roomId);
                succeed('Room created.');
              },
              onError: fail,
            })
          }
        />
      ) : null}

      {rooms.isPending ? (
        <LoadingState label="Loading rooms" />
      ) : rooms.isError ? (
        <CallsErrorPanel error={rooms.error} onRetry={() => void rooms.refetch()} />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <Card as="section" aria-labelledby="rooms-title">
            <CardHeader title="Active rooms" headingLevel={2} />
            <CardBody>
              {rooms.data.length === 0 ? (
                <EmptyState
                  icon={<PhoneIcon />}
                  title="No active rooms"
                  description="Create a room to start a community call."
                  testId="calls-no-rooms"
                />
              ) : (
                <ul className="space-y-3">
                  {rooms.data.map((room) => (
                    <li
                      key={room.roomId}
                      className={`flex items-center justify-between gap-2 rounded-control bg-surface-raised p-3 ${selectedId === room.roomId ? 'ring-2 ring-focus' : ''}`}
                    >
                      <button
                        type="button"
                        className="min-w-0 flex-1 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                        aria-pressed={selectedId === room.roomId}
                        onClick={() => setSelectedId(room.roomId)}
                      >
                        <span className="block truncate font-medium text-fg">{roomTitle(room)}</span>
                        <span className="mt-1 flex items-center gap-1.5 text-sm text-fg-muted">
                          <UserGroupIcon aria-hidden="true" className="h-4 w-4" />
                          {headcount(room)} {headcount(room) === 1 ? 'participant' : 'participants'}
                          {room.locked ? (
                            <Badge tone="gold" className="ml-2">
                              Locked
                            </Badge>
                          ) : null}
                        </span>
                      </button>
                      <IconButton
                        label={room.locked ? `Unlock room ${roomTitle(room)}` : `Lock room ${roomTitle(room)}`}
                        icon={room.locked ? <LockOpenIcon /> : <LockClosedIcon />}
                        disabled={lock.isPending}
                        onClick={() => onToggleLock(room)}
                      />
                      <IconButton
                        label={`Delete room ${roomTitle(room)}`}
                        icon={<TrashIcon />}
                        tone="danger"
                        disabled={remove.isPending}
                        onClick={() => onDelete(room)}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </CardBody>
          </Card>

          <Card as="section" aria-label="Room details">
            <CardBody>
              {selected ? (
                <RoomDetail communityId={communityId} room={selected} onNotice={succeed} onActionError={fail} />
              ) : (
                <p className="py-8 text-center text-fg-muted">Select a room to see who is in it and who has a hand raised.</p>
              )}
            </CardBody>
          </Card>
        </div>
      )}
    </div>
  );
}

/** Route wrapper: validates `:communityId` before any request is made. */
export default function AdminCommunityCalls() {
  const { communityId } = useParams<{ communityId: string }>();
  const id = Number(communityId);
  if (!Number.isInteger(id) || id <= 0) {
    return <ErrorState title="Unknown community" message="This calls link is not valid." />;
  }
  return <CommunityCalls communityId={id} />;
}
