import {
  forwardRef,
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';
import { cx } from './cx';

/** Props shared by every labelled form control. */
interface FieldChromeProps {
  /** Visible label. Always provided: an unlabelled control is an accessibility defect. */
  label: ReactNode;
  /** Keeps the label for assistive tech but hides it visually (e.g. search boxes). */
  hideLabel?: boolean | undefined;
  hint?: ReactNode;
  /** Validation message. Sets `aria-invalid` and is announced via `aria-describedby`. */
  error?: ReactNode;
}

/** Derives the stable ids that wire a control to its label, hint and error text. */
function useFieldIds(idProp: string | undefined) {
  const generated = useId();
  const id = idProp ?? generated;
  return { id, hintId: `${id}-hint`, errorId: `${id}-error` };
}

/** Builds `aria-describedby` from whichever of hint/error are rendered. */
function describedBy(ids: { hintId: string; errorId: string }, hint: ReactNode, error: ReactNode): string | undefined {
  const parts = [hint ? ids.hintId : null, error ? ids.errorId : null].filter(Boolean);
  return parts.length > 0 ? parts.join(' ') : undefined;
}

interface FieldShellProps extends FieldChromeProps {
  id: string;
  hintId: string;
  errorId: string;
  required: boolean | undefined;
  className: string | undefined;
  children: ReactNode;
}

/** Label + control slot + hint + error. The one place field markup is defined. */
function FieldShell({ id, hintId, errorId, label, hideLabel, hint, error, required, className, children }: FieldShellProps) {
  return (
    <div className={className}>
      <label htmlFor={id} className={cx('label', hideLabel && 'sr-only')}>
        {label}
        {required ? (
          <span aria-hidden="true" className="ml-0.5 text-danger-fg">
            *
          </span>
        ) : null}
      </label>
      {children}
      {hint ? (
        <p id={hintId} className="field-hint">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="field-error">
          {error}
        </p>
      ) : null}
    </div>
  );
}

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
    <FieldShell {...ids} label={label} hideLabel={hideLabel} hint={hint} error={error} required={required} className={wrapperClassName ?? className}>
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
    <FieldShell {...ids} label={label} hideLabel={hideLabel} hint={hint} error={error} required={required} className={wrapperClassName ?? className}>
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
    <FieldShell {...ids} label={label} hideLabel={hideLabel} hint={hint} error={error} required={required} className={wrapperClassName ?? className}>
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
