import type { ReactNode } from 'react';
import { cx } from './cx';

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  /** Decorative illustration/icon; hidden from assistive tech. */
  icon?: ReactNode;
  /** The next step: a Button or link. An empty state without a way forward is a dead end. */
  action?: ReactNode;
  headingLevel?: 2 | 3 | 4;
  className?: string;
}

/** "Nothing here yet" block that explains why and offers the next action. */
export function EmptyState({ title, description, icon, action, headingLevel = 3, className }: EmptyStateProps) {
  const Heading = `h${headingLevel}` as const;
  return (
    <div
      data-testid="empty-state"
      className={cx('flex flex-col items-center justify-center px-6 py-14 text-center', className)}
    >
      {icon ? (
        <div
          aria-hidden="true"
          className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-surface-raised text-fg-secondary [&>svg]:h-6 [&>svg]:w-6"
        >
          {icon}
        </div>
      ) : null}
      <Heading className="text-base font-semibold text-fg">{title}</Heading>
      {description ? <p className="mt-1.5 max-w-md text-sm text-fg-secondary">{description}</p> : null}
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}
