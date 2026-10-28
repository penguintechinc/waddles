import { LEADERBOARD_PERIODS, type LeaderboardPeriod } from '../../services/communityActivityApi';
import { cx } from '../ui';
import { periodLabel } from './leaderboardFormat';

export interface PeriodSelectorProps {
  value: LeaderboardPeriod;
  onChange: (period: LeaderboardPeriod) => void;
  /** Short labels ("Week", "All") for the dashboard widget. */
  compact?: boolean;
}

/** Weekly / monthly / all-time toggle; the pressed state is exposed to assistive tech. */
export function PeriodSelector({ value, onChange, compact = false }: PeriodSelectorProps) {
  return (
    <div role="group" aria-label="Leaderboard period" className="flex gap-1.5">
      {LEADERBOARD_PERIODS.map((period) => (
        <button
          key={period}
          type="button"
          aria-pressed={value === period}
          onClick={() => onChange(period)}
          className={cx(
            'rounded-md font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus',
            compact ? 'px-2 py-1 text-xs' : 'px-3 py-2 text-sm',
            value === period ? 'bg-accent text-fg-inverse' : 'bg-surface-raised text-fg-secondary hover:text-fg',
          )}
        >
          {periodLabel(period, compact)}
        </button>
      ))}
    </div>
  );
}
