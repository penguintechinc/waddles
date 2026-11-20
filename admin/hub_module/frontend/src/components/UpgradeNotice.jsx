/**
 * Informative state for a feature the plan does not include (HTTP 402). Shows the server's own
 * message; the server gate is authoritative, this is only a UX hint.
 */
function UpgradeNotice({ message, feature = 'This feature' }) {
  return (
    <div className="text-center py-12" data-testid="upgrade-notice">
      <div className="card p-8 max-w-2xl mx-auto" role="status">
        <h2 className="text-2xl font-bold text-sky-100 mb-2">{feature} is not available on this plan</h2>
        <p className="text-navy-400">{message || 'Upgrade your plan to unlock this feature.'}</p>
      </div>
    </div>
  );
}

/** True when an axios error is the 402 "feature/plan not enabled" response. */
export function isUpgradeRequired(err) {
  return err?.response?.status === 402;
}

/** Server message from either `{error: {message}}` or `{error: 'text'}` bodies. */
export function upgradeMessage(err) {
  const e = err?.response?.data?.error;
  return typeof e === 'string' ? e : e?.message;
}

export default UpgradeNotice;
