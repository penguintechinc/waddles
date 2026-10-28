import { forwardRef, useId, type InputHTMLAttributes, type ReactNode } from 'react';
import { cx } from './cx';

export interface CheckboxFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label: ReactNode;
  hint?: ReactNode;
}

/** Checkbox whose whole label is clickable; hint is announced as the description. */
export const CheckboxField = forwardRef<HTMLInputElement, CheckboxFieldProps>(function CheckboxField(
  { label, hint, id: idProp, className, ...rest },
  ref,
) {
  const generated = useId();
  const id = idProp ?? generated;
  const hintId = `${id}-hint`;
  return (
    <div className={cx('flex items-start gap-3', className)}>
      <input
        ref={ref}
        id={id}
        type="checkbox"
        aria-describedby={hint ? hintId : undefined}
        className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded"
        {...rest}
      />
      <div>
        <label htmlFor={id} className="cursor-pointer text-sm font-medium text-fg">
          {label}
        </label>
        {hint ? (
          <p id={hintId} className="field-hint mt-0.5">
            {hint}
          </p>
        ) : null}
      </div>
    </div>
  );
});
