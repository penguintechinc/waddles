/**
 * Single UI source for community license tier codes, names and colors. Mirrors
 * docs/community-tier-allocation.md: the API speaks int codes only (1 Free, 2 Premium),
 * never names. Communities have only these two tiers (one Premium pool per tenant).
 */

export const TIER = { free: 1, premium: 2 } as const;

export type TierCode = 1 | 2;

export const TIER_LABEL: Record<number, string> = {
  1: 'Free',
  2: 'Premium',
};

/** Badge classes per tier (text + border + tint, so the tier never relies on color alone: the label is always shown). */
export const TIER_BADGE_CLASS: Record<number, string> = {
  1: 'bg-navy-700/40 text-navy-300 border-navy-600',
  2: 'bg-gold-500/20 text-gold-300 border-gold-500/40',
};

/** Tiers an admin can pick, in ascending order. */
export const TIER_OPTIONS: readonly TierCode[] = [1, 2];

/** Operator-override reason tokens accepted by the server (a closed set, never free text). */
export const REASON_CODES = [
  'support_request',
  'sales_agreement',
  'trial_extension',
  'billing_correction',
  'partner_program',
  'data_correction',
  'other',
] as const;

export type ReasonCode = (typeof REASON_CODES)[number];

export const REASON_LABEL: Record<ReasonCode, string> = {
  support_request: 'Support request',
  sales_agreement: 'Sales agreement',
  trial_extension: 'Trial extension',
  billing_correction: 'Billing correction',
  partner_program: 'Partner program',
  data_correction: 'Data correction',
  other: 'Other',
};

/** Coerces any wire value to a valid tier code; a missing or unknown value is Free (1). */
export function normalizeTier(value: unknown): TierCode {
  return value === 2 ? 2 : 1;
}

/** Human label for a tier code (unknown codes read as Free, matching the server's fail-closed rule). */
export function tierLabel(value: unknown): string {
  return TIER_LABEL[normalizeTier(value)] ?? 'Free';
}
