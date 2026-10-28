import { getPlatformConfig } from '../../utils/platformConfig';
import type { LiveActivityEvent } from '../../services/communityActivityApi';

/** Formats an ISO timestamp as a local clock time; empty string when unparseable. */
function formatEventTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** One incoming chat message and, when the bot answered, its reply. */
export function LiveActivityRow({ event }: { event: LiveActivityEvent }) {
  const platform = getPlatformConfig(event.platform);
  return (
    <li className="space-y-2 rounded-lg border border-line bg-surface-raised px-4 py-3" data-testid="live-activity-row">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${platform.color}`}
          >
            <span aria-hidden="true">{platform.icon}</span>
            {platform.label}
          </span>
          <span className="truncate text-sm font-medium text-fg">{event.actor ?? 'Unknown user'}</span>
        </div>
        <time dateTime={event.occurredAt} className="shrink-0 text-xs text-fg-muted">
          {formatEventTime(event.occurredAt)}
        </time>
      </div>
      {event.messageIn ? <p className="border-l-2 border-line-strong pl-2 text-sm text-fg-secondary">{event.messageIn}</p> : null}
      {event.replyOut ? (
        <p className="border-l-2 border-gold-500/50 pl-2 text-sm text-gold-300">
          <span aria-hidden="true">🐧</span> Waddles: {event.replyOut}
        </p>
      ) : null}
    </li>
  );
}
