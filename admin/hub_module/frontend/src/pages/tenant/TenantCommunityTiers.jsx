import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { tenantApi } from '../../services/api';
import { tenantCommunityTiersApi } from '../../services/tenantCommunityTiersApi';
import { TIER_BADGE_CLASS, TIER_LABEL, TIER_OPTIONS, normalizeTier, tierLabel } from '../../services/communityTierConstants';
import { classifyTierError } from '../../services/communityTierErrors';
import { ConfirmDialog, PoolMeter } from '../../components/tenant/CommunityTierParts';

const PAGE_SIZE = 50;
const ALLOCATION_PAGE = 200;

/**
 * Tenant-admin page: shows the tenant's per-tier team pool and lets the admin allocate
 * Free / Professional / Enterprise to each community. Server-side gates are authoritative; this is
 * a presentation layer over `/api/v1/tenant/<slug>/license/pool` and `.../communities/<uuid>/tier`.
 */
function TenantCommunityTiers() {
  const { tenantSlug } = useParams();
  const [communities, setCommunities] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pool, setPool] = useState(null);
  const [tierById, setTierById] = useState({});
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState(null);
  const [poolUnavailable, setPoolUnavailable] = useState(false);
  const [savingId, setSavingId] = useState(null);
  const [notice, setNotice] = useState(null);
  const [exhausted, setExhausted] = useState(null);
  const [pending, setPending] = useState(null);
  const requestSeq = useRef(0);

  const loadAllocations = useCallback(async () => {
    const map = {};
    let offset = 0;
    for (;;) {
      const res = await tenantCommunityTiersApi.listAllocations(tenantSlug, { limit: ALLOCATION_PAGE, offset });
      for (const a of res.allocations || []) map[a.communityId] = normalizeTier(a.tier);
      offset += ALLOCATION_PAGE;
      if (!res.meta || offset >= res.meta.total || (res.allocations || []).length === 0) break;
    }
    return map;
  }, [tenantSlug]);

  /** Reloads pool + allocations (never optimistic: usage always comes from the server). */
  const loadUsage = useCallback(async () => {
    const [poolRes, allocRes] = await Promise.allSettled([
      tenantCommunityTiersApi.getPool(tenantSlug),
      loadAllocations(),
    ]);
    let fatal = null;
    let unavailable = false;
    if (poolRes.status === 'fulfilled') setPool(poolRes.value);
    else {
      const c = classifyTierError(poolRes.reason);
      if (c.kind === 'unavailable') {
        unavailable = true;
        setPool(null);
      } else fatal = c;
    }
    if (allocRes.status === 'fulfilled') setTierById(allocRes.value);
    else if (!fatal) {
      const c = classifyTierError(allocRes.reason);
      if (c.kind === 'unavailable') unavailable = true;
      else fatal = c;
    }
    setPoolUnavailable(unavailable);
    return fatal;
  }, [tenantSlug, loadAllocations]);

  useEffect(() => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setPageError(null);
    (async () => {
      let fatal = null;
      try {
        const res = await tenantApi.getCommunities(tenantSlug, { page, limit: PAGE_SIZE });
        if (seq !== requestSeq.current) return;
        setCommunities(res.data.communities || []);
        setTotal(res.data.total ?? res.data.communities?.length ?? 0);
      } catch (err) {
        fatal = classifyTierError(err);
      }
      if (!fatal) fatal = await loadUsage();
      if (seq !== requestSeq.current) return;
      setPageError(fatal);
      setLoading(false);
    })();
  }, [tenantSlug, page, loadUsage]);

  const applyChange = async (community, to) => {
    setSavingId(community.publicId);
    setNotice(null);
    setExhausted(null);
    try {
      const res = await tenantCommunityTiersApi.setTier(tenantSlug, community.publicId, to);
      setNotice({
        type: 'success',
        text: res.changed
          ? `${community.name} is now ${tierLabel(res.tier)} (was ${tierLabel(res.previousTier)}).`
          : `${community.name} was already ${tierLabel(res.tier)}.`,
      });
    } catch (err) {
      const c = classifyTierError(err);
      if (c.kind === 'exhausted') {
        const d = c.details || {};
        setExhausted({
          tier: normalizeTier(d.requiredTier ?? to),
          limit: d.limit ?? null,
          used: d.used ?? null,
        });
      } else if (c.kind === 'unavailable') {
        setPoolUnavailable(true);
      } else if (c.kind === 'conflict') {
        setNotice({ type: 'error', text: 'This community was changed by someone else. The latest values have been reloaded.' });
      } else if (c.kind === 'forbidden') {
        setNotice({ type: 'error', text: 'You do not have permission to change community tiers.' });
      } else if (c.kind === 'disabled') {
        setNotice({ type: 'error', text: 'Community tier allocation is not enabled for this tenant.' });
      } else {
        setNotice({ type: 'error', text: c.message || 'Failed to change tier.' });
      }
    }
    await loadUsage();
    setSavingId(null);
  };

  const onSelect = (community, value) => {
    const to = Number(value);
    const from = tierById[community.publicId] ?? 1;
    if (to === from) return;
    if (to < from) setPending({ community, from, to });
    else applyChange(community, to);
  };

  const confirmPending = async () => {
    const p = pending;
    setPending(null);
    await applyChange(p.community, p.to);
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  let body;
  if (loading) {
    body = (
      <div className="flex items-center justify-center py-16" role="status" aria-label="Loading community tiers">
        <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-b-2 border-gold-400" />
      </div>
    );
  } else if (pageError?.kind === 'disabled') {
    body = (
      <div className="card p-6 text-sky-200" data-testid="tiers-not-enabled">
        <h2 className="font-semibold text-sky-100 mb-1">Community tier allocation isn&apos;t enabled</h2>
        <p className="text-sm">This feature is not enabled for your tenant yet. Contact your platform administrator if you expected to see it.</p>
      </div>
    );
  } else if (pageError?.kind === 'forbidden') {
    body = (
      <div role="alert" className="card p-6 text-red-300" data-testid="tiers-forbidden">
        You do not have permission to manage community tiers for this tenant.
      </div>
    );
  } else if (pageError) {
    body = (
      <div role="alert" className="card p-6 text-red-300" data-testid="tiers-error">
        {pageError.message || 'Failed to load community tiers.'}
      </div>
    );
  } else {
    body = (
      <>
        {poolUnavailable && (
          <div role="alert" className="card p-4 border border-amber-500/40 text-amber-200" data-testid="pool-unavailable">
            Team licensing isn&apos;t available from the license server yet; contact your platform administrator.
          </div>
        )}

        {pool && (
          <section aria-label="License pool" className="space-y-3">
            <div className="flex items-center gap-3 flex-wrap">
              <h2 className="text-lg font-semibold text-sky-100">Team license pool</h2>
              {pool.stale && (
                <span className="badge border border-amber-500/40 text-amber-300" data-testid="pool-stale">
                  Stale{pool.observedAt ? `: last observed ${new Date(pool.observedAt).toLocaleString()}` : ''}
                </span>
              )}
              {!pool.stale && pool.observedAt && (
                <span className="text-xs text-navy-300">Observed {new Date(pool.observedAt).toLocaleString()}</span>
              )}
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              {(pool.tiers || []).map((row) => (
                <PoolMeter key={row.tier} row={row} />
              ))}
            </div>
          </section>
        )}

        {exhausted && (
          <div role="alert" className="card p-4 border border-gold-500/40 text-gold-200" data-testid="pool-exhausted">
            <p className="font-semibold">
              Your {TIER_LABEL[exhausted.tier]} pool is full
              {exhausted.used !== null && exhausted.limit !== null ? ` (${exhausted.used} of ${exhausted.limit})` : ''}.
            </p>
            <p className="text-sm">Release a slot or upgrade your license.</p>
          </div>
        )}

        <div aria-live="polite" data-testid="tiers-live">
          {notice && (
            <p
              role={notice.type === 'error' ? 'alert' : 'status'}
              className={`text-sm ${notice.type === 'error' ? 'text-red-300' : 'text-emerald-300'}`}
            >
              {notice.text}
            </p>
          )}
        </div>

        <section aria-label="Communities" className="card overflow-hidden">
          <div className="px-5 py-4 border-b border-navy-700 flex items-center justify-between">
            <h2 className="text-sky-100 font-semibold">Communities</h2>
            <span className="text-sky-400 text-sm">{total} total</span>
          </div>
          {communities.length === 0 ? (
            <div className="py-12 text-center text-sky-400">No communities found in this tenant.</div>
          ) : (
            <ul className="divide-y divide-navy-700">
              {communities.map((c) => {
                const tier = tierById[c.publicId] ?? 1;
                const saving = savingId === c.publicId;
                const selectId = `community-tier-${c.publicId}`;
                return (
                  <li key={c.publicId || c.id} className="px-5 py-4 flex flex-col sm:flex-row sm:items-center gap-3 justify-between">
                    <div className="min-w-0">
                      <p className="text-sky-100 font-medium truncate">{c.name}</p>
                      <span className={`badge border mt-1 ${TIER_BADGE_CLASS[tier]}`}>{TIER_LABEL[tier]}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <label htmlFor={selectId} className="text-sm text-sky-200">
                        Tier for {c.name}
                      </label>
                      <select
                        id={selectId}
                        className="input w-40"
                        value={tier}
                        disabled={saving || poolUnavailable || !c.publicId}
                        onChange={(e) => onSelect(c, e.target.value)}
                      >
                        {TIER_OPTIONS.map((t) => (
                          <option key={t} value={t}>
                            {TIER_LABEL[t]}
                          </option>
                        ))}
                      </select>
                      {saving && <span className="text-xs text-sky-400">Saving...</span>}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
          {totalPages > 1 && (
            <div className="flex items-center justify-between p-4 border-t border-navy-700">
              <button className="btn btn-secondary text-sm" disabled={page === 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                Previous
              </button>
              <span className="text-sm text-sky-400">
                Page {page} of {totalPages}
              </span>
              <button className="btn btn-secondary text-sm" disabled={page === totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>
                Next
              </button>
            </div>
          )}
        </section>
      </>
    );
  }

  return (
    <div className="p-4 sm:p-6 space-y-6">
      <div>
        <h1 className="text-sky-100 text-2xl font-bold">Community tiers</h1>
        <p className="text-sky-400 text-sm mt-1">Allocate Professional and Enterprise slots from your pool to communities.</p>
      </div>
      {body}
      <ConfirmDialog pending={pending} onCancel={() => setPending(null)} onConfirm={confirmPending} busy={savingId !== null} />
    </div>
  );
}

export default TenantCommunityTiers;
