import { useId, type ReactNode } from 'react';
import { cx } from './cx';

/** Props shared by every labelled form control. */
export interface FieldChromeProps {
  /** Visible label. Always provided: an unlabelled control is an accessibility defect. */
  label: ReactNode;
  /** Keeps the label for assistive tech but hides it visually (e.g. search boxes). */
  hideLabel?: boolean | undefined;
  hint?: ReactNode;
  /** Validation message. Sets `aria-invalid` and is announced via `aria-describedby`. */
  error?: ReactNode;
}

/** Ids that wire a control to its label, hint and error text. */
export interface FieldIds {
  id: string;
  hintId: string;
  errorId: string;
}

/** Derives stable field ids, honouring a caller-supplied `id`. */
export function useFieldIds(idProp: string | undefined): FieldIds {
  const generated = useId();
  const id = idProp ?? generated;
  return { id, hintId: `${id}-hint`, errorId: `${id}-error` };
}

/** Builds `aria-describedby` from whichever of hint/error are rendered. */
export function describedBy(ids: FieldIds, hint: ReactNode, error: ReactNode): string | undefined {
  const parts = [hint ? ids.hintId : null, error ? ids.errorId : null].filter(Boolean);
  return parts.length > 0 ? parts.join(' ') : undefined;
}

interface FieldShellProps extends FieldChromeProps {
  ids: FieldIds;
  required: boolean | undefined;
  className: string | undefined;
  children: ReactNode;
}

/** Label + control slot + hint + error. The one place field markup is defined. */
export function FieldShell({ ids, label, hideLabel, hint, error, required, className, children }: FieldShellProps) {
  return (
    <div className={className}>
      <label htmlFor={ids.id} className={cx('label', hideLabel && 'sr-only')}>
        {label}
        {required ? (
          <span aria-hidden="true" className="ml-0.5 text-danger-fg">
            *
          </span>
        ) : null}
      </label>
      {children}
      {hint ? (
        <p id={ids.hintId} className="field-hint">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={ids.errorId} className="field-error">
          {error}
        </p>
      ) : null}
    </div>
  );
}
