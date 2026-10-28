import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { cx } from './cx';
import { Spinner } from './Spinner';

export type ButtonVariant = 'primary' | 'secondary' | 'success' | 'danger' | 'outline' | 'ghost';
export type ButtonSize = 'sm' | 'md' | 'lg';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual weight. `primary` (gold) is reserved for the single main action in a view. */
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner, sets `aria-busy` and blocks activation while an action is in flight. */
  loading?: boolean;
  /** Decorative icon before the label; hidden from assistive tech. */
  leadingIcon?: ReactNode;
}

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: 'btn-primary',
  secondary: 'btn-secondary',
  success: 'btn-success',
  danger: 'btn-danger',
  outline: 'btn-outline',
  ghost: 'btn-ghost',
};

const SIZE_CLASS: Record<ButtonSize, string | undefined> = {
  sm: 'btn-sm',
  md: undefined,
  lg: 'btn-lg',
};

/**
 * Themed button over the shared `.btn` classes.
 * Defaults to `type="button"` so a stray button inside a form never submits it,
 * and exposes loading state to screen readers via `aria-busy`.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', loading = false, leadingIcon, disabled, className, children, type = 'button', ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled === true || loading}
      aria-busy={loading || undefined}
      className={cx('btn', VARIANT_CLASS[variant], SIZE_CLASS[size], className)}
      {...rest}
    >
      {loading ? <Spinner size="sm" label="Working" /> : leadingIcon ? <span aria-hidden="true">{leadingIcon}</span> : null}
      {children}
    </button>
  );
});
