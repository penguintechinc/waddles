
/**
 * Registry of dangerous resource-access grant kinds, keyed by the consent-summary
 * key hub-api emits (`hub_api/services/bundle_access_grants.py`). A new kind
 * (e.g. `storageAccess`) adds one entry here -- its summary entries must carry
 * `reason` and be describable as a single line.
 */
export const ACCESS_GRANT_KINDS = {
  tableAccess: {
    title: 'PLATFORM TABLE ACCESS',
    blurb: 'This bundle asks to read/write Waddles platform tables it does not own.',
    describe: (e) =>
      `${e.table} -- columns: ${e.columns.join(', ')} -- ops: ${e.ops.join(', ')}`,
  },
};

/**
 * Bold-red warning listing every dangerous access grant a bundle requests
 * (table, columns, ops, reason). Renders nothing when the summary has none.
 * With `requireAck`, shows a checkbox and reports it through `onAckChange` so the
 * caller can keep its approve/activate button disabled until acknowledged.
 */
export default function AccessGrantWarning({ summary, requireAck = false, onAckChange }) {
  const blocks = Object.entries(ACCESS_GRANT_KINDS)
    .map(([key, kind]) => ({ key, kind, entries: (summary && summary[key]) || [] }))
    .filter((b) => b.entries.length > 0);
  if (blocks.length === 0) return null;

  return (
    <div
      role="alert"
      data-testid="access-grant-warning"
      className="border-2 border-red-500 bg-red-500/10 rounded p-3 mb-4"
    >
      <p className="text-red-500 font-bold uppercase" data-testid="access-grant-warning-title">
        DANGEROUS: this bundle requests access beyond its own data
      </p>
      {blocks.map(({ key, kind, entries }) => (
        <div key={key} className="mt-2" data-testid={`access-grant-${key}`}>
          <p className="text-red-500 font-bold">{kind.title}</p>
          <p className="text-red-400 text-sm">{kind.blurb}</p>
          <ul className="list-disc ml-5 mt-1">
            {entries.map((e, i) => (
              <li key={`${key}-${i}`} className="text-red-500 font-bold" data-testid="access-grant-entry">
                {kind.describe(e)}
                <span className="block text-red-400 font-normal text-sm">Reason: {e.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {requireAck && (
        <label className="flex items-center gap-2 mt-3 text-red-500 font-bold">
          <input
            type="checkbox"
            data-testid="access-grant-ack"
            onChange={(ev) => onAckChange && onAckChange(ev.target.checked)}
          />
          I understand and approve this access
        </label>
      )}
    </div>
  );
}
