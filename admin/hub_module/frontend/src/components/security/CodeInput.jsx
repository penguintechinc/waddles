/**
 * Labelled one-time-code input used by every MFA form (6-digit codes, or a
 * recovery code when `recovery` is set). Uses the platform's one-time-code
 * autofill hint so phones can fill emailed / SMS-style codes.
 */
export default function CodeInput({ id, label, value, onChange, recovery = false, disabled = false }) {
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium text-sky-200 mb-1">
        {label}
      </label>
      <input
        id={id}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        inputMode={recovery ? 'text' : 'numeric'}
        autoComplete="one-time-code"
        maxLength={recovery ? 16 : 6}
        placeholder={recovery ? 'XXXXX-XXXXX' : '123456'}
        className="w-full px-3 py-2 bg-navy-900 border border-navy-600 rounded-lg text-sky-100 font-mono tracking-widest focus:ring-2 focus:ring-sky-400"
      />
    </div>
  );
}
