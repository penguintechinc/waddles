import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { cx } from './cx';

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label' | 'children'> {
  /** Accessible name. Required: an icon alone is not a label. */
  label: string;
  icon: ReactNode;
  tone?: 'neutral' | 'danger';
}

/**
 * Square icon-only button with a mandatory accessible name and a 40px hit area.
 * The `label` prop is also used as the native tooltip so sighted mouse users get the same hint.
 */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, tone = 'neutral', className, type = 'button', ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      title={label}
      className={cx(
        'inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-control transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus',
        'disabled:cursor-not-allowed disabled:opacity-50',
        tone === 'danger'
          ? 'text-danger-fg hover:bg-danger/15'
          : 'text-fg-secondary hover:bg-surface-raised hover:text-fg',
        className,
      )}
      {...rest}
    >
      <span aria-hidden="true" className="h-5 w-5 [&>svg]:h-5 [&>svg]:w-5">
        {icon}
      </span>
    </button>
  );
});
