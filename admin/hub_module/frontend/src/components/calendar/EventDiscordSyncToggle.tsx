import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useFeatureFlag } from '../../lib/useFeatureFlag';
import { eventSyncApi, type EventSyncState } from '../../services/eventSyncApi';
import { extractErrorMessage } from '../../services/roleSyncApi';

interface EventDiscordSyncToggleProps {
  communityId: string | number;
}

/**
 * Per-community "Event sync to Discord" switch. Rendered only when the
 * `waddles.calendar.event_discord_sync` flag is on (never crashes if the flag
 * server is unreachable -- the hook resolves OFF). Reads/writes the community's
 * event-sync state through TanStack Query with an optimistic update that rolls
 * back on failure; API errors (402 flag-off, 404 no pairing, 5xx) are shown to
 * the user verbatim from the error envelope, never swallowed.
 */
export default function EventDiscordSyncToggle({ communityId }: EventDiscordSyncToggleProps) {
  const flagOn = useFeatureFlag('waddles.calendar.event_discord_sync');
  const queryClient = useQueryClient();
  const key = ['event-sync', String(communityId)] as const;

  const query = useQuery({
    queryKey: key,
    queryFn: () => eventSyncApi.get(communityId),
    enabled: flagOn,
    retry: false,
  });

  const mutation = useMutation({
    mutationFn: (enabled: boolean) => eventSyncApi.set(communityId, enabled),
    onMutate: async (enabled) => {
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<EventSyncState>(key);
      if (previous) {
        queryClient.setQueryData<EventSyncState>(key, { ...previous, event_sync_enabled: enabled });
      }
      return { previous };
    },
    onError: (err, _enabled, context) => {
      if (context?.previous) queryClient.setQueryData(key, context.previous);
      console.error('[EventDiscordSyncToggle] UpdateFailed', {
        communityId: String(communityId),
        error: extractErrorMessage(err, 'unknown error'),
      });
    },
    onSuccess: (state) => queryClient.setQueryData(key, state),
    onSettled: () => queryClient.invalidateQueries({ queryKey: key }),
  });

  if (!flagOn) return null;

  const state = query.data;
  const noPairings = state !== undefined && state.pairings_total === 0;
  const error = query.error
    ? extractErrorMessage(query.error, 'Failed to load Discord event sync state')
    : mutation.error
      ? extractErrorMessage(mutation.error, 'Failed to update Discord event sync')
      : null;

  return (
    <div className="bg-navy-800 border border-navy-700 rounded p-4" data-testid="event-discord-sync">
      <label className="flex items-center gap-3 text-white">
        <input
          type="checkbox"
          aria-label="Event sync to Discord"
          checked={Boolean(state?.event_sync_enabled)}
          disabled={!state || mutation.isPending || noPairings}
          onChange={(e) => mutation.mutate(e.target.checked)}
          className="focus:ring-2 focus:ring-sky-500"
        />
        <span className="font-medium">Event sync to Discord</span>
        {query.isLoading && <span role="status" className="text-xs text-navy-300">Loading…</span>}
      </label>
      <p className="text-xs text-navy-300 mt-1">
        {noPairings
          ? 'Pair a Discord server first (Connections) to push events as Discord scheduled events.'
          : 'New and updated Waddles events are created as scheduled events in your paired Discord server(s). The bot needs the Manage Events permission.'}
      </p>
      {error && (
        <p className="text-xs text-red-300 mt-1" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
