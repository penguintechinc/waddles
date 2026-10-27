import { useEffect, useState } from 'react';
import { adminApi } from '../../services/api';
import { useFeatureFlag } from '../../lib/useFeatureFlag';

/**
 * Per-community "Event sync to Discord" switch. Rendered only when the
 * `waddles.calendar.event_discord_sync` flag is on; toggles `event_sync_enabled`
 * on the community's guild pairings via the authz'd hub-api admin endpoint, so
 * new Waddles events are pushed out as Discord scheduled events.
 */
function EventDiscordSyncToggle({ communityId }) {
  const flagOn = useFeatureFlag('waddles.calendar.event_discord_sync');
  const [state, setState] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!flagOn) return undefined;
    let cancelled = false;
    adminApi
      .getEventDiscordSync(communityId)
      .then((res) => { if (!cancelled) setState(res.data); })
      .catch((err) => {
        if (!cancelled) setError(err.response?.data?.error?.message || 'Failed to load Discord event sync state');
      });
    return () => { cancelled = true; };
  }, [flagOn, communityId]);

  if (!flagOn) return null;

  const noPairings = state && state.pairings_total === 0;

  const handleChange = async (event) => {
    const enabled = event.target.checked;
    setSaving(true);
    setError(null);
    try {
      const res = await adminApi.setEventDiscordSync(communityId, enabled);
      setState(res.data);
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to update Discord event sync');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="bg-navy-800 border border-navy-700 rounded p-4" data-testid="event-discord-sync">
      <label className="flex items-center gap-3 text-white">
        <input
          type="checkbox"
          aria-label="Event sync to Discord"
          checked={Boolean(state?.event_sync_enabled)}
          disabled={!state || saving || noPairings}
          onChange={handleChange}
        />
        <span className="font-medium">Event sync to Discord</span>
      </label>
      <p className="text-xs text-navy-300 mt-1">
        {noPairings
          ? 'Pair a Discord server first (Connections) to push events as Discord scheduled events.'
          : 'New and updated Waddles events are created as scheduled events in your paired Discord server(s). The bot needs the Manage Events permission.'}
      </p>
      {error && <p className="text-xs text-red-300 mt-1" role="alert">{error}</p>}
    </div>
  );
}

export default EventDiscordSyncToggle;
