import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';

import paymentController from '../paymentController.js';
import paymentService from '../../services/paymentService.js';

function mockRes() {
  const res = {};
  res.status = jest.fn().mockReturnValue(res);
  res.json = jest.fn().mockReturnValue(res);
  return res;
}

describe('PaymentController (non-refund endpoints)', () => {
  beforeEach(() => {
    jest.spyOn(console, 'error').mockImplementation(() => {});
    jest.spyOn(console, 'log').mockImplementation(() => {});
    delete process.env.DEFAULT_PAYMENT_PROVIDER;
  });
  afterEach(() => jest.restoreAllMocks());

  describe('createCheckout', () => {
    it('rejects empty/missing items and missing email', async () => {
      for (const body of [{}, { items: [] }, { items: 'x' }]) {
        const res = mockRes();
        await paymentController.createCheckout({ body }, res);
        expect(res.status).toHaveBeenCalledWith(400);
      }
      const res = mockRes();
      await paymentController.createCheckout({ body: { items: [{ id: 1 }] } }, res);
      expect(res.json.mock.calls[0][0].error).toBe('Customer email is required');
    });

    it('creates a checkout and stamps the authenticated user id into metadata', async () => {
      const spy = jest.spyOn(paymentService, 'createCheckout').mockResolvedValue({ ok: 1 });
      process.env.DEFAULT_PAYMENT_PROVIDER = 'paypal';
      const res = mockRes();
      await paymentController.createCheckout({
        body: { items: [{ id: 1 }], customerEmail: 'a@b.c', metadata: { x: 1 } },
        user: { id: 9 },
      }, res);
      expect(spy.mock.calls[0][0]).toMatchObject({ provider: 'paypal', metadata: { x: 1, userId: 9 } });
      expect(res.json).toHaveBeenCalledWith({ ok: 1 });
    });

    it('works anonymously with default provider and returns 500 on failure', async () => {
      const spy = jest.spyOn(paymentService, 'createCheckout').mockResolvedValue({});
      await paymentController.createCheckout({ body: { items: [{}], customerEmail: 'a@b.c' } }, mockRes());
      expect(spy.mock.calls[0][0].provider).toBe('stripe');
      spy.mockRejectedValue(new Error('boom'));
      const res = mockRes();
      await paymentController.createCheckout({ body: { items: [{}], customerEmail: 'a@b.c' } }, res);
      expect(res.status).toHaveBeenCalledWith(500);
      expect(res.json).toHaveBeenCalledWith({ success: false, error: 'boom' });
    });
  });

  describe('completePayment', () => {
    it('validates input, succeeds and handles errors', async () => {
      const res0 = mockRes();
      await paymentController.completePayment({ body: { provider: 'stripe' } }, res0);
      expect(res0.status).toHaveBeenCalledWith(400);
      const spy = jest.spyOn(paymentService, 'completePayment').mockResolvedValue({ done: true });
      const res = mockRes();
      await paymentController.completePayment({ body: { provider: 'stripe', sessionId: 's' } }, res);
      expect(res.json).toHaveBeenCalledWith({ done: true });
      spy.mockRejectedValue(new Error('x'));
      const res2 = mockRes();
      await paymentController.completePayment({ body: { provider: 'stripe', sessionId: 's' } }, res2);
      expect(res2.status).toHaveBeenCalledWith(500);
    });
  });

  describe('createSubscription', () => {
    it('validates, creates with user metadata, and handles errors', async () => {
      const res0 = mockRes();
      await paymentController.createSubscription({ body: { planId: 'p' } }, res0);
      expect(res0.status).toHaveBeenCalledWith(400);
      const spy = jest.spyOn(paymentService, 'createSubscription').mockResolvedValue({ s: 1 });
      const res = mockRes();
      await paymentController.createSubscription({
        body: { planId: 'p', customerEmail: 'a@b.c' }, user: { id: 4 },
      }, res);
      expect(spy.mock.calls[0][0]).toMatchObject({ provider: 'stripe', trialPeriodDays: 0, metadata: { userId: 4 } });
      spy.mockRejectedValue(new Error('x'));
      const res2 = mockRes();
      await paymentController.createSubscription({ body: { planId: 'p', customerEmail: 'a@b.c' } }, res2);
      expect(res2.status).toHaveBeenCalledWith(500);
    });
  });

  describe('cancelSubscription', () => {
    it('passes through flags and defaults', async () => {
      const spy = jest.spyOn(paymentService, 'cancelSubscription').mockResolvedValue({ c: 1 });
      const res = mockRes();
      await paymentController.cancelSubscription({ params: { provider: 'stripe', id: 'i' }, body: {} }, res);
      expect(spy).toHaveBeenCalledWith('stripe', 'i', false, '');
      await paymentController.cancelSubscription({ params: { provider: 'stripe', id: 'i' }, body: { immediately: true, reason: 'r' } }, mockRes());
      expect(spy).toHaveBeenLastCalledWith('stripe', 'i', true, 'r');
    });
  });

  describe.each([
    ['getPayment', 'getPayment'],
    ['getSubscription', 'getSubscription'],
    ['reactivateSubscription', 'reactivateSubscription'],
    ['getRefund', 'getRefund'],
    ['getCustomer', 'getCustomer'],
    ['listPaymentMethods', 'listPaymentMethods'],
    ['cancelSubscription', 'cancelSubscription'],
  ])('%s', (ctrlMethod, svcMethod) => {
    it('returns the service result, and 500 with the message on failure', async () => {
      const spy = jest.spyOn(paymentService, svcMethod).mockResolvedValue({ r: 1 });
      const req = { params: { provider: 'stripe', id: 'abc' }, body: {} };
      const res = mockRes();
      await paymentController[ctrlMethod](req, res);
      expect(res.json).toHaveBeenCalledWith({ r: 1 });
      spy.mockRejectedValue(new Error('nope'));
      const res2 = mockRes();
      await paymentController[ctrlMethod](req, res2);
      expect(res2.status).toHaveBeenCalledWith(500);
      expect(res2.json).toHaveBeenCalledWith({ success: false, error: 'nope' });
    });
  });

  describe('webhooks', () => {
    it.each([
      ['handleStripeWebhook', 'stripe'],
      ['handlePayPalWebhook', 'paypal'],
    ])('%s forwards headers/payload, and returns 400 on a bad signature', async (method, provider) => {
      const spy = jest.spyOn(paymentService, 'handleWebhook').mockResolvedValue({ handled: true });
      const res = mockRes();
      await paymentController[method]({ headers: { h: 1 }, body: { b: 1 } }, res);
      expect(spy).toHaveBeenCalledWith({ provider, headers: { h: 1 }, payload: { b: 1 } });
      expect(res.json).toHaveBeenCalledWith({ received: true, handled: true });
      spy.mockRejectedValue(new Error('Invalid webhook signature'));
      const res2 = mockRes();
      await paymentController[method]({ headers: {}, body: {} }, res2);
      expect(res2.status).toHaveBeenCalledWith(400);
      expect(res2.json.mock.calls[0][0].error).toBe('Invalid webhook signature');
    });
  });

  describe('createCustomer', () => {
    it('validates, stamps user id, and handles failures', async () => {
      const res0 = mockRes();
      await paymentController.createCustomer({ body: { email: 'a@b.c' } }, res0);
      expect(res0.status).toHaveBeenCalledWith(400);
      const spy = jest.spyOn(paymentService, 'createCustomer').mockResolvedValue({ id: 'c' });
      const res = mockRes();
      await paymentController.createCustomer({ body: { email: 'a@b.c', name: 'n' }, user: { id: 2 } }, res);
      expect(spy.mock.calls[0][0].metadata.userId).toBe(2);
      spy.mockRejectedValue(new Error('x'));
      const res2 = mockRes();
      await paymentController.createCustomer({ body: { email: 'a@b.c', name: 'n' } }, res2);
      expect(res2.status).toHaveBeenCalledWith(500);
    });
  });

  describe('config endpoints', () => {
    it('validateConfig returns result or 500', async () => {
      const spy = jest.spyOn(paymentService, 'validateProviderConfig').mockReturnValue({ valid: true });
      const res = mockRes();
      await paymentController.validateConfig({ params: { provider: 'stripe' } }, res);
      expect(res.json).toHaveBeenCalledWith({ valid: true });
      spy.mockImplementation(() => { throw new Error('bad'); });
      const res2 = mockRes();
      await paymentController.validateConfig({ params: { provider: 'x' } }, res2);
      expect(res2.status).toHaveBeenCalledWith(500);
    });

    it('getSupportedProviders returns providers and default, or 500', async () => {
      const res = mockRes();
      await paymentController.getSupportedProviders({}, res);
      expect(res.json).toHaveBeenCalledWith({ success: true, providers: ['stripe', 'paypal'], defaultProvider: 'stripe' });
      process.env.DEFAULT_PAYMENT_PROVIDER = 'paypal';
      const res1 = mockRes();
      await paymentController.getSupportedProviders({}, res1);
      expect(res1.json.mock.calls[0][0].defaultProvider).toBe('paypal');
      jest.spyOn(paymentService, 'getSupportedProviders').mockImplementation(() => { throw new Error('x'); });
      const res2 = mockRes();
      await paymentController.getSupportedProviders({}, res2);
      expect(res2.status).toHaveBeenCalledWith(500);
    });
  });
});

describe('PaymentController.createRefund authorization (direct)', () => {
  const base = { provider: 'stripe', paymentId: 'cs_1' };
  let resolveSpy;
  let refundSpy;

  beforeEach(() => {
    jest.spyOn(console, 'error').mockImplementation(() => {});
    resolveSpy = jest.spyOn(paymentService, 'getRefundablePayment');
    refundSpy = jest.spyOn(paymentService, 'createRefund').mockResolvedValue({ id: 're_1' });
  });
  afterEach(() => jest.restoreAllMocks());

  const attempt = async (user, payment, body = base) => {
    resolveSpy.mockResolvedValue(payment);
    const res = mockRes();
    await paymentController.createRefund({ body, user }, res);
    return res;
  };

  it('requires provider and paymentId', async () => {
    const res = mockRes();
    await paymentController.createRefund({ body: { provider: 'stripe' }, user: { id: 1 } }, res);
    expect(res.status).toHaveBeenCalledWith(400);
    expect(resolveSpy).not.toHaveBeenCalled();
  });

  it('returns 404 when the payment does not exist', async () => {
    const res = await attempt({ id: 1 }, null);
    expect(res.status).toHaveBeenCalledWith(404);
    expect(refundSpy).not.toHaveBeenCalled();
  });

  it('returns 400 for an uncaptured payment', async () => {
    const res = await attempt({ id: 1 }, { ownerId: '1', refundablePaymentId: undefined });
    expect(res.status).toHaveBeenCalledWith(400);
    expect(refundSpy).not.toHaveBeenCalled();
  });

  it.each([
    ['a different non-admin user', { id: 2, roles: ['viewer'] }, { ownerId: '1', refundablePaymentId: 'pi' }],
    ['a user with no roles on an ownerless payment', { id: 2 }, { ownerId: undefined, refundablePaymentId: 'pi' }],
    ['a user without an id', { roles: [] }, { ownerId: '1', refundablePaymentId: 'pi' }],
    ['a community admin who is not a platform admin', { id: 3, roles: ['community-admin'] }, { ownerId: '1', refundablePaymentId: 'pi' }],
  ])('denies %s', async (_label, user, payment) => {
    const res = await attempt(user, payment);
    expect(res.status).toHaveBeenCalledWith(403);
    expect(res.json.mock.calls[0][0].error).toBe('Not authorized to refund this payment');
    expect(refundSpy).not.toHaveBeenCalled();
  });

  it.each([
    ['the owner (numeric vs string id)', { id: 1 }],
    ['a super_admin role', { id: 9, roles: ['super_admin'] }],
    ['a platform-admin role', { id: 9, roles: ['platform-admin'] }],
    ['an isSuperAdmin flag', { id: 9, isSuperAdmin: true }],
  ])('allows %s and records who refunded', async (_label, user) => {
    const res = await attempt(user, { ownerId: '1', refundablePaymentId: 'pi_1' }, { ...base, amount: 5, metadata: { a: 1 } });
    expect(res.json).toHaveBeenCalledWith({ id: 're_1' });
    expect(refundSpy.mock.calls[0][0]).toMatchObject({
      paymentId: 'pi_1', amount: 5, currency: 'USD', reason: 'requested_by_customer',
      metadata: { a: 1, refundedBy: user.id },
    });
  });

  it('surfaces provider failures as 500', async () => {
    resolveSpy.mockResolvedValue({ ownerId: '1', refundablePaymentId: 'pi' });
    refundSpy.mockRejectedValue(new Error('provider down'));
    const res = mockRes();
    await paymentController.createRefund({ body: base, user: { id: 1 } }, res);
    expect(res.status).toHaveBeenCalledWith(500);
  });
});
