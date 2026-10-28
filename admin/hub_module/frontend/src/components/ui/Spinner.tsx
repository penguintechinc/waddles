import { cx } from './cx';

export interface SpinnerProps {
  size?: 'sm' | 'md' | 'lg';
  /** Announced to screen readers; visually hidden. */
  label?: string;
  className?: string;
}

const SIZE_CLASS = { sm: 'h-4 w-4 border-2', md: 'h-6 w-6 border-2', lg: 'h-10 w-10 border-[3px]' } as const;

/**
 * Accessible loading indicator: a `role="status"` live region with a hidden label.
 * The ring rotates slowly under `prefers-reduced-motion` (see index.css) instead of stopping.
 */
export function Spinner({ size = 'md', label = 'Loading', className }: SpinnerProps) {
  return (
    <span role="status" className={cx('inline-flex items-center', className)}>
      <span
        aria-hidden="true"
        className={cx('animate-spin rounded-full border-line-strong border-t-accent', SIZE_CLASS[size])}
      />
      <span className="sr-only">{label}</span>
    </span>
  );
}
