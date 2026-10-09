/**
 * Test stand-in for `@penguintechinc/react-libs`' `FormModalBuilder`.
 *
 * The package's built dist is a directory-import ESM bundle vitest/node cannot
 * resolve, so page tests substitute this controlled form. It honours the
 * pieces of the real contract the pages rely on: `isOpen`, `title`, `fields`
 * (text-like, textarea, checkbox + select, `defaultValue`, `showWhen`), async
 * `onSubmit`, `onClose`. Field values reset from `defaultValue`s each time it
 * opens.
 */
import { useEffect, useState } from 'react';

interface StubField {
  name: string;
  label: string;
  type: string;
  defaultValue?: string | number | boolean;
  options?: ReadonlyArray<{ value: string | number; label: string }>;
  showWhen?: (values: Record<string, unknown>) => boolean;
}

interface FormModalStubProps {
  isOpen: boolean;
  title: string;
  fields: ReadonlyArray<StubField>;
  onSubmit: (data: Record<string, unknown>) => Promise<void>;
  onClose: () => void;
  submitButtonText: string;
}

function initialValues(fields: ReadonlyArray<StubField>): Record<string, unknown> {
  return Object.fromEntries(fields.map((f) => [f.name, f.defaultValue ?? (f.type === 'checkbox' ? false : '')]));
}

/** Controlled form modal used in place of the real FormModalBuilder in tests. */
export function FormModalStub({ isOpen, title, fields, onSubmit, onClose, submitButtonText }: FormModalStubProps) {
  const [values, setValues] = useState<Record<string, unknown>>(() => initialValues(fields));

  useEffect(() => {
    if (isOpen) setValues(initialValues(fields));
    // Re-seed only when the modal opens; `fields` is often rebuilt every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  if (!isOpen) return null;

  const setValue = (name: string, value: unknown) => setValues((v) => ({ ...v, [name]: value }));

  return (
    <div role="dialog" aria-label={title}>
      <h2>{title}</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          // The page under test owns error display; a rejected submit is swallowed here.
          onSubmit(values).catch(() => undefined);
        }}
      >
        {fields.filter((f) => !f.showWhen || f.showWhen(values)).map((f) => (
          <div key={f.name}>
            <label htmlFor={`stub-${f.name}`}>{f.label}</label>
            {f.type === 'select' ? (
              <select
                id={`stub-${f.name}`}
                value={String(values[f.name] ?? '')}
                onChange={(e) => setValue(f.name, e.target.value)}
              >
                {f.options?.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            ) : f.type === 'checkbox' ? (
              <input
                id={`stub-${f.name}`}
                type="checkbox"
                checked={Boolean(values[f.name])}
                onChange={(e) => setValue(f.name, e.target.checked)}
              />
            ) : f.type === 'textarea' ? (
              <textarea
                id={`stub-${f.name}`}
                value={String(values[f.name] ?? '')}
                onChange={(e) => setValue(f.name, e.target.value)}
              />
            ) : (
              <input
                id={`stub-${f.name}`}
                type={f.type}
                value={String(values[f.name] ?? '')}
                onChange={(e) => setValue(f.name, e.target.value)}
              />
            )}
          </div>
        ))}
        <button type="submit">{submitButtonText}</button>
        <button type="button" onClick={onClose}>
          Cancel
        </button>
      </form>
    </div>
  );
}
