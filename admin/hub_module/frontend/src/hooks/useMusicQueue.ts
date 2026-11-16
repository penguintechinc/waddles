import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { fetchMusicQueue, type MusicQueueView } from '../services/musicQueueApi';

/** Auto-refresh cadence for the music page. */
export const MUSIC_POLL_MS = 15_000;

/** Query keys for the music page. */
export const musicKeys = {
  queue: (communityId: number) => ['community', communityId, 'music'] as const,
};

/** Polls the music snapshot every 15s while the tab is visible; paused when hidden. */
export function useMusicQueue(communityId: number): UseQueryResult<MusicQueueView> {
  return useQuery({
    queryKey: musicKeys.queue(communityId),
    queryFn: () => fetchMusicQueue(communityId),
    refetchInterval: () => (document.visibilityState === 'hidden' ? false : MUSIC_POLL_MS),
    refetchIntervalInBackground: false,
    retry: (count, error) => {
      const status = (error as { response?: { status?: number } }).response?.status;
      return status !== 404 && count < 2;
    },
  });
}
