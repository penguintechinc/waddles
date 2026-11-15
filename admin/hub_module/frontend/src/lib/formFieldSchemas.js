/**
 * Zod 4 schemas for FormModalBuilder fields whose custom type-error messages
 * @penguintechinc/react-libs 1.3.4 passes via the zod 3 `invalid_type_error`
 * option, which zod 4 silently ignores. Remove once react-libs ships zod 4 params.
 */
import { z } from 'zod';

/**
 * Returns true when FormModalBuilder would build this field's schema with
 * `invalid_type_error` (number, select/radio with options, required checkbox).
 */
function needsTypeMessage(field) {
  if (field.schema || field.pattern) return false;
  if (field.type === 'number') return true;
  if ((field.type === 'select' || field.type === 'radio') && field.options?.length > 0) return true;
  return field.type === 'checkbox' && Boolean(field.required);
}

/**
 * Builds the zod 4 schema for one field, mirroring react-libs' own logic but
 * with the custom messages passed through the zod 4 `error` param. Optional
 * fields accept '' so the library's `.optional()` wrap keeps prior behavior.
 */
export function buildTypeMessageSchema(field) {
  const { label } = field;
  if (field.type === 'checkbox') {
    return z.literal(true, { error: `${label} must be checked` });
  }
  let schema;
  if (field.type === 'number') {
    schema = z.coerce.number({ error: `${label} must be a number` });
    if (field.min !== undefined) schema = schema.min(field.min, `${label} must be at least ${field.min}`);
    if (field.max !== undefined) schema = schema.max(field.max, `${label} must be at most ${field.max}`);
    return field.required ? schema : schema.or(z.literal(''));
  }
  const values = field.options.map((o) => String(o.value));
  const error = `${label} must be one of the available options`;
  // Optional selects also accept '' (cleared); keeping it in the enum keeps the custom message.
  if (!field.required) return z.enum(['', ...values], { error });
  return z.enum(values, { error }).refine((val) => val !== undefined && val !== null && val !== '', {
    message: `${label} is required`,
  });
}

/**
 * Attaches zod 4 schemas (with the custom messages) to the fields that need
 * them; all other fields are returned unchanged.
 */
export function withZod4TypeMessages(fields = []) {
  return fields.map((field) =>
    needsTypeMessage(field) ? { ...field, schema: buildTypeMessageSchema(field) } : field,
  );
}
