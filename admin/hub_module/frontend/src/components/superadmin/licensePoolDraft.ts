/** Pure helpers for the operator license-pool dialog (kept out of the component file for fast refresh). */

export const NOT_ALLOWED_MESSAGE =
  'Operator-managed pools are only available on operator (bypass-domain) deployments; this deployment gets pool sizes from the license server.';

/** One tier's editable value: free text, or the explicit 'Not licensed' choice. */
export interface Draft {
  text: string;
  cleared: boolean;
}

/** Parses a draft into the wire value, or an error message. null = not licensed, -1 = unlimited. */
export function draftToLimit(d: Draft): { limit: number | null } | { error: string } {
  if (d.cleared) return { limit: null };
  const t = d.text.trim();
  if (!/^-?\d+$/.test(t)) return { error: 'Enter a whole number (-1 for unlimited) or choose Not licensed.' };
  const n = Number.parseInt(t, 10);
  if (n < -1) return { error: 'The pool size must be -1 (unlimited) or greater.' };
  return { limit: n };
}

