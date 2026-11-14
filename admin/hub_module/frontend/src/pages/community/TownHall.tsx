import { useParams } from 'react-router-dom';
import { VideoCameraIcon } from '@heroicons/react/24/outline';
import { Button, EmptyState, ErrorState, LoadingState, PageHeader } from '../../components/ui';
import { useAuthView } from '../../contexts/authView';
import { CallRoomView } from '../../components/calls/CallRoomView';
import { CallsActionError, CallsErrorPanel } from '../../components/calls/CallsErrors';
import { useEnsureTownHall, useTownHall } from '../../hooks/useCalls';
import type { SessionDeps } from '../../services/media/callSession';

/** The community's persistent default room: members meet here with audio and video. */
function TownHallRoom({ communityId, overrides }: { communityId: number; overrides: Pick<SessionDeps, 'devices' | 'media'> | undefined }) {
  const { isCommunityAdmin } = useAuthView();
  const townHall = useTownHall(communityId);
  const ensure = useEnsureTownHall(communityId);

  let body;
  if (townHall.isPending) {
    body = <LoadingState label="Loading Town Hall" />;
  } else if (townHall.isError && townHall.error.kind === 'not-found') {
    body = (
      <>
        {ensure.isError ? <CallsActionError error={ensure.error} onDismiss={() => ensure.reset()} /> : null}
        <EmptyState
          icon={<VideoCameraIcon />}
          title="The Town Hall has not been opened yet"
          description="Open it to give your community a place to meet with audio and video."
          testId="town-hall-not-opened"
          action={
            <Button variant="primary" loading={ensure.isPending} onClick={() => ensure.mutate(undefined)}>
              Open Town Hall
            </Button>
          }
        />
      </>
    );
  } else if (townHall.isError) {
    body = <CallsErrorPanel error={townHall.error} onRetry={() => void townHall.refetch()} />;
  } else {
    body = (
      <CallRoomView
        communityId={communityId}
        room={{ roomId: townHall.data.roomId, label: townHall.data.label }}
        moderator={isCommunityAdmin(communityId)}
        {...(overrides ? { overrides } : {})}
      />
    );
  }

  return (
    <div>
      <PageHeader title="Town Hall" description="Meet your community in one place, with audio and video." />
      {body}
    </div>
  );
}

/** Route wrapper: validates `:communityId` before any request is made. */
export default function TownHall({ overrides }: { overrides?: Pick<SessionDeps, 'devices' | 'media'> }) {
  const { communityId } = useParams<{ communityId: string }>();
  const id = Number(communityId);
  if (!Number.isInteger(id) || id <= 0) {
    return <ErrorState title="Unknown community" message="This Town Hall link is not valid." />;
  }
  return <TownHallRoom communityId={id} overrides={overrides} />;
}
