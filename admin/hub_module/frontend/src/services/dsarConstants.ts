/**
 * Single UI source for DSAR inbox enum codes and error-code messages. Mirrors docs/dsar-inbox.md
 * and the RequestDTO / RequestEventDTO schemas in openapi/v1.yaml; the API speaks int codes only.
 */

export const REQUEST_TYPE = { export: 1, erase: 2, doNotSell: 3, consentWithdrawal: 4, correction: 5, other: 6 } as const;

export const REQUEST_TYPE_LABEL: Record<number, string> = {
  1: 'Access / export',
  2: 'Erasure',
  3: 'Do not sell or share',
  4: 'Consent withdrawal',
  5: 'Correction',
  6: 'Other',
};

export const STATUS = {
  received: 1,
  verificationPending: 2,
  verified: 3,
  inProgress: 4,
  completed: 5,
  rejected: 6,
  withdrawn: 7,
  expired: 8,
} as const;

export const STATUS_LABEL: Record<number, string> = {
  1: 'Received',
  2: 'Awaiting email verification',
  3: 'Verified',
  4: 'In progress',
  5: 'Completed',
  6: 'Rejected',
  7: 'Withdrawn',
  8: 'Expired',
};

/** Statuses 5-8 are terminal. */
export const isClosedStatus = (status: number): boolean => status >= 5 && status <= 8;

/** Statuses an admin may set via PATCH (2, 3 and 8 are system-driven). */
export const ADMIN_SETTABLE_STATUSES: readonly number[] = [4, 5, 6, 7];

export const REGIME = { gdpr: 1, ccpa: 2, other: 3 } as const;

export const REGIME_LABEL: Record<number, string> = { 1: 'GDPR', 2: 'CCPA / CPRA', 3: 'Other' };

export const SOURCE_LABEL: Record<number, string> = { 1: 'Signed-in user', 2: 'Public form', 3: 'Admin entered' };

export const EVENT_TYPE_LABEL: Record<number, string> = {
  1: 'Created',
  2: 'Verification sent',
  3: 'Verification send failed',
  4: 'Verified',
  5: 'Status changed',
  6: 'Assigned',
  7: 'Notes updated',
  8: 'Extended',
  9: 'Action executed',
  10: 'Expired',
  11: 'Contact data purged',
  12: 'Verification resent',
  13: 'Subject linked',
};

export const ACTOR_KIND_LABEL: Record<number, string> = { 1: 'System', 2: 'Admin', 3: 'Subject', 4: 'Public requester' };

export type DueState = 'ok' | 'due_soon' | 'overdue';
export const DUE_STATES: readonly DueState[] = ['ok', 'due_soon', 'overdue'];
export const DUE_STATE_LABEL: Record<DueState, string> = { ok: 'On track', due_soon: 'Due soon', overdue: 'Overdue' };

/** The `act` action matching a request type (1 export, 2 erase, 3 do_not_sell); others are manual. */
export const ACT_FOR_TYPE: Record<number, 'export' | 'erase' | 'do_not_sell' | undefined> = {
  1: 'export',
  2: 'erase',
  3: 'do_not_sell',
};

/**
 * Per-user `result.status` of an act call. `partial` (export only) means data came back but at
 * least one source failed: INCOMPLETE, never a success, and the request stays in progress.
 */
export const RESULT_STATUS = {
  completed: 'completed',
  alreadyDone: 'already_done',
  partial: 'partial',
  notFound: 'not_found',
  conflict: 'conflict',
  forbidden: 'forbidden',
  failed: 'failed',
  auditUnavailable: 'audit_unavailable',
} as const;

export const RESULT_STATUS_LABEL: Record<string, string> = {
  completed: 'Completed',
  already_done: 'Already done',
  partial: 'Incomplete',
  not_found: 'Not found',
  conflict: 'Conflict',
  forbidden: 'Forbidden',
  failed: 'Failed',
  audit_unavailable: 'Audit unavailable',
};

/** Codes that mean the stored request changed under the admin: reload, never retry blindly. */
export const DSAR_STALE_CODES: readonly string[] = ['DSAR_UNVERIFIED', 'DSAR_REQUEST_CLOSED', 'DSAR_ALREADY_EXTENDED', 'DSAR_CONFLICT', 'DSAR_SUBJECT_OVERRIDE_REQUIRED'];

export const DSAR_ERROR_MESSAGE: Record<string, string> = {
  DSAR_INBOX_DISABLED: 'The privacy request inbox is not enabled.',
  DSAR_UNVERIFIED: 'The requester has not verified their email yet, so this request cannot be actioned.',
  DSAR_REQUEST_CLOSED: 'This request is already closed.',
  DSAR_ALREADY_EXTENDED: 'This request has already been extended once.',
  DSAR_DUPLICATE_OPEN: 'You already have an open request of this type.',
  DSAR_RATE_LIMITED: 'Too many requests. Please wait a while and try again.',
  DSAR_CAPTCHA_FAILED: 'The verification challenge failed. Please try again.',
  DSAR_CAPTCHA_UNAVAILABLE: 'Verification is temporarily unavailable. Please try again later.',
  DSAR_UNAVAILABLE: 'This service is temporarily unavailable. Please try again later.',
  DSAR_ACTION_NOT_APPLICABLE: 'This request type has no automated action; handle it manually.',
  DSAR_NO_SUBJECT_USER: 'No user is linked to this request, so there is nothing to act on.',
  DSAR_CONFLICT:
    'Someone else changed or is acting on this request. It has been reloaded with the latest state; review it and try again if it is still needed.',
  DSAR_SUBJECT_LOCKED:
    'The linked user cannot be changed: this request was submitted by a signed-in user, so the account they used is fixed.',
  DSAR_SUBJECT_OVERRIDE_REQUIRED:
    'This request is already linked to a user. Changing the link needs a reason, which is recorded in the audit trail.',
  DSAR_SUBJECT_EMAIL_MISMATCH:
    'The requester\'s verified email does not match the linked account, so erasure is blocked. An administrator may override this with a recorded reason.',
  DSAR_VERIFICATION_INVALID: 'This verification link is invalid, expired or already used.',
};

/** Stable `error.code` from the `{ error: { code, message } }` envelope, or null. */
export function dsarErrorCode(err: unknown): string | null {
  const data = (err as { response?: { data?: { error?: { code?: unknown } } } })?.response?.data;
  const code = data?.error?.code;
  if (typeof code === 'string') return code;
  const direct = (err as { code?: unknown })?.code;
  return typeof direct === 'string' && direct.startsWith('DSAR_') ? direct : null;
}

/** HTTP status of an axios or PublicApiError, if any. */
export function dsarErrorStatus(err: unknown): number | undefined {
  const status = (err as { response?: { status?: unknown } })?.response?.status ?? (err as { status?: unknown })?.status;
  return typeof status === 'number' ? status : undefined;
}

/** Friendly message for a DSAR error: known code first, then server message, then fallback. */
export function dsarErrorMessage(err: unknown, fallback: string): string {
  const code = dsarErrorCode(err);
  if (code && DSAR_ERROR_MESSAGE[code]) return DSAR_ERROR_MESSAGE[code];
  const msg = (err as { response?: { data?: { error?: { message?: unknown } } } })?.response?.data?.error?.message;
  return typeof msg === 'string' ? msg : fallback;
}
