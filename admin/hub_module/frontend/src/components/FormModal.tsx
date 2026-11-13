/**
 * Drop-in wrapper for react-libs FormModalBuilder that restores the custom
 * zod type-error messages lost under zod 4 (see lib/formFieldSchemas.js).
 */
import { useMemo } from 'react';
import {
  FormModalBuilder as LibFormModalBuilder,
  type FormField,
  type FormModalBuilderProps,
} from '@penguintechinc/react-libs';
import { withZod4TypeMessages } from '../lib/formFieldSchemas';

/**
 * Renders FormModalBuilder with its fields passed through withZod4TypeMessages.
 */
export function FormModalBuilder({ fields, ...props }: FormModalBuilderProps) {
  const patched = useMemo(() => withZod4TypeMessages(fields) as FormField[], [fields]);
  return <LibFormModalBuilder fields={patched} {...props} />;
}

export default FormModalBuilder;
