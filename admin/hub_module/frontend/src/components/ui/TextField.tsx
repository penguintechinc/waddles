import { forwardRef, type InputHTMLAttributes } from 'react';
import { cx } from './cx';
import { describedBy, FieldShell, useFieldIds, type FieldChromeProps } from './fieldKit';

export interface TextFieldProps extends FieldChromeProps, InputHTMLAttributes<HTMLInputElement> {
  /** Class applied to the wrapping element (the control itself takes `inputClassName`). */
  wrapperClassName?: string;
  inputClassName?: string;
}

/** Labelled text-like input (text, email, number, password, search, ...). */
export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField(
  { label, hideLabel, hint, error, id: idProp, required, wrapperClassName, inputClassName, className, ...rest },
  ref,
) {
  const ids = useFieldIds(idProp);
  return (
    <FieldShell ids={ids} label={label} hideLabel={hideLabel} hint={hint} error={error} required={required} className={wrapperClassName ?? className}>
      <input
        ref={ref}
        id={ids.id}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(ids, hint, error)}
        className={cx('input', inputClassName)}
        {...rest}
      />
    </FieldShell>
  );
});
