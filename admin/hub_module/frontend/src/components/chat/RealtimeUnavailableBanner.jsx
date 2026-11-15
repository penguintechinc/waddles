/**
 * Visible notice shown when realtime chat is off or its connection failed.
 * Keeps the degraded state explicit instead of silently showing stale chat.
 */
export const REALTIME_UNAVAILABLE_MESSAGE =
  'Live chat updates are unavailable; refresh to see new messages';

export default function RealtimeUnavailableBanner() {
  return (
    <div
      role="status"
      data-testid="realtime-unavailable-banner"
      className="bg-amber-900/30 border-b border-amber-800 px-6 py-3 text-amber-200 text-sm"
    >
      {REALTIME_UNAVAILABLE_MESSAGE}
    </div>
  );
}
