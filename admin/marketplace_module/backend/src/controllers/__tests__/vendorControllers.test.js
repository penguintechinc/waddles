import { beforeEach, describe, expect, it, jest } from '@jest/globals';
import { call } from '../../__tests__/helpers.js';

const vendor = Object.fromEntries(['getVendorProfile', 'getVendorDashboard', 'updateVendorProfile', 'getVendorAnalyticsOverview', 'createVendorProfile', 'getVendorModules', 'createVendorModule', 'updateVendorModule', 'submitModuleForReview', 'getVendorRequest', 'createVendorRequest'].map((n) => [n, jest.fn()]));
const analytics = Object.fromEntries(['getSalesMetrics', 'getInstallTimeSeries', 'getApiUsageMetrics', 'getDiscountCodePerformance', 'getCommunityDrilldown', 'exportAnalyticsCsv'].map((n) => [n, jest.fn()]));
const discount = Object.fromEntries(['getVendorDiscountCodes', 'createDiscountCode', 'updateDiscountCode', 'deleteDiscountCode', 'validateDiscountCode', 'redeemDiscountCode'].map((n) => [n, jest.fn()]));
jest.unstable_mockModule('../../services/vendorService.js', () => vendor);
jest.unstable_mockModule('../../services/vendorAnalyticsService.js', () => analytics);
jest.unstable_mockModule('../../services/discountCodeService.js', () => discount);

const vendorCtl = await import('../vendorController.js');
const analyticsCtl = await import('../vendorAnalyticsController.js');
const discountCtl = await import('../discountCodeController.js');

const reset = (...groups) => groups.forEach((g) => Object.values(g).forEach((f) => f.mockReset()));
const boom = () => Promise.reject(new Error('boom'));

describe('vendorController', () => {
  beforeEach(() => reset(vendor));

  it.each([
    ['getVendorProfile', 'getVendorProfile', { seller: 1 }, {}, { success: true, seller: { seller: 1 } }],
    ['getVendorDashboard', 'getVendorDashboard', { stats: 1 }, {}, { success: true, stats: 1 }],
    ['getVendorAnalyticsOverview', 'getVendorAnalyticsOverview', { a: 1 }, {}, { success: true, analytics: { a: 1 } }],
    ['getVendorRequest', 'getVendorRequest', { r: 1 }, {}, { success: true, request: { r: 1 } }],
  ])('%s scopes to the authenticated user', async (ctl, svc, value, req, expected) => {
    vendor[svc].mockResolvedValueOnce(value);
    const { res } = await call(vendorCtl[ctl], { ...req, user: { id: 42 } });
    expect(vendor[svc]).toHaveBeenCalledWith(42);
    expect(res.json).toHaveBeenCalledWith(expected);
    vendor[svc].mockImplementationOnce(boom);
    expect((await call(vendorCtl[ctl])).next).toHaveBeenCalledWith(expect.any(Error));
  });

  it('createVendorProfile / createVendorModule / createVendorRequest return 201', async () => {
    for (const [ctl, svc] of [['createVendorProfile', 'createVendorProfile'], ['createVendorModule', 'createVendorModule'], ['createVendorRequest', 'createVendorRequest']]) {
      vendor[svc].mockResolvedValueOnce({ id: 1 });
      const { res } = await call(vendorCtl[ctl], { body: { x: 1 }, user: { id: 7 } });
      expect(res.status).toHaveBeenCalledWith(201);
      expect(vendor[svc]).toHaveBeenCalledWith(7, { x: 1 });
      vendor[svc].mockImplementationOnce(boom);
      expect((await call(vendorCtl[ctl])).next).toHaveBeenCalled();
    }
  });

  describe('updateVendorProfile validation', () => {
    it.each([
      [{}, 'displayName is required'],
      [{ displayName: '   ' }, 'displayName is required'],
      [{ displayName: 5 }, 'displayName is required'],
      [{ displayName: 'ok', websiteUrl: 'javascript:alert(1)' }, 'websiteUrl must be a valid URL'],
      [{ displayName: 'ok', websiteUrl: 'ftp://x' }, 'websiteUrl must be a valid URL'],
      [{ displayName: 'ok', payoutMethod: 'crypto' }, 'payoutMethod must be one of'],
    ])('rejects %j', async (body, message) => {
      const { res } = await call(vendorCtl.updateVendorProfile, { body });
      expect(res.status).toHaveBeenCalledWith(400);
      expect(res.json.mock.calls[0][0].error.message).toContain(message);
      expect(vendor.updateVendorProfile).not.toHaveBeenCalled();
    });

    it('trims the name and updates', async () => {
      vendor.updateVendorProfile.mockResolvedValueOnce({ id: 1 });
      const { res } = await call(vendorCtl.updateVendorProfile, { body: { displayName: ' Name ', websiteUrl: 'https://x.io', payoutMethod: 'stripe' }, user: { id: 3 } });
      expect(vendor.updateVendorProfile).toHaveBeenCalledWith(3, { displayName: 'Name', description: undefined, websiteUrl: 'https://x.io', payoutMethod: 'stripe' });
      expect(res.json).toHaveBeenCalledWith({ success: true, seller: { id: 1 } });
      vendor.updateVendorProfile.mockImplementationOnce(boom);
      expect((await call(vendorCtl.updateVendorProfile, { body: { displayName: 'x' } })).next).toHaveBeenCalled();
    });
  });

  it('getVendorModules parses paging with defaults', async () => {
    vendor.getVendorModules.mockResolvedValue({ modules: [1], pagination: { p: 1 } });
    await call(vendorCtl.getVendorModules, { query: { page: '3', limit: '10' }, user: { id: 2 } });
    expect(vendor.getVendorModules).toHaveBeenLastCalledWith(2, { page: 3, limit: 10 });
    const { res } = await call(vendorCtl.getVendorModules);
    expect(vendor.getVendorModules).toHaveBeenLastCalledWith(1, { page: 1, limit: 25 });
    expect(res.json).toHaveBeenCalledWith({ success: true, modules: [1], pagination: { p: 1 } });
    vendor.getVendorModules.mockImplementationOnce(boom);
    expect((await call(vendorCtl.getVendorModules)).next).toHaveBeenCalled();
  });

  it('updateVendorModule and submitModuleForReview scope to the user', async () => {
    vendor.updateVendorModule.mockResolvedValueOnce({});
    await call(vendorCtl.updateVendorModule, { params: { id: '4' }, body: { name: 'n' }, user: { id: 8 } });
    expect(vendor.updateVendorModule).toHaveBeenCalledWith(8, 4, { name: 'n' });
    vendor.updateVendorModule.mockImplementationOnce(boom);
    expect((await call(vendorCtl.updateVendorModule, { params: { id: '4' } })).next).toHaveBeenCalled();
    vendor.submitModuleForReview.mockResolvedValueOnce({ submissionId: 5 });
    const { res } = await call(vendorCtl.submitModuleForReview, { params: { id: '4' }, body: { changesDescription: 'c' }, user: { id: 8 } });
    expect(vendor.submitModuleForReview).toHaveBeenCalledWith(8, 4, 'c');
    expect(res.json).toHaveBeenCalledWith({ success: true, submissionId: 5, message: 'Module submitted for review' });
    vendor.submitModuleForReview.mockImplementationOnce(boom);
    expect((await call(vendorCtl.submitModuleForReview, { params: { id: '4' } })).next).toHaveBeenCalled();
    expect(vendorCtl.default.getVendorDashboard).toBe(vendorCtl.getVendorDashboard);
  });
});

describe('vendorAnalyticsController', () => {
  beforeEach(() => reset(analytics));

  it('getSalesMetrics returns data, 404 without a profile, and forwards errors', async () => {
    analytics.getSalesMetrics.mockResolvedValueOnce({ a: 1 });
    expect((await call(analyticsCtl.getSalesMetrics, { query: { period: '7d' } })).res.json).toHaveBeenCalledWith({ success: true, data: { a: 1 } });
    expect(analytics.getSalesMetrics).toHaveBeenCalledWith(1, { period: '7d' });
    analytics.getSalesMetrics.mockResolvedValueOnce(null);
    expect((await call(analyticsCtl.getSalesMetrics)).res.status).toHaveBeenCalledWith(404);
    analytics.getSalesMetrics.mockImplementationOnce(boom);
    expect((await call(analyticsCtl.getSalesMetrics)).next).toHaveBeenCalled();
  });

  it.each([
    ['getInstallTimeSeries', 'getInstallTimeSeries', { period: '90d', granularity: 'week' }],
    ['getApiUsageMetrics', 'getApiUsageMetrics', { period: '90d' }],
  ])('%s forwards period options with defaults', async (ctl, svc, expected) => {
    analytics[svc].mockResolvedValue([1]);
    await call(analyticsCtl[ctl], { query: expected });
    expect(analytics[svc]).toHaveBeenLastCalledWith(1, expected);
    await call(analyticsCtl[ctl]);
    expect(analytics[svc].mock.calls[1][1].period).toBe('30d');
    analytics[svc].mockImplementationOnce(boom);
    expect((await call(analyticsCtl[ctl])).next).toHaveBeenCalled();
  });

  it('getDiscountCodePerformance returns data or forwards errors', async () => {
    analytics.getDiscountCodePerformance.mockResolvedValueOnce({ c: 1 });
    expect((await call(analyticsCtl.getDiscountCodePerformance)).res.json).toHaveBeenCalledWith({ success: true, data: { c: 1 } });
    analytics.getDiscountCodePerformance.mockImplementationOnce(boom);
    expect((await call(analyticsCtl.getDiscountCodePerformance)).next).toHaveBeenCalled();
  });

  describe('getCommunityDrilldown', () => {
    it.each([['abc'], ['0'], ['-2']])('rejects moduleId=%s', async (moduleId) => {
      const { res } = await call(analyticsCtl.getCommunityDrilldown, { query: { moduleId } });
      expect(res.status).toHaveBeenCalledWith(400);
      expect(analytics.getCommunityDrilldown).not.toHaveBeenCalled();
    });
    it('clamps paging and forwards the module filter', async () => {
      analytics.getCommunityDrilldown.mockResolvedValue({ rows: [] });
      await call(analyticsCtl.getCommunityDrilldown, { query: { moduleId: '3', page: '0', limit: '9999', sortBy: 'status' } });
      expect(analytics.getCommunityDrilldown).toHaveBeenLastCalledWith(1, { moduleId: 3, page: 1, limit: 100, sortBy: 'status' });
      await call(analyticsCtl.getCommunityDrilldown, { query: { page: 'x', limit: 'y' } });
      expect(analytics.getCommunityDrilldown).toHaveBeenLastCalledWith(1, { moduleId: null, page: 1, limit: 25, sortBy: 'installed_at' });
      analytics.getCommunityDrilldown.mockImplementationOnce(boom);
      expect((await call(analyticsCtl.getCommunityDrilldown)).next).toHaveBeenCalled();
    });
  });

  describe('exportCsv', () => {
    it('rejects unknown types', async () => {
      const { res } = await call(analyticsCtl.exportCsv, { query: { type: 'users' } });
      expect(res.status).toHaveBeenCalledWith(400);
      expect(analytics.exportAnalyticsCsv).not.toHaveBeenCalled();
    });
    it('sends a CSV attachment', async () => {
      analytics.exportAnalyticsCsv.mockResolvedValue({ csv: 'a,b', filename: 'f.csv' });
      const { res } = await call(analyticsCtl.exportCsv);
      expect(res.setHeader).toHaveBeenCalledWith('Content-Type', 'text/csv');
      expect(res.setHeader).toHaveBeenCalledWith('Content-Disposition', 'attachment; filename="f.csv"');
      expect(res.send).toHaveBeenCalledWith('a,b');
      expect(analytics.exportAnalyticsCsv).toHaveBeenCalledWith(1, { type: 'sales', period: '30d' });
      analytics.exportAnalyticsCsv.mockImplementationOnce(boom);
      expect((await call(analyticsCtl.exportCsv, { query: { type: 'installs' } })).next).toHaveBeenCalled();
    });
  });
});

describe('discountCodeController', () => {
  beforeEach(() => reset(discount));
  const valid = { code: 'A', discountType: 'percentage', discountValue: '10' };

  it('listDiscountCodes clamps paging and normalises status', async () => {
    discount.getVendorDiscountCodes.mockResolvedValue({ x: 1 });
    await call(discountCtl.listDiscountCodes, { query: { page: '0', limit: '500', status: 'bogus' } });
    expect(discount.getVendorDiscountCodes).toHaveBeenLastCalledWith(1, { page: 1, limit: 100, status: 'all' });
    const { res } = await call(discountCtl.listDiscountCodes, { query: { page: '2', limit: '5', status: 'active' } });
    expect(discount.getVendorDiscountCodes).toHaveBeenLastCalledWith(1, { page: 2, limit: 5, status: 'active' });
    expect(res.json).toHaveBeenCalledWith({ success: true, data: { x: 1 } });
    discount.getVendorDiscountCodes.mockImplementationOnce(boom);
    expect((await call(discountCtl.listDiscountCodes)).next).toHaveBeenCalled();
  });

  describe('createDiscountCode', () => {
    it.each([
      [{ ...valid, code: undefined }, 'code is required'],
      [{ ...valid, code: ' ' }, 'code is required'],
      [{ ...valid, discountType: undefined }, 'discountType is required'],
      [{ ...valid, discountValue: undefined }, 'discountValue is required'],
      [{ ...valid, discountValue: null }, 'discountValue is required'],
      [{ ...valid, discountValue: 'abc' }, 'positive number'],
      [{ ...valid, discountValue: '-5' }, 'positive number'],
      [{ ...valid, discountValue: '150' }, 'cannot exceed 100'],
    ])('rejects %#', async (body, message) => {
      const { res } = await call(discountCtl.createDiscountCode, { body });
      expect(res.status).toHaveBeenCalledWith(400);
      expect(res.json.mock.calls[0][0].error.message).toContain(message);
      expect(discount.createDiscountCode).not.toHaveBeenCalled();
    });

    it('coerces numbers and nulls optional fields', async () => {
      discount.createDiscountCode.mockResolvedValue({ id: 1 });
      const { res } = await call(discountCtl.createDiscountCode, { body: { ...valid, maxUses: '5', moduleId: 3, validFrom: 'a', validUntil: 'b', description: 'd' } });
      expect(discount.createDiscountCode).toHaveBeenCalledWith(1, { code: 'A', discountType: 'percentage', discountValue: 10, moduleId: 3, validFrom: 'a', validUntil: 'b', maxUses: 5, description: 'd' });
      expect(res.status).toHaveBeenCalledWith(201);
      await call(discountCtl.createDiscountCode, { body: valid });
      expect(discount.createDiscountCode.mock.calls[1][1]).toMatchObject({ moduleId: null, validFrom: null, validUntil: null, maxUses: null, description: null });
      discount.createDiscountCode.mockImplementationOnce(boom);
      expect((await call(discountCtl.createDiscountCode, { body: valid })).next).toHaveBeenCalled();
    });
  });

  describe('updateDiscountCode', () => {
    it('rejects a non-numeric id and a bad value', async () => {
      expect((await call(discountCtl.updateDiscountCode, { params: { id: 'x' } })).res.status).toHaveBeenCalledWith(400);
      expect((await call(discountCtl.updateDiscountCode, { params: { id: '1' }, body: { discountValue: '-1' } })).res.status).toHaveBeenCalledWith(400);
      expect(discount.updateDiscountCode).not.toHaveBeenCalled();
    });
    it('coerces discountValue and delegates with the owner id', async () => {
      discount.updateDiscountCode.mockResolvedValue({ id: 1 });
      await call(discountCtl.updateDiscountCode, { params: { id: '1' }, body: { discountValue: '7' }, user: { id: 9 } });
      expect(discount.updateDiscountCode).toHaveBeenCalledWith(9, 1, { discountValue: 7 });
      await call(discountCtl.updateDiscountCode, { params: { id: '1' }, body: { isActive: false } });
      expect(discount.updateDiscountCode).toHaveBeenLastCalledWith(1, 1, { isActive: false });
      discount.updateDiscountCode.mockImplementationOnce(boom);
      expect((await call(discountCtl.updateDiscountCode, { params: { id: '1' } })).next).toHaveBeenCalled();
    });
  });

  it('deleteDiscountCode validates the id, delegates and forwards errors', async () => {
    expect((await call(discountCtl.deleteDiscountCode, { params: { id: 'x' } })).res.status).toHaveBeenCalledWith(400);
    discount.deleteDiscountCode.mockResolvedValueOnce({ id: 1 });
    const { res } = await call(discountCtl.deleteDiscountCode, { params: { id: '1' }, user: { id: 9 } });
    expect(discount.deleteDiscountCode).toHaveBeenCalledWith(9, 1);
    expect(res.json).toHaveBeenCalledWith({ success: true, data: { discountCode: { id: 1 } } });
    discount.deleteDiscountCode.mockImplementationOnce(boom);
    expect((await call(discountCtl.deleteDiscountCode, { params: { id: '1' } })).next).toHaveBeenCalled();
  });

  it('validateCode requires a code and parses the module id', async () => {
    expect((await call(discountCtl.validateCode, { body: {} })).res.status).toHaveBeenCalledWith(400);
    expect((await call(discountCtl.validateCode, { body: { code: '  ' } })).res.status).toHaveBeenCalledWith(400);
    discount.validateDiscountCode.mockResolvedValue({ valid: true });
    await call(discountCtl.validateCode, { body: { code: 'A', moduleId: '4' } });
    expect(discount.validateDiscountCode).toHaveBeenLastCalledWith('A', 4);
    await call(discountCtl.validateCode, { body: { code: 'A' } });
    expect(discount.validateDiscountCode).toHaveBeenLastCalledWith('A', null);
    discount.validateDiscountCode.mockImplementationOnce(boom);
    expect((await call(discountCtl.validateCode, { body: { code: 'A' } })).next).toHaveBeenCalled();
  });

  describe('redeemCode', () => {
    const full = { codeId: '1', communityId: '2', subscriptionId: '3', originalPriceCents: '100' };
    it.each([
      ['codeId'], ['communityId'], ['subscriptionId'], ['originalPriceCents'],
    ])('requires %s', async (field) => {
      const { res } = await call(discountCtl.redeemCode, { body: { ...full, [field]: undefined } });
      expect(res.status).toHaveBeenCalledWith(400);
      expect(res.json.mock.calls[0][0].error.message).toContain(`${field} is required`);
    });
    it('rejects null and invalid prices', async () => {
      expect((await call(discountCtl.redeemCode, { body: { ...full, originalPriceCents: null } })).res.status).toHaveBeenCalledWith(400);
      expect((await call(discountCtl.redeemCode, { body: { ...full, originalPriceCents: '-1' } })).res.status).toHaveBeenCalledWith(400);
      expect((await call(discountCtl.redeemCode, { body: { ...full, originalPriceCents: 'abc' } })).res.status).toHaveBeenCalledWith(400);
    });
    it('parses and redeems', async () => {
      discount.redeemDiscountCode.mockResolvedValue({ ok: 1 });
      const { res } = await call(discountCtl.redeemCode, { body: full });
      expect(discount.redeemDiscountCode).toHaveBeenCalledWith(1, 2, 3, 100);
      expect(res.json).toHaveBeenCalledWith({ success: true, data: { ok: 1 } });
      discount.redeemDiscountCode.mockImplementationOnce(boom);
      expect((await call(discountCtl.redeemCode, { body: full })).next).toHaveBeenCalled();
    });
  });
});
