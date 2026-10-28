import { cx } from './cx';
import { Spinner } from './Spinner';

export interface LoadingStateProps {
  /** Visible + announced text. */
  label?: string;
  /** Fill the viewport (route-level Suspense fallback) instead of a block-level region. */
  fullPage?: boolean;
  className?: string;
}

/** Centered spinner with a visible caption; the standard "data is on its way" state. */
export function LoadingState({ label = 'Loading', fullPage = false, className }: LoadingStateProps) {
  return (
    <div
      data-testid="loading-state"
      className={cx(
        'flex flex-col items-center justify-center gap-3 text-fg-secondary',
        fullPage ? 'min-h-[60vh]' : 'py-16',
        className,
      )}
    >
      <Spinner size="lg" label={label} />
      <p aria-hidden="true" className="text-sm">
        {label}…
      </p>
    </div>
  );
}
