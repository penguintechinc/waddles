import { Alert } from '../ui';

/**
 * Honest state for the in-call view: joining registers presence and enables hands, but this build has
 * no browser media (WHIP/WHEP) client, so no audio or video is sent or received. Never claims otherwise.
 */
export function MediaJoinNotice() {
  return (
    <div data-testid="media-join-coming">
      <Alert tone="warning" title="Audio and video are not available yet">
        You are registered in this room and can raise your hand, but the browser media client is not part of this
        release yet, so no audio or video is sent or received. Media join is coming.
      </Alert>
    </div>
  );
}
