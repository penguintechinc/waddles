import { forwardRef, useId, type InputHTMLAttributes, type ReactNode } from 'react';
import { cx } from './cx';

export interface SwitchFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'role'> {
  label: ReactNode;
  description?: ReactNode;
}

/**
 * On/off toggle built on a real checkbox (`role="switch"`), so keyboard, form
 * submission and label-click all behave natively. The track/thumb are decorative
 * siblings driven by the `peer` state; motion respects `prefers-reduced-motion`.
 */
export const SwitchField = forwardRef<HTMLInputElement, SwitchFieldProps>(function SwitchField(
  { label, description, id: idProp, className, ...rest },
  ref,
) {
  const generated = useId();
  const id = idProp ?? generated;
  const descId = `${id}-desc`;
  return (
    <div className={cx('flex items-start justify-between gap-4', className)}>
      <div>
        <label htmlFor={id} className="cursor-pointer text-sm font-medium text-fg">
          {label}
        </label>
        {description ? (
          <p id={descId} className="field-hint mt-0.5">
            {description}
          </p>
        ) : null}
      </div>
      <span className="relative inline-flex h-6 w-11 shrink-0">
        <input
          ref={ref}
          id={id}
          type="checkbox"
          role="switch"
          aria-describedby={description ? descId : undefined}
          className="peer absolute inset-0 z-10 h-full w-full cursor-pointer opacity-0 disabled:cursor-not-allowed"
          {...rest}
        />
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 rounded-full bg-line-strong transition-colors peer-checked:bg-interactive peer-disabled:opacity-50 peer-focus-visible:ring-2 peer-focus-visible:ring-focus peer-focus-visible:ring-offset-2"
        />
        <span
          aria-hidden="true"
          className="pointer-events-none absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform peer-checked:translate-x-5"
        />
      </span>
    </div>
  );
});
