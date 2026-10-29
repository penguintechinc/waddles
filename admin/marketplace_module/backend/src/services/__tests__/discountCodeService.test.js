import { beforeEach, describe, expect, it, jest } from '@jest/globals';

const query = jest.fn();
const transaction = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, transaction, default: { query, transaction } }));

const svc = await import('../discountCodeService.js');
const { logger } = await import('../../utils/logger.js');

describe('discountCodeService', () => {
  beforeEach(() => {
    query.mockReset();
    transaction.mockReset();
    jest.spyOn(logger, 'info').mockImplementation(() => {});
  });

  describe('getVendorDiscountCodes', () => {
    it.each([
      ['all', false, false],
      ['active', true, false],
      ['expired', false, true],
    ])('status=%s', async (status, hasActive, hasExpired) => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [{ total: '4' }] });
      const out = await svc.getVendorDiscountCodes(9, { page: 2, limit: 5, status });
      expect(out).toEqual({ discountCodes: [{ id: 1 }], pagination: { page: 2, limit: 5, total: 4 } });
      expect(query.mock.calls[0][1]).toEqual([9, 5, 5]);
      expect(query.mock.calls[0][0].includes('dc.is_active = true')).toBe(hasActive);
      expect(query.mock.calls[0][0].includes('dc.is_active = false')).toBe(hasExpired);
    });
    it('uses defaults', async () => {
      query.mockResolvedValueOnce({ rows: [] }).mockResolvedValueOnce({ rows: [{ total: '0' }] });
      expect((await svc.getVendorDiscountCodes(9)).pagination).toEqual({ page: 1, limit: 20, total: 0 });
    });
  });

  describe('createDiscountCode', () => {
    const valid = { code: ' save10 ', discountType: 'percentage', discountValue: 10 };

    it.each([
      [{ ...valid, code: undefined }, 'code is required'],
      [{ ...valid, code: '   ' }, 'code is required'],
      [{ ...valid, code: 5 }, 'code is required'],
      [{ ...valid, discountType: 'bogus' }, 'discountType must be one of'],
      [{ ...valid, discountValue: undefined }, 'discountValue is required'],
      [{ ...valid, discountValue: null }, 'discountValue is required'],
      [{ ...valid, discountValue: '5' }, 'positive number'],
      [{ ...valid, discountValue: 0 }, 'positive number'],
      [{ ...valid, discountValue: 101 }, 'cannot exceed 100'],
    ])('rejects invalid input %#', async (data, message) => {
      await expect(svc.createDiscountCode(9, data)).rejects.toMatchObject({ statusCode: 400, message: expect.stringContaining(message) });
      expect(query).not.toHaveBeenCalled();
    });

    it('requires a vendor profile', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.createDiscountCode(9, valid)).rejects.toMatchObject({ statusCode: 404 });
    });

    it('uppercases and trims the code and scopes it to the seller', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 3 }] }).mockResolvedValueOnce({ rows: [{ id: 50 }] });
      expect(await svc.createDiscountCode(9, valid)).toEqual({ id: 50 });
      expect(query.mock.calls[1][1].slice(0, 5)).toEqual([3, null, 'SAVE10', 'percentage', 10]);
    });

    it('allows fixed discounts above 100 and maps duplicates to 409', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 3 }] }).mockRejectedValueOnce(Object.assign(new Error('d'), { code: '23505' }));
      await expect(svc.createDiscountCode(9, { code: 'a', discountType: 'fixed_cents', discountValue: 500 })).rejects.toMatchObject({ statusCode: 409 });
      query.mockResolvedValueOnce({ rows: [{ id: 3 }] }).mockRejectedValueOnce(new Error('db'));
      await expect(svc.createDiscountCode(9, valid)).rejects.toThrow('db');
    });
  });

  describe('ownership enforcement (update/delete)', () => {
    it('404s for unknown codes', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.updateDiscountCode(9, 1, { isActive: false })).rejects.toMatchObject({ statusCode: 404 });
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.deleteDiscountCode(9, 1)).rejects.toMatchObject({ statusCode: 404 });
    });

    it('403s when another vendor owns the code, without mutating', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [] });
      await expect(svc.updateDiscountCode(9, 1, { isActive: false })).rejects.toMatchObject({ statusCode: 403 });
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [] });
      await expect(svc.deleteDiscountCode(9, 1)).rejects.toMatchObject({ statusCode: 403 });
      expect(query).toHaveBeenCalledTimes(4);
    });
  });

  describe('updateDiscountCode', () => {
    const existing = { id: 1, discount_type: 'percentage', discount_value: 10 };
    const owned = () => query.mockResolvedValueOnce({ rows: [existing] }).mockResolvedValueOnce({ rows: [{ id: 1 }] });

    it('returns the existing row when nothing changes', async () => {
      owned();
      expect(await svc.updateDiscountCode(9, 1, {})).toBe(existing);
    });

    it('builds a SET clause from camelCase fields only', async () => {
      owned();
      query.mockResolvedValueOnce({ rows: [{ id: 1, updated: true }] });
      const out = await svc.updateDiscountCode(9, 1, { discountValue: 20, isActive: false, bogus: 1 });
      expect(out).toEqual({ id: 1, updated: true });
      const [sql, params] = query.mock.calls[2];
      expect(sql).toContain('discount_value = $1');
      expect(sql).toContain('is_active = $2');
      expect(sql).not.toContain('bogus');
      expect(params).toEqual([20, false, 1]);
    });

    it.each([
      [{ discountType: 'bogus' }, 'discountType must be one of'],
      [{ discountValue: 'x' }, 'positive number'],
      [{ discountValue: -1 }, 'positive number'],
      [{ discountValue: 150 }, 'cannot exceed 100'],
      [{ discountType: 'percentage', discountValue: 101 }, 'cannot exceed 100'],
    ])('validates updates %#', async (updates, message) => {
      owned();
      await expect(svc.updateDiscountCode(9, 1, updates)).rejects.toMatchObject({ message: expect.stringContaining(message) });
    });
  });

  it('deleteDiscountCode soft-deletes an owned code', async () => {
    query.mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [{ id: 1, is_active: false }] });
    expect(await svc.deleteDiscountCode(9, 1)).toEqual({ id: 1, is_active: false });
    expect(query.mock.calls[2][0]).toContain('is_active = false');
  });

  describe('validateDiscountCode', () => {
    const future = new Date(Date.now() + 86400000).toISOString();
    const past = new Date(Date.now() - 86400000).toISOString();
    const dc = (over = {}) => ({ id: 1, is_active: true, valid_from: null, valid_until: null, max_uses: null, current_uses: 0, module_id: null, discount_type: 'fixed_cents', discount_value: 5, seller_id: 2, ...over });
    const check = async (row, moduleId) => {
      query.mockResolvedValueOnce({ rows: row ? [row] : [] });
      return svc.validateDiscountCode(' abc ', moduleId);
    };

    it('requires a code', async () => {
      await expect(svc.validateDiscountCode('')).rejects.toMatchObject({ statusCode: 400 });
    });

    it.each([
      [null, undefined, 'CODE_NOT_FOUND'],
      [dc({ is_active: false }), undefined, 'CODE_INACTIVE'],
      [dc({ valid_from: future }), undefined, 'CODE_NOT_YET_VALID'],
      [dc({ valid_until: past }), undefined, 'CODE_EXPIRED'],
      [dc({ max_uses: 3, current_uses: 3 }), undefined, 'CODE_MAX_USES_REACHED'],
      [dc({ module_id: 7 }), 8, 'CODE_WRONG_MODULE'],
    ])('rejects: %#', async (row, moduleId, reason) => {
      expect(await check(row, moduleId)).toEqual({ valid: false, reason });
    });

    it('normalises the code before lookup', async () => {
      await check(null);
      expect(query.mock.calls[0][1]).toEqual(['ABC']);
    });

    it('accepts valid codes, reporting remaining uses', async () => {
      expect(await check(dc({ max_uses: 10, current_uses: 4, valid_from: past, valid_until: future, module_id: 7 }), 7)).toMatchObject({ valid: true, usesRemaining: 6, moduleId: 7 });
      expect((await check(dc())).usesRemaining).toBeNull();
      expect((await check(dc({ module_id: 7 }), null)).valid).toBe(true);
    });
  });

  describe('redeemDiscountCode', () => {
    it.each([
      [[0, 1, 1, 100]], [[1, 0, 1, 100]], [[1, 1, 0, 100]], [[1, 1, 1, undefined]],
    ])('requires all arguments %#', async (args) => {
      await expect(svc.redeemDiscountCode(...args)).rejects.toMatchObject({ statusCode: 400 });
    });
    it('rejects invalid prices', async () => {
      await expect(svc.redeemDiscountCode(1, 1, 1, -5)).rejects.toMatchObject({ statusCode: 400 });
      await expect(svc.redeemDiscountCode(1, 1, 1, '5')).rejects.toMatchObject({ statusCode: 400 });
    });

    const run = async (dcRow, price) => {
      const client = { query: jest.fn()
        .mockResolvedValueOnce({ rows: dcRow ? [dcRow] : [] })
        .mockResolvedValueOnce({ rows: [] })
        .mockResolvedValueOnce({ rows: [{ id: 'red' }] }) };
      transaction.mockImplementation(async (cb) => cb(client));
      return { out: await svc.redeemDiscountCode(1, 2, 3, price), client };
    };

    it('applies percentage discounts', async () => {
      const { out, client } = await run({ discount_type: 'percentage', discount_value: 25 }, 1000);
      expect(out).toEqual({ redemption: { id: 'red' }, originalPriceCents: 1000, discountedPriceCents: 750, savingsCents: 250 });
      expect(client.query.mock.calls[0][0]).toContain('FOR UPDATE');
    });

    it('applies fixed discounts and never goes below zero', async () => {
      expect((await run({ discount_type: 'fixed_cents', discount_value: 300 }, 1000)).out.discountedPriceCents).toBe(700);
      expect((await run({ discount_type: 'fixed_cents', discount_value: 5000 }, 1000)).out.discountedPriceCents).toBe(0);
    });

    it('409s when the code is exhausted/expired at redemption time', async () => {
      const client = { query: jest.fn().mockResolvedValueOnce({ rows: [] }) };
      transaction.mockImplementation(async (cb) => cb(client));
      await expect(svc.redeemDiscountCode(1, 2, 3, 100)).rejects.toMatchObject({ statusCode: 409 });
      expect(client.query).toHaveBeenCalledTimes(1);
    });
  });
});
