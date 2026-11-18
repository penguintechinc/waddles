/** Shared formatting helpers for the DSAR inbox views. */
import { DUE_STATE_LABEL, RESULT_STATUS } from '../../../services/dsarConstants';
import { isSuccessStatus } from '../../../services/tenantPrivacyApi';

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Locale date-time, or an em dash when absent. */
export function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

const DUE_TONE = { ok: 'green', due_soon: 'gold', overdue: 'red' };

/** Badge props for a server dueState; the label is always text, never colour alone. */
export function dueBadge(dueState) {
  if (!dueState) return null;
  return { tone: DUE_TONE[dueState] ?? 'gray', label: DUE_STATE_LABEL[dueState] ?? dueState };
}

/** Badge tone for a per-user result: green success, gold incomplete (partial), red failure. */
export function resultTone(status) {
  if (isSuccessStatus(status)) return 'green';
  return status === RESULT_STATUS.partial ? 'gold' : 'red';
}
