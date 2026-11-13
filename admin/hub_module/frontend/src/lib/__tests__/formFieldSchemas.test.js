/**
 * Regression tests: custom FormModalBuilder type-error messages survive zod 4
 * (react-libs passes `invalid_type_error`, which zod 4 ignores).
 */
import { describe, it, expect } from 'vitest';
import { buildTypeMessageSchema, withZod4TypeMessages } from '../formFieldSchemas';

const firstMessage = (schema, value) => schema.safeParse(value).error.issues[0].message;

describe('withZod4TypeMessages', () => {
  it('returns the custom message for a non-numeric number field', () => {
    const schema = buildTypeMessageSchema({ name: 'n', label: 'Max seats', type: 'number', required: true });
    expect(firstMessage(schema, 'abc')).toBe('Max seats must be a number');
  });

  it('returns the custom message for an unknown select option', () => {
    const schema = buildTypeMessageSchema({
      name: 's', label: 'Tier', type: 'select', options: [{ value: 'a' }, { value: 'b' }],
    });
    expect(firstMessage(schema, 'zzz')).toContain('Tier must be one of the available options');
  });

  it('returns the custom message for an unchecked required checkbox', () => {
    const schema = buildTypeMessageSchema({ name: 'c', label: 'Terms', type: 'checkbox', required: true });
    expect(firstMessage(schema, false)).toBe('Terms must be checked');
  });

  it('keeps min/max messages and accepts empty optional numbers', () => {
    const schema = buildTypeMessageSchema({ name: 'n', label: 'Age', type: 'number', min: 1, max: 9 });
    expect(schema.safeParse('').success).toBe(true);
    expect(schema.safeParse('5').success).toBe(true);
    expect(firstMessage(buildTypeMessageSchema({ name: 'n', label: 'Age', type: 'number', min: 1, required: true }), '0'))
      .toBe('Age must be at least 1');
  });

  it('leaves fields without type messages and custom schemas untouched', () => {
    const custom = { name: 'x', label: 'X', type: 'number', schema: {} };
    const text = { name: 't', label: 'T', type: 'text' };
    const [a, b] = withZod4TypeMessages([custom, text]);
    expect(a).toBe(custom);
    expect(b).toBe(text);
  });
});
