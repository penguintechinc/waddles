import { beforeEach, describe, expect, it, jest } from '@jest/globals';
import { call } from '../../__tests__/helpers.js';

const query = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, transaction: jest.fn(), default: { query } }));

const premium = Object.fromEntries(['getPricingConfig', 'getCurrentSeatCount', 'calculateMonthlyBill', 'getSubscriptionStatus', 'subscribeCommunityPremium', 'cancelCommunityPremium'].map((n) => [n, jest.fn()]));
jest.unstable_mockModule('../../services/premiumService.js', () => premium);

const vendorExec = { incrementRequestCount: jest.fn(), executeCommand: jest.fn() };
jest.unstable_mockModule('../../services/vendorExecutionService.js', () => vendorExec);

const admin = await import('../adminReviewController.js');
const subs = await import('../subscriptionController.js');
const premiumCtl = await import('../premiumController.js');
const router = await import('../routerIntegrationController.js');
const { logger } = await import('../../utils/logger.js');

const boom = () => Promise.reject(new Error('boom'));

beforeEach(() => {
  query.mockReset();
  Object.values(premium).forEach((f) => f.mockReset());
  Object.values(vendorExec).forEach((f) => f.mockReset());
  jest.spyOn(logger, 'audit').mockImplementation(() => {});
});

describe('adminReviewController', () => {
  describe('list endpoints', () => {
    it.each([
      ['getVendorRequests', 'requests', 'vrr.status'],
      ['getSubmissions', 'submissions', 'ms.status'],
    ])('%s paginates and filters by status', async (ctl, key, column) => {
      query.mockResolvedValueOnce({ rows: [{ id: 1 }] }).mockResolvedValueOnce({ rows: [{ total: '51' }] });
      const { res } = await call(admin[ctl], { query: { status: 'pending', page: '2', limit: '10' } });
      expect(query.mock.calls[0][0]).toContain(`${column} = $1`);
      expect(query.mock.calls[0][1]).toEqual(['pending', 10, 10]);
      expect(res.json).toHaveBeenCalledWith({ success: true, [key]: [{ id: 1 }], pagination: { page: 2, limit: 10, total: 51, totalPages: 6 } });
    });

    it.each([['getVendorRequests'], ['getSubmissions']])('%s works unfiltered with defaults and forwards errors', async (ctl) => {
      query.mockResolvedValueOnce({ rows: [] }).mockResolvedValueOnce({ rows: [{ total: '0' }] });
      await call(admin[ctl]);
      expect(query.mock.calls[0][1]).toEqual([25, 0]);
      query.mockImplementationOnce(boom);
      expect((await call(admin[ctl])).next).toHaveBeenCalledWith(expect.any(Error));
    });
  });

  describe('vendor requests', () => {
    it('approve upserts a verified seller for the requester', async () => {
      query.mockResolvedValueOnce({ rows: [{ user_id: 8 }] }).mockResolvedValueOnce({ rows: [] });
      const { res } = await call(admin.approveVendorRequest, { params: { id: '3' }, body: { notes: 'n' }, user: { id: 99 } });
      expect(query.mock.calls[0][1]).toEqual([99, 'n', '3']);
      expect(query.mock.calls[1][1]).toEqual([8]);
      expect(res.json).toHaveBeenCalledWith({ success: true, message: 'Vendor request approved' });
    });
    it('approve is a no-op upsert when the request is unknown or has no user', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await call(admin.approveVendorRequest, { params: { id: '3' } });
      query.mockResolvedValueOnce({ rows: [{ user_id: null }] });
      await call(admin.approveVendorRequest, { params: { id: '3' } });
      expect(query).toHaveBeenCalledTimes(2);
    });
    it('approve/reject forward errors', async () => {
      query.mockImplementationOnce(boom);
      expect((await call(admin.approveVendorRequest, { params: { id: '3' } })).next).toHaveBeenCalled();
      query.mockImplementationOnce(boom);
      expect((await call(admin.rejectVendorRequest, { params: { id: '3' } })).next).toHaveBeenCalled();
    });
    it('reject records the reviewer and reason', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      const { res } = await call(admin.rejectVendorRequest, { params: { id: '3' }, body: { reason: 'r', notes: 'n' }, user: { id: 99 } });
      expect(query.mock.calls[0][1]).toEqual(['r', 'n', 99, '3']);
      expect(res.json).toHaveBeenCalledWith({ success: true, message: 'Vendor request rejected' });
    });
  });

  describe('submissions', () => {
    it.each([['approveSubmission'], ['rejectSubmission']])('%s 404s for unknown submissions without mutating', async (ctl) => {
      query.mockResolvedValueOnce({ rows: [] });
      const { next } = await call(admin[ctl], { params: { id: '1' } });
      expect(next.mock.calls[0][0].statusCode).toBe(404);
      expect(query).toHaveBeenCalledTimes(1);
    });
    it('approve updates the submission and publishes the module', async () => {
      query.mockResolvedValueOnce({ rows: [{ module_id: 5 }] }).mockResolvedValue({ rows: [] });
      const { res } = await call(admin.approveSubmission, { params: { id: '1' }, body: { notes: 'ok' }, user: { id: 99 } });
      expect(query.mock.calls[1][1]).toEqual([99, 'ok', '1']);
      expect(query.mock.calls[2][1]).toEqual([99, 5]);
      expect(res.json).toHaveBeenCalledWith({ success: true, message: 'Submission approved' });
    });
    it('reject updates the submission and rejects the module', async () => {
      query.mockResolvedValueOnce({ rows: [{ module_id: 5 }] }).mockResolvedValue({ rows: [] });
      const { res } = await call(admin.rejectSubmission, { params: { id: '1' }, body: { reason: 'bad', notes: 'n' }, user: { id: 99 } });
      expect(query.mock.calls[2][1]).toEqual(['bad', 5]);
      expect(res.json).toHaveBeenCalledWith({ success: true });
    });
    it('forward errors', async () => {
      query.mockImplementation(boom);
      expect((await call(admin.approveSubmission, { params: { id: '1' } })).next).toHaveBeenCalled();
      expect((await call(admin.rejectSubmission, { params: { id: '1' } })).next).toHaveBeenCalled();
      query.mockReset();
    });
  });

  describe('settings', () => {
    it('lists settings as a map', async () => {
      query.mockResolvedValueOnce({ rows: [{ setting_key: 'a', setting_value: '1' }, { setting_key: 'b', setting_value: '2' }] });
      expect((await call(admin.getMarketplaceSettings)).res.json).toHaveBeenCalledWith({ success: true, settings: { a: '1', b: '2' } });
      query.mockImplementationOnce(boom);
      expect((await call(admin.getMarketplaceSettings)).next).toHaveBeenCalled();
    });
    it('upserts each setting with the updater id', async () => {
      query.mockResolvedValue({ rows: [] });
      const { res } = await call(admin.updateMarketplaceSettings, { body: { settings: { a: '1', b: '2' } }, user: { id: 99 } });
      expect(query).toHaveBeenCalledTimes(2);
      expect(query.mock.calls[1][1]).toEqual(['b', '2', 99]);
      expect(res.json).toHaveBeenCalledWith({ success: true, message: 'Settings updated' });
      query.mockReset();
      expect((await call(admin.updateMarketplaceSettings, { body: {} })).next).toHaveBeenCalled();
    });
  });
});

describe('subscriptionController', () => {
  it('getCommunitySubscriptions maps rows', async () => {
    query.mockResolvedValueOnce({ rows: [{ id: 1, module_id: 2, name: 'n', display_name: null, is_enabled: true, installed_at: new Date('2026-01-01T00:00:00Z'), updated_at: null }] });
    const { res } = await call(subs.getCommunitySubscriptions, { params: { communityId: '4' } });
    const body = res.json.mock.calls[0][0];
    expect(body.total).toBe(1);
    expect(body.subscriptions[0]).toMatchObject({ moduleId: 2, displayName: 'n', installedAt: '2026-01-01T00:00:00.000Z' });
    query.mockImplementationOnce(boom);
    expect((await call(subs.getCommunitySubscriptions, { params: { communityId: '4' } })).next).toHaveBeenCalled();
  });

  describe('subscribeModule', () => {
    const req = { params: { communityId: '4' }, body: { moduleId: 2 }, user: { id: 7, platformUserId: 'p' } };
    it('requires moduleId', async () => {
      expect((await call(subs.subscribeModule, { ...req, body: {} })).next.mock.calls[0][0].statusCode).toBe(400);
    });
    it('404s for unpublished/unknown modules', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      expect((await call(subs.subscribeModule, req)).next.mock.calls[0][0].statusCode).toBe(404);
    });
    it('409s when already installed', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 2, name: 'n' }] }).mockResolvedValueOnce({ rows: [{ id: 1 }] });
      expect((await call(subs.subscribeModule, req)).next.mock.calls[0][0].statusCode).toBe(409);
    });
    it('installs and returns 201', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 2, name: 'n' }] }).mockResolvedValueOnce({ rows: [] }).mockResolvedValueOnce({ rows: [{ id: 9, installed_at: new Date('2026-01-01T00:00:00Z') }] });
      const { res } = await call(subs.subscribeModule, req);
      expect(query.mock.calls[2][1]).toEqual([4, 2, 7]);
      expect(res.status).toHaveBeenCalledWith(201);
    });
    it('forwards errors', async () => {
      query.mockImplementationOnce(boom);
      expect((await call(subs.subscribeModule, req)).next).toHaveBeenCalledWith(expect.any(Error));
    });
  });

  describe('unsubscribeModule', () => {
    const req = { params: { communityId: '4', subscriptionId: '6' }, user: { id: 7, platformUserId: 'p' } };
    it('404s when the subscription is not in this community', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      expect((await call(subs.unsubscribeModule, req)).next.mock.calls[0][0].statusCode).toBe(404);
    });
    it('refuses to remove core modules', async () => {
      query.mockResolvedValueOnce({ rows: [{ name: 'core', is_core: true }] });
      expect((await call(subs.unsubscribeModule, req)).next.mock.calls[0][0].statusCode).toBe(400);
      expect(query).toHaveBeenCalledTimes(1);
    });
    it('deletes non-core subscriptions scoped to the community', async () => {
      query.mockResolvedValueOnce({ rows: [{ name: 'm', is_core: false }] }).mockResolvedValueOnce({ rows: [{ id: 6 }] });
      const { res } = await call(subs.unsubscribeModule, req);
      expect(query.mock.calls[1][1]).toEqual([6, 4]);
      expect(res.json).toHaveBeenCalledWith({ success: true, message: 'Module unsubscribed successfully' });
    });
    it('forwards errors', async () => {
      query.mockImplementationOnce(boom);
      expect((await call(subs.unsubscribeModule, req)).next).toHaveBeenCalled();
    });
  });

  describe('updateSubscription', () => {
    const base = { params: { communityId: '4', subscriptionId: '6' }, user: { id: 7, platformUserId: 'p' } };
    it('requires something to update', async () => {
      expect((await call(subs.updateSubscription, { ...base, body: {} })).next.mock.calls[0][0].statusCode).toBe(400);
    });
    it('updates config and enabled flag', async () => {
      query.mockResolvedValueOnce({ rows: [{ id: 6 }] });
      const { res } = await call(subs.updateSubscription, { ...base, body: { config: { a: 1 }, isEnabled: false } });
      expect(query.mock.calls[0][1]).toEqual([6, 4, '{"a":1}', false]);
      expect(res.json).toHaveBeenCalledWith({ success: true, message: 'Subscription updated successfully' });
    });
    it('supports a single field and 404s for unknown subscriptions', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      expect((await call(subs.updateSubscription, { ...base, body: { isEnabled: true } })).next.mock.calls[0][0].statusCode).toBe(404);
    });
    it('forwards errors', async () => {
      query.mockImplementationOnce(boom);
      expect((await call(subs.updateSubscription, { ...base, body: { isEnabled: true } })).next).toHaveBeenCalled();
      expect(subs.default.updateSubscription).toBe(subs.updateSubscription);
    });
  });
});

describe('premiumController', () => {
  it('getPricing returns config, plus a bill estimate when a community is given', async () => {
    premium.getPricingConfig.mockResolvedValue({ basePriceCents: 100, baseSeatLimit: 5, overagePriceCents: 10 });
    expect((await call(premiumCtl.getPricing)).res.json).toHaveBeenCalledWith({ success: true, pricing: { basePriceCents: 100, baseSeatLimit: 5, overagePriceCents: 10 } });
    premium.getCurrentSeatCount.mockResolvedValue(8);
    premium.calculateMonthlyBill.mockReturnValue(130);
    const { res } = await call(premiumCtl.getPricing, { query: { communityId: '3' } });
    expect(res.json.mock.calls[0][0].pricing).toMatchObject({ seatCount: 8, estimatedCents: 130 });
    premium.getPricingConfig.mockImplementationOnce(boom);
    expect((await call(premiumCtl.getPricing)).next).toHaveBeenCalled();
  });

  it('getSubscriptionStatus combines the subscription and seat count', async () => {
    premium.getSubscriptionStatus.mockResolvedValue({ s: 1 });
    premium.getCurrentSeatCount.mockResolvedValue(3);
    expect((await call(premiumCtl.getSubscriptionStatus, { params: { communityId: '3' }, communityId: 3 })).res.json).toHaveBeenCalledWith({ success: true, subscription: { s: 1 }, currentSeatCount: 3 });
    premium.getSubscriptionStatus.mockImplementationOnce(boom);
    expect((await call(premiumCtl.getSubscriptionStatus, { params: { communityId: '3' }, communityId: 3 })).next).toHaveBeenCalled();
  });

  it('subscribePremium defaults to stripe', async () => {
    premium.subscribeCommunityPremium.mockResolvedValue({ checkoutUrl: 'u' });
    const { res } = await call(premiumCtl.subscribePremium, { communityId: 3, body: { communityId: 3, successUrl: 's', cancelUrl: 'c' } });
    expect(premium.subscribeCommunityPremium).toHaveBeenCalledWith(3, 'stripe', 's', 'c');
    expect(res.json).toHaveBeenCalledWith({ success: true, checkoutUrl: 'u' });
    premium.subscribeCommunityPremium.mockImplementationOnce(boom);
    expect((await call(premiumCtl.subscribePremium, { communityId: 3, body: {} })).next).toHaveBeenCalled();
    // never re-parses the body: without the gate's normalized id it fails loudly
    const bypass = await call(premiumCtl.subscribePremium, { body: { communityId: '3abc' } });
    expect(bypass.next.mock.calls[0][0].statusCode).toBe(400);
  });

  it('cancelPremium defaults to cancel-at-period-end', async () => {
    premium.cancelCommunityPremium.mockResolvedValue({ cancelAtPeriodEnd: true });
    await call(premiumCtl.cancelPremium, { communityId: 3, body: { communityId: 3 } });
    expect(premium.cancelCommunityPremium).toHaveBeenCalledWith(3, false);
    premium.cancelCommunityPremium.mockImplementationOnce(boom);
    expect((await call(premiumCtl.cancelPremium, { communityId: 3, body: {} })).next).toHaveBeenCalled();
    expect((await call(premiumCtl.cancelPremium, { body: { communityId: 3 } })).next.mock.calls[0][0].statusCode).toBe(400);
  });
});

describe('routerIntegrationController', () => {
  it('getCommunityCommands returns only enabled marketplace commands for the community', async () => {
    query.mockResolvedValueOnce({ rows: [{ command: '!a' }] });
    const { res } = await call(router.getCommunityCommands, { params: { communityId: '4' } });
    expect(query.mock.calls[0][1]).toEqual([4]);
    expect(query.mock.calls[0][0]).toContain("marketplace:%");
    expect(res.json).toHaveBeenCalledWith({ success: true, commands: [{ command: '!a' }] });
    query.mockImplementationOnce(boom);
    expect((await call(router.getCommunityCommands, { params: { communityId: '4' } })).next).toHaveBeenCalled();
  });

  it('executeModuleCommand counts the request and proxies to the vendor', async () => {
    vendorExec.executeCommand.mockResolvedValue({ reply: 'x' });
    const { res } = await call(router.executeModuleCommand, { params: { moduleId: '9' }, body: { command: '!a' } });
    expect(vendorExec.incrementRequestCount).toHaveBeenCalledWith(9);
    expect(vendorExec.executeCommand).toHaveBeenCalledWith(9, { command: '!a' });
    expect(res.json).toHaveBeenCalledWith({ reply: 'x' });
    vendorExec.executeCommand.mockImplementationOnce(boom);
    expect((await call(router.executeModuleCommand, { params: { moduleId: '9' } })).next).toHaveBeenCalled();
  });
});
