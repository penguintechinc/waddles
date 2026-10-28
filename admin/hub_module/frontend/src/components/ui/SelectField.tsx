import { forwardRef, type SelectHTMLAttributes } from 'react';
import { cx } from './cx';
import { describedBy, FieldShell, useFieldIds, type FieldChromeProps } from './fieldKit';

export interface SelectFieldProps extends FieldChromeProps, SelectHTMLAttributes<HTMLSelectElement> {
  wrapperClassName?: string;
  selectClassName?: string;
}

/** Labelled native select. Children are `<option>` elements. */
export const SelectField = forwardRef<HTMLSelectElement, SelectFieldProps>(function SelectField(
  { label, hideLabel, hint, error, id: idProp, required, wrapperClassName, selectClassName, className, children, ...rest },
  ref,
) {
  const ids = useFieldIds(idProp);
  return (
    <FieldShell ids={ids} label={label} hideLabel={hideLabel} hint={hint} error={error} required={required} className={wrapperClassName ?? className}>
      <select
        ref={ref}
        id={ids.id}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(ids, hint, error)}
        className={cx('input', selectClassName)}
        {...rest}
      >
        {children}
      </select>
    </FieldShell>
  );
});
