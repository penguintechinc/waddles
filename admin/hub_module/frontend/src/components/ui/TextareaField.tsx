import { forwardRef, type TextareaHTMLAttributes } from 'react';
import { cx } from './cx';
import { describedBy, FieldShell, useFieldIds, type FieldChromeProps } from './fieldKit';

export interface TextareaFieldProps extends FieldChromeProps, TextareaHTMLAttributes<HTMLTextAreaElement> {
  wrapperClassName?: string;
  textareaClassName?: string;
}

/** Labelled multi-line input. */
export const TextareaField = forwardRef<HTMLTextAreaElement, TextareaFieldProps>(function TextareaField(
  { label, hideLabel, hint, error, id: idProp, required, wrapperClassName, textareaClassName, className, ...rest },
  ref,
) {
  const ids = useFieldIds(idProp);
  return (
    <FieldShell ids={ids} label={label} hideLabel={hideLabel} hint={hint} error={error} required={required} className={wrapperClassName ?? className}>
      <textarea
        ref={ref}
        id={ids.id}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(ids, hint, error)}
        className={cx('input', textareaClassName)}
        {...rest}
      />
    </FieldShell>
  );
});
