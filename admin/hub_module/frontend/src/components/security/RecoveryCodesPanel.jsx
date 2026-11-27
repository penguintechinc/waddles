import { useState } from 'react';

/**
 * One-time display of freshly issued MFA recovery codes. The codes exist in the
 * browser only while this panel is mounted; the user must acknowledge saving
 * them before it closes. Nothing here is logged or persisted.
 */
export default function RecoveryCodesPanel({ codes, onDone }) {
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(codes.join('\n'));
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="p-4 bg-navy-900 rounded-lg border border-gold-500/40 space-y-4" data-testid="recovery-codes">
      <div>
        <h3 className="font-semibold text-gold-400">Save your recovery codes</h3>
        <p className="text-sm text-navy-300 mt-1">
          Each code works once if you lose access to your authenticator or email. They will not be shown again.
        </p>
      </div>
      <ul className="grid grid-cols-2 gap-2 font-mono text-sky-100" aria-label="Recovery codes">
        {codes.map((code) => (
          <li key={code} className="px-3 py-2 bg-navy-800 rounded border border-navy-700 text-center">
            {code}
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={handleCopy} className="btn btn-secondary text-sm">
          {copied ? 'Copied' : 'Copy codes'}
        </button>
        <label className="flex items-center gap-2 text-sm text-sky-200">
          <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} />
          I have saved these codes somewhere safe
        </label>
        <button type="button" onClick={onDone} disabled={!saved} className="btn btn-primary text-sm">
          Done
        </button>
      </div>
    </div>
  );
}
