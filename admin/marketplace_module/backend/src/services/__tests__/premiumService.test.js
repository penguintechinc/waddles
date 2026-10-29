import { beforeEach, describe, expect, it, jest } from '@jest/globals';

const query = jest.fn();
const transaction = jest.fn();
jest.unstable_mockModule('../../config/database.js', () => ({ query, transaction, default: { query, transaction } }));

const stripe = { createCheckoutSession: jest.fn(), cancelSubscription: jest.fn() };
jest.unstable_mockModule('../stripeService.js', () => ({ default: stripe }));

const svc = await import('../premiumService.js');

describe('premiumService', () => {
  beforeEach(() => {
    query.mockReset();
    stripe.createCheckoutSession.mockReset();
    stripe.cancelSubscription.mockReset();
  });

  it('getPricingConfig parses settings and defaults missing keys to 0', async () => {
    query.mockResolvedValueOnce({ rows: [
      { setting_key: 'community_premium_base_price_cents', setting_value: '1000' },
      { setting_key: 'community_premium_base_seat_limit', setting_value: '50' },
    ] });
    expect(await svc.getPricingConfig()).toEqual({ basePriceCents: 1000, baseSeatLimit: 50, overagePriceCents: 0 });
  });

  it('getSubscriptionStatus returns the row or null', async () => {
    query.mockResolvedValueOnce({ rows: [{ id: 1 }] });
    expect(await svc.getSubscriptionStatus(1)).toEqual({ id: 1 });
    query.mockResolvedValueOnce({ rows: [] });
    expect(await svc.getSubscriptionStatus(1)).toBeNull();
  });

  it('getCurrentSeatCount parses the count', async () => {
    query.mockResolvedValueOnce({ rows: [{ count: '12' }] });
    expect(await svc.getCurrentSeatCount(1)).toBe(12);
  });

  it('calculateMonthlyBill only charges overage above the base seat limit', () => {
    expect(svc.calculateMonthlyBill(10, 1000, 50, 20)).toBe(1000);
    expect(svc.calculateMonthlyBill(50, 1000, 50, 20)).toBe(1000);
    expect(svc.calculateMonthlyBill(60, 1000, 50, 20)).toBe(1200);
  });

  describe('subscribeCommunityPremium', () => {
    const settings = { rows: [
      { setting_key: 'community_premium_base_price_cents', setting_value: '1000' },
      { setting_key: 'community_premium_base_seat_limit', setting_value: '50' },
      { setting_key: 'community_premium_overage_price_cents', setting_value: '10' },
    ] };

    it('creates a Stripe checkout for the computed total and records a trialing subscription', async () => {
      query.mockResolvedValueOnce(settings).mockResolvedValueOnce({ rows: [{ count: '60' }] }).mockResolvedValueOnce({ rows: [] });
      stripe.createCheckoutSession.mockResolvedValue({ url: 'u', sessionId: 's' });
      const out = await svc.subscribeCommunityPremium(4, 'stripe', 'ok', 'no');
      expect(stripe.createCheckoutSession.mock.calls[0][0].items[0].price).toBe(11);
      expect(stripe.createCheckoutSession.mock.calls[0][0].metadata).toEqual({ communityId: '4', type: 'community_premium' });
      expect(out).toEqual({ checkoutUrl: 'u', sessionId: 's', pricing: { basePriceCents: 1000, seatCount: 60, totalCents: 1100 } });
      expect(query.mock.calls[2][1]).toEqual([4, 1000, 10, 50, 60]);
    });

    it('defaults to stripe', async () => {
      query.mockResolvedValueOnce(settings).mockResolvedValueOnce({ rows: [{ count: '1' }] }).mockResolvedValueOnce({ rows: [] });
      stripe.createCheckoutSession.mockResolvedValue({ url: 'u', sessionId: 's' });
      await svc.subscribeCommunityPremium(4, undefined, 'ok', 'no');
      expect(stripe.createCheckoutSession).toHaveBeenCalled();
    });

    it('fails loudly for providers without checkout support', async () => {
      query.mockResolvedValueOnce(settings).mockResolvedValueOnce({ rows: [{ count: '1' }] }).mockResolvedValueOnce({ rows: [] });
      await expect(svc.subscribeCommunityPremium(4, 'paypal', 'ok', 'no')).rejects.toThrow(TypeError);
    });
  });

  describe('cancelCommunityPremium', () => {
    it('404s when there is no subscription', async () => {
      query.mockResolvedValueOnce({ rows: [] });
      await expect(svc.cancelCommunityPremium(1)).rejects.toMatchObject({ statusCode: 404 });
    });

    it('cancels at period end without touching is_premium', async () => {
      query.mockResolvedValueOnce({ rows: [{ stripe_subscription_id: 'sub' }] }).mockResolvedValue({ rows: [] });
      expect(await svc.cancelCommunityPremium(1)).toEqual({ success: true, cancelAtPeriodEnd: true });
      expect(stripe.cancelSubscription).toHaveBeenCalledWith('sub', false);
      expect(query).toHaveBeenCalledTimes(2);
    });

    it('cancels immediately, revoking premium, and skips Stripe when no id is linked', async () => {
      query.mockResolvedValueOnce({ rows: [{}] }).mockResolvedValue({ rows: [] });
      expect(await svc.cancelCommunityPremium(1, true)).toEqual({ success: true, cancelAtPeriodEnd: false });
      expect(stripe.cancelSubscription).not.toHaveBeenCalled();
      expect(query.mock.calls[2][0]).toContain('is_premium=false');
    });
  });

  it('syncSeatCount stores and returns the live count', async () => {
    query.mockResolvedValueOnce({ rows: [{ count: '7' }] }).mockResolvedValueOnce({ rows: [] });
    expect(await svc.syncSeatCount(2)).toBe(7);
    expect(query.mock.calls[1][1]).toEqual([7, 2]);
  });
});
