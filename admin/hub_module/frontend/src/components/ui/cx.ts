/** A class-name fragment; falsy values are skipped so conditions can be inlined. */
export type ClassValue = string | false | null | undefined;

/**
 * Joins class-name fragments, dropping falsy ones.
 * Why: lets primitives compose variant + caller classes without template-string noise.
 */
export function cx(...values: ClassValue[]): string {
  return values.filter(Boolean).join(' ');
}
