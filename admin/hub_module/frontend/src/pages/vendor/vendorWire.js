/**
 * Wire adapters for the vendor marketplace analytics and discount-code endpoints.
 *
 * hub-api wraps every success body as `{ success: true, data: ... }` and each endpoint has its own
 * payload shape (`{ rows, total }`, `{ codes, summary }`, `{ discountCodes, total }`, a bare array).
 * These helpers pin those shapes in one place so pages never guess between "array or wrapper" and
 * never call `.map` on something that is not an array.
 */

/** True for a non-null, non-array object. */
const isObject = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);

/** Returns `value` when it is an array, otherwise an empty array. */
export const asArray = (value) => (Array.isArray(value) ? value : []);

/**
 * Returns the payload of a hub-api success envelope. A body that is not the envelope (for example an
 * HTML fallback page or `{ success: false, error }`) throws, so callers surface an error state
 * instead of treating the body as data.
 */
export function unwrapEnvelope(response) {
  const body = response?.data;
  if (!isObject(body) || body.success === false || !('data' in body)) {
    const reason = isObject(body) && typeof body.error === 'string' ? body.error : 'Unexpected response from the server';
    throw new Error(reason);
  }
  return body.data;
}

/**
 * Sales payload `{ period, installations: { total, active, churnRate }, revenue: { mtdCents } }`
 * flattened to what the stat cards read; `null` when the payload is not an object.
 */
export function normalizeSales(data) {
  if (!isObject(data)) return null;
  const installations = isObject(data.installations) ? data.installations : {};
  const revenue = isObject(data.revenue) ? data.revenue : {};
  return {
    totalInstallations: installations.total ?? 0,
    activeInstallations: installations.active ?? 0,
    churnRate: installations.churnRate ?? 0,
    revenueMtd: revenue.mtdCents ?? 0,
  };
}

/** Install series `[{ date, installs, uninstalls }]` with a short x-axis label per point. */
export function normalizeInstalls(data) {
  return asArray(data).map((point) => ({
    installs: point?.installs ?? 0,
    uninstalls: point?.uninstalls ?? 0,
    label: point?.date ? new Date(point.date).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '',
  }));
}

/** Discount performance payload `{ codes, summary }` to the rows the performance table reads. */
export function normalizeDiscountPerformance(data) {
  return asArray(isObject(data) ? data.codes : null).map((code) => {
    const maxUses = code.maxUses ?? null;
    const expired = Boolean(code.validUntil) && new Date(code.validUntil) < new Date();
    const exhausted = maxUses != null && (code.currentUses ?? 0) >= maxUses;
    return {
      id: code.id,
      code: code.code,
      redemptions: code.totalRedemptions ?? 0,
      revenueImpact: code.totalDiscountCents ?? 0,
      remainingUses: maxUses == null ? -1 : Math.max(0, maxUses - (code.currentUses ?? 0)),
      status: !code.isActive ? 'inactive' : expired ? 'expired' : exhausted ? 'maxed' : 'active',
    };
  });
}

/** Community drill-down payload `{ rows, total, page, limit }`; rows keep the API's camelCase keys. */
export function normalizeCommunities(data) {
  if (!isObject(data)) return { rows: [], total: 0 };
  return { rows: asArray(data.rows), total: Number.isFinite(data.total) ? data.total : 0 };
}

/** Maps one API discount code (camelCase DTO) to the snake_case row the discount-codes page renders. */
export function normalizeDiscountCode(code) {
  return {
    id: code.id,
    code: code.code,
    module_id: code.moduleId ?? null,
    discount_type: code.discountType,
    discount_value: code.discountValue,
    max_uses: code.maxUses ?? null,
    use_count: code.currentUses ?? 0,
    valid_from: code.validFrom ?? null,
    valid_until: code.validUntil ?? null,
    is_active: Boolean(code.isActive),
    description: code.description ?? null,
    created_at: code.createdAt ?? null,
  };
}

/** Paginated discount-code payload `{ discountCodes, page, limit, total }` as `{ codes, total }`. */
export function normalizeDiscountCodePage(data) {
  if (!isObject(data)) return { codes: [], total: 0 };
  return {
    codes: asArray(data.discountCodes).map(normalizeDiscountCode),
    total: Number.isFinite(data.total) ? data.total : 0,
  };
}

/** Module list body `{ success, modules: [...] }` (not enveloped under `data`). */
export function normalizeModules(body) {
  return asArray(isObject(body) ? body.modules : null);
}
