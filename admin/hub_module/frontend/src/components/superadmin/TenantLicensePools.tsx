import { useCallback, useEffect, useId, useState } from 'react';
import type { FormEvent } from 'react';
import { operatorLicensePoolsApi } from '../../services/tenantLicenseApi';
import type { OperatorPool, OperatorPoolsResponse } from '../../services/tenantLicenseApi';
import { TIER_LABEL } from '../../services/communityTierConstants';
import { limitText } from '../../services/communityTierErrors';
import { isRecord } from '../../lib/wire';
import { NOT_ALLOWED_MESSAGE, draftToLimit } from './licensePoolDraft';
import type { Draft } from './licensePoolDraft';

type PoolTierCode = 2;
const TIERS: readonly PoolTierCode[] = [2];

type LoadState = 'loading' | 'ready' | 'forbidden' | 'error';

function toDraft(pool: OperatorPool | undefined): Draft {
  if (!pool || pool.limit === null) return { text: '', cleared: true };
  return { text: String(pool.limit), cleared: false };
}

function errInfo(err: unknown): { status?: number; code?: string; message?: string } {
  const res = isRecord(err) && isRecord(err['response']) ? err['response'] : {};
  const data = isRecord(res['data']) ? res['data'] : {};
  const error = data['error'];
  const out: { status?: number; code?: string; message?: string } = {};
  if (typeof res['status'] === 'number') out.status = res['status'];
  if (isRecord(error)) {
    if (typeof error['code'] === 'string') out.code = error['code'];
    if (typeof error['message'] === 'string') out.message = error['message'];
  }
  return out;
}

interface Props {
  tenantId: number;
  tenantSlug: string;
  onClose: () => void;
}

/**
 * Operator dialog for one tenant's Premium community pool (tier 2; one pool per tenant). Editing is only
 * possible on bypass-domain deployments; the server decides (`bypassDomain` in the GET and a 409
 * `POOL_OPERATOR_SET_NOT_ALLOWED` on PUT), the client never guesses the deployment mode.
 */
export default function TenantLicensePools({ tenantId, tenantSlug, onClose }: Props) {
  const titleId = useId();
  const [state, setState] = useState<LoadState>('loading');
  const [data, setData] = useState<OperatorPoolsResponse | null>(null);
  const [drafts, setDrafts] = useState<Record<number, Draft>>({});
  const [locked, setLocked] = useState(false);
  const [errors, setErrors] = useState<Record<number, string>>({});
  const [saving, setSaving] = useState<number | null>(null);
  const [saved, setSaved] = useState<number | null>(null);
  const [loadMessage, setLoadMessage] = useState('');

  const apply = useCallback((res: OperatorPoolsResponse) => {
    setData(res);
    setDrafts({
      2: toDraft(res.pools.find((p) => p.tierCode === 2)),
    });
    setLocked(!res.bypassDomain);
  }, []);

  const load = useCallback(async () => {
    setState('loading');
    try {
      apply(await operatorLicensePoolsApi.get(tenantId));
      setState('ready');
    } catch (err) {
      const info = errInfo(err);
      if (info.status === 403) setState('forbidden');
      else {
        setLoadMessage(info.message ?? 'Failed to load license pools.');
        setState('error');
      }
    }
  }, [tenantId, apply]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (e: FormEvent, tier: PoolTierCode) => {
    e.preventDefault();
    setSaved(null);
    const parsed = draftToLimit(drafts[tier] ?? { text: '', cleared: true });
    if ('error' in parsed) {
      setErrors((p) => ({ ...p, [tier]: parsed.error }));
      return;
    }
    setErrors((p) => ({ ...p, [tier]: '' }));
    setSaving(tier);
    try {
      const res = await operatorLicensePoolsApi.set(tenantId, tier, parsed.limit);
      apply(res);
      setSaved(tier);
    } catch (err) {
      const info = errInfo(err);
      if (info.status === 409 || info.code === 'POOL_OPERATOR_SET_NOT_ALLOWED') {
        setLocked(true);
      } else if (info.status === 403) {
        setErrors((p) => ({ ...p, [tier]: 'You do not have permission to change pool sizes.' }));
      } else {
        setErrors((p) => ({ ...p, [tier]: info.message ?? 'Failed to save the pool size.' }));
      }
    } finally {
      setSaving(null);
    }
  };

  const setDraft = (tier: PoolTierCode, d: Draft) => setDrafts((p) => ({ ...p, [tier]: d }));

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="bg-navy-900 rounded-xl shadow-xl max-w-lg w-full mx-4 border border-navy-700"
      >
        <div className="p-6 border-b border-navy-700">
          <h2 id={titleId} className="text-xl font-semibold text-sky-100">
            License pools: {tenantSlug}
          </h2>
        </div>
        <div className="p-6 space-y-4">
          {state === 'loading' && (
            <div role="status" aria-label="Loading license pools" className="text-center text-navy-300">
              Loading...
            </div>
          )}
          {state === 'forbidden' && (
            <div role="alert" data-testid="pools-forbidden" className="text-red-300">
              You do not have permission to manage license pools.
            </div>
          )}
          {state === 'error' && (
            <div role="alert" data-testid="pools-error" className="text-red-300">
              {loadMessage}{' '}
              <button type="button" className="underline" onClick={() => void load()}>
                Retry
              </button>
            </div>
          )}
          {state === 'ready' && data && (
            <>
              {locked && (
                <div role="status" data-testid="pools-locked" className="p-3 bg-amber-500/20 border border-amber-500/30 rounded-lg text-amber-200 text-sm">
                  {NOT_ALLOWED_MESSAGE}
                </div>
              )}
              {TIERS.map((tier) => {
                const label = TIER_LABEL[tier];
                const row = data.pools.find((p) => p.tierCode === tier);
                const d = drafts[tier] ?? { text: '', cleared: true };
                const inputId = `pool-limit-${tier}`;
                const err = errors[tier];
                return (
                  <form key={tier} onSubmit={(e) => void save(e, tier)} className="card p-3" aria-label={`${label} pool`}>
                    <div className="flex items-center justify-between mb-2">
                      <h3 className="font-semibold text-sky-100">{label}</h3>
                      <span className="text-xs text-navy-300" data-testid={`pool-current-${tier}`}>
                        Current: {limitText(row?.limit)}
                        {row ? ` (${row.source})` : ''}
                      </span>
                    </div>
                    <label htmlFor={inputId} className="block text-sm text-sky-100 mb-1">
                      {label} pool size
                    </label>
                    <div className="flex gap-2">
                      <input
                        id={inputId}
                        type="text"
                        inputMode="numeric"
                        value={d.cleared ? '' : d.text}
                        placeholder={d.cleared ? 'Not licensed' : ''}
                        onChange={(e) => setDraft(tier, { text: e.target.value, cleared: false })}
                        disabled={locked || saving !== null}
                        className="input flex-1 font-mono"
                        aria-invalid={err ? 'true' : undefined}
                        aria-describedby={err ? `${inputId}-error` : undefined}
                      />
                      <button
                        type="button"
                        className="btn btn-secondary text-sm"
                        disabled={locked || saving !== null}
                        onClick={() => setDraft(tier, { text: '-1', cleared: false })}
                      >
                        Unlimited
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary text-sm"
                        disabled={locked || saving !== null}
                        onClick={() => setDraft(tier, { text: '', cleared: true })}
                      >
                        Not licensed
                      </button>
                    </div>
                    {err && (
                      <p id={`${inputId}-error`} role="alert" className="text-sm text-red-300 mt-2">
                        {err}
                      </p>
                    )}
                    {saved === tier && !err && (
                      <p role="status" className="text-sm text-green-300 mt-2">
                        {label} pool saved.
                      </p>
                    )}
                    <div className="mt-3">
                      <button type="submit" className="btn btn-primary text-sm" disabled={locked || saving !== null}>
                        {saving === tier ? 'Saving...' : `Save ${label}`}
                      </button>
                    </div>
                  </form>
                );
              })}
              <p className="text-xs text-navy-400">
                Pool usage is shown on the tenant&apos;s Community tiers page.
              </p>
            </>
          )}
        </div>
        <div className="p-6 border-t border-navy-700 flex justify-end">
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
