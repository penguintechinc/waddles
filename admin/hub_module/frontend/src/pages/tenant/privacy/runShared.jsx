import { Badge, Button } from '../../../components/ui';
import { RESULT_STATUS_LABEL } from '../../../services/dsarConstants';
import { resultTone } from './dsarFormat';

export const ACTIONS = [
  { value: 'export', label: 'Export personal data (access request)' },
  { value: 'erase', label: 'Erase / anonymize (erasure request)' },
  { value: 'do_not_sell', label: 'Record Do-Not-Sell opt-out' },
];

export const ACTION_LABEL = { export: 'Export', erase: 'Erasure', do_not_sell: 'Do-Not-Sell' };

export const STATUS_LABEL = RESULT_STATUS_LABEL;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Parses a comma/space separated list of user UUIDs (trimmed, lower-cased, de-duplicated).
 * Returns `{ ids }` or `{ error }`; numeric ids are rejected -- the API never uses them.
 */
export function parseUserIds(raw) {
  const tokens = raw.split(/[\s,]+/).filter(Boolean);
  if (tokens.length === 0) return { error: 'Enter at least one user ID (UUID).' };
  const ids = [];
  for (const t of tokens) {
    const id = t.toLowerCase();
    if (!UUID_RE.test(id)) return { error: 'User IDs must be UUIDs, e.g. 123e4567-e89b-12d3-a456-426614174000.' };
    ids.push(id);
  }
  return { ids: [...new Set(ids)] };
}

/** Saves an export payload as a JSON download without putting any data in a URL. */
export function downloadExport(result) {
  const blob = new Blob([JSON.stringify({ userId: result.userId, data: result.data, incomplete: result.incomplete }, null, 2)], {
    type: 'application/json',
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `waddles-dsar-${result.userId}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

/** Per-user outcome list for a finished run. */
export function RunResults({ run }) {
  return (
    <ul className="space-y-2">
      {run.results.map((r) => (
        <li key={r.userId} className="flex flex-wrap items-center gap-3" data-testid={`privacy-result-${r.userId}`}>
          <span>User {r.userId}</span>
          <Badge tone={resultTone(r.status)}>{STATUS_LABEL[r.status] || r.status}</Badge>
          {r.detail && <span className="text-sm text-fg-muted">{r.detail}</span>}
          {run.action === 'export' && r.data && (
            <>
              <span className="text-sm text-fg-muted">
                {Object.entries(r.data).map(([k, rows]) => `${k}: ${rows.length}`).join(', ') || 'no records'}
              </span>
              <Button size="sm" onClick={() => downloadExport(r)}>{`Download export for user ${r.userId}`}</Button>
            </>
          )}
          {r.incomplete && r.incomplete.length > 0 && <Badge tone="gold">{`${r.incomplete.length} source(s) incomplete`}</Badge>}
        </li>
      ))}
    </ul>
  );
}
