import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';

const stripe = {
  createCheckoutSession: jest.fn(), getCheckoutSession: jest.fn(), createSubscriptionSession: jest.fn(),
  getSubscription: jest.fn(), updateSubscription: jest.fn(), cancelSubscription: jest.fn(),
  reactivateSubscription: jest.fn(), createRefund: jest.fn(), getRefund: jest.fn(),
  constructWebhookEvent: jest.fn(), handleWebhook: jest.fn(), createCustomer: jest.fn(),
  getCustomer: jest.fn(), listPaymentMethods: jest.fn(),
};
const paypal = {
  createOrder: jest.fn(), captureOrder: jest.fn(), getOrder: jest.fn(), createSubscription: jest.fn(),
  getSubscription: jest.fn(), cancelSubscription: jest.fn(), createRefund: jest.fn(),
  getRefund: jest.fn(), verifyWebhookSignature: jest.fn(), handleWebhook: jest.fn(),
};
jest.unstable_mockModule('../stripeService.js', () => ({ default: stripe }));
jest.unstable_mockModule('../paypalService.js', () => ({ default: paypal }));

const { default: svc } = await import('../paymentService.js');

describe('PaymentService', () => {
  beforeEach(() => {
    jest.spyOn(console, 'error').mockImplementation(() => {});
    for (const m of [...Object.values(stripe), ...Object.values(paypal)]) m.mockReset();
  });
  afterEach(() => jest.restoreAllMocks());

  it('resolves providers case-insensitively and rejects unknown ones', () => {
    expect(svc.getProvider('STRIPE')).toBe(stripe);
    expect(svc.getProvider()).toBe(stripe);
    expect(() => svc.getProvider('venmo')).toThrow('Unsupported payment provider: venmo');
    expect(svc.getSupportedProviders()).toEqual(['stripe', 'paypal']);
  });

  describe('createCheckout', () => {
    it('delegates to stripe', async () => {
      stripe.createCheckoutSession.mockResolvedValue({ id: 1 });
      await expect(svc.createCheckout({ items: [{}], customerEmail: 'e', successUrl: 's', cancelUrl: 'c' })).resolves.toEqual({ id: 1 });
      expect(stripe.createCheckoutSession).toHaveBeenCalledWith(expect.objectContaining({ customerEmail: 'e', metadata: {} }));
    });

    it('totals items for paypal, defaulting quantity and currency', async () => {
      paypal.createOrder.mockResolvedValue({ id: 2 });
      await svc.createCheckout({ provider: 'paypal', items: [{ price: 5, quantity: 2 }, { price: 3 }], successUrl: 's', cancelUrl: 'c' });
      expect(paypal.createOrder).toHaveBeenCalledWith(expect.objectContaining({ totalAmount: 13, currency: 'USD', returnUrl: 's' }));
      await svc.createCheckout({ provider: 'paypal', items: [{ price: 1, currency: 'EUR' }] });
      expect(paypal.createOrder).toHaveBeenLastCalledWith(expect.objectContaining({ currency: 'EUR' }));
      await svc.createCheckout({ provider: 'paypal', items: [] });
      expect(paypal.createOrder).toHaveBeenLastCalledWith(expect.objectContaining({ currency: 'USD', totalAmount: 0 }));
    });

    it('rethrows unsupported providers', async () => {
      await expect(svc.createCheckout({ provider: 'x', items: [] })).rejects.toThrow('Unsupported');
    });

    it('throws when a registered provider has no checkout handler', async () => {
      svc.providers.other = {};
      try {
        await expect(svc.createCheckout({ provider: 'other', items: [] })).rejects.toThrow('not implemented');
        await expect(svc.completePayment('other', 'i')).rejects.toThrow('not implemented');
        await expect(svc.getPayment('other', 'i')).rejects.toThrow('not implemented');
        await expect(svc.createSubscription({ provider: 'other' })).rejects.toThrow('not implemented');
        await expect(svc.cancelSubscription('other', 'i')).rejects.toThrow('not implemented');
        await expect(svc.createRefund({ provider: 'other' })).rejects.toThrow('not implemented');
        await expect(svc.handleWebhook({ provider: 'other' })).rejects.toThrow('not implemented');
        await expect(svc.getRefundablePayment('other', 'i')).rejects.toThrow('not implemented');
      } finally {
        delete svc.providers.other;
      }
    });
  });

  describe('completePayment / getPayment', () => {
    it('routes per provider', async () => {
      stripe.getCheckoutSession.mockResolvedValue('s');
      paypal.captureOrder.mockResolvedValue('c');
      paypal.getOrder.mockResolvedValue('o');
      expect(await svc.completePayment('stripe', 'i')).toBe('s');
      expect(await svc.completePayment('paypal', 'i')).toBe('c');
      expect(await svc.getPayment('stripe', 'i')).toBe('s');
      expect(await svc.getPayment('paypal', 'i')).toBe('o');
    });
    it('propagates failures', async () => {
      await expect(svc.completePayment('x', 'i')).rejects.toThrow();
      await expect(svc.getPayment('x', 'i')).rejects.toThrow();
    });
  });

  describe('getRefundablePayment', () => {
    it('normalizes stripe sessions (string and expanded payment_intent)', async () => {
      stripe.getCheckoutSession.mockResolvedValueOnce({ session: { payment_intent: 'pi_1', metadata: { userId: '7' } } });
      expect(await svc.getRefundablePayment('Stripe', 's')).toMatchObject({ ownerId: '7', refundablePaymentId: 'pi_1' });
      stripe.getCheckoutSession.mockResolvedValueOnce({ session: { payment_intent: { id: 'pi_2' } } });
      expect(await svc.getRefundablePayment('stripe', 's')).toMatchObject({ ownerId: undefined, refundablePaymentId: 'pi_2' });
      stripe.getCheckoutSession.mockResolvedValueOnce({});
      expect(await svc.getRefundablePayment('stripe', 's')).toBeNull();
    });

    it('normalizes paypal orders, preferring COMPLETED captures', async () => {
      paypal.getOrder.mockResolvedValueOnce({ order: { purchase_units: [{ custom_id: 'u1', payments: { captures: [{ id: 'a', status: 'PENDING' }, { id: 'b', status: 'COMPLETED' }] } }] } });
      expect(await svc.getRefundablePayment('paypal', 'o')).toMatchObject({ ownerId: 'u1', refundablePaymentId: 'b' });
      paypal.getOrder.mockResolvedValueOnce({ order: { purchase_units: [{ payments: { captures: [{ id: 'a', status: 'PENDING' }] } }] } });
      expect((await svc.getRefundablePayment('paypal', 'o')).refundablePaymentId).toBe('a');
      paypal.getOrder.mockResolvedValueOnce({ order: { purchase_units: [{}] } });
      expect((await svc.getRefundablePayment('paypal', 'o')).refundablePaymentId).toBeUndefined();
      paypal.getOrder.mockResolvedValueOnce({ order: {} });
      expect(await svc.getRefundablePayment('paypal', 'o')).toMatchObject({ ownerId: undefined, refundablePaymentId: undefined });
      paypal.getOrder.mockResolvedValueOnce({});
      expect(await svc.getRefundablePayment('paypal', 'o')).toBeNull();
    });
  });

  describe('subscriptions', () => {
    it('creates per provider', async () => {
      await svc.createSubscription({ planId: 'p', customerEmail: 'e' });
      expect(stripe.createSubscriptionSession).toHaveBeenCalledWith(expect.objectContaining({ priceId: 'p', trialPeriodDays: 0 }));
      await svc.createSubscription({ provider: 'paypal', planId: 'p', customerEmail: 'e', successUrl: 's' });
      expect(paypal.createSubscription).toHaveBeenCalledWith(expect.objectContaining({ subscriber: { email_address: 'e' }, returnUrl: 's' }));
    });
    it('gets, updates, cancels and reactivates', async () => {
      await svc.getSubscription('stripe', 'i');
      expect(stripe.getSubscription).toHaveBeenCalledWith('i');
      await svc.updateSubscription('stripe', 'i', { a: 1 });
      expect(stripe.updateSubscription).toHaveBeenCalledWith('i', { a: 1 });
      await expect(svc.updateSubscription('paypal', 'i', {})).rejects.toThrow('not implemented');
      await svc.cancelSubscription('stripe', 'i', true);
      expect(stripe.cancelSubscription).toHaveBeenCalledWith('i', true);
      await svc.cancelSubscription('paypal', 'i', false, 'why');
      expect(paypal.cancelSubscription).toHaveBeenCalledWith('i', 'why');
      await svc.reactivateSubscription('stripe', 'i');
      expect(stripe.reactivateSubscription).toHaveBeenCalled();
      await expect(svc.reactivateSubscription('paypal', 'i')).rejects.toThrow('not implemented');
      await expect(svc.getSubscription('x', 'i')).rejects.toThrow();
    });
  });

  describe('refunds', () => {
    it('maps arguments per provider with defaults', async () => {
      await svc.createRefund({ provider: 'STRIPE', paymentId: 'pi', amount: 5, metadata: { m: 1 } });
      expect(stripe.createRefund).toHaveBeenCalledWith({ paymentIntentId: 'pi', amount: 5, reason: 'requested_by_customer', metadata: { m: 1 } });
      await svc.createRefund({ provider: 'paypal', paymentId: 'cap', amount: 5, note: 'n' });
      expect(paypal.createRefund).toHaveBeenCalledWith('cap', { amount: 5, currency: 'USD', note: 'n' });
    });
    it('rejects unknown providers and fetches refunds', async () => {
      await expect(svc.createRefund({ provider: 'x', paymentId: 'p' })).rejects.toThrow('Unsupported');
      await svc.getRefund('stripe', 'r');
      expect(stripe.getRefund).toHaveBeenCalledWith('r');
      await expect(svc.getRefund('x', 'r')).rejects.toThrow();
    });
  });

  describe('webhooks', () => {
    it('verifies stripe signatures through the provider', async () => {
      stripe.constructWebhookEvent.mockReturnValue({ t: 1 });
      stripe.handleWebhook.mockResolvedValue({ ok: 1 });
      expect(await svc.handleWebhook({ provider: 'stripe', headers: { 'stripe-signature': 'sig' }, payload: 'raw' })).toEqual({ ok: 1 });
      expect(stripe.constructWebhookEvent).toHaveBeenCalledWith('raw', 'sig');
    });
    it('rejects paypal webhooks with invalid signatures without processing', async () => {
      paypal.verifyWebhookSignature.mockResolvedValue(false);
      await expect(svc.handleWebhook({ provider: 'paypal', headers: {}, payload: {} })).rejects.toThrow('Invalid webhook signature');
      expect(paypal.handleWebhook).not.toHaveBeenCalled();
      paypal.verifyWebhookSignature.mockResolvedValue(true);
      paypal.handleWebhook.mockResolvedValue({ ok: 2 });
      expect(await svc.handleWebhook({ provider: 'paypal', headers: {}, payload: {} })).toEqual({ ok: 2 });
    });
  });

  describe('customers', () => {
    it('supports stripe only', async () => {
      await svc.createCustomer({ email: 'e', name: 'n' });
      expect(stripe.createCustomer).toHaveBeenCalledWith({ email: 'e', name: 'n', metadata: {} });
      await svc.getCustomer('stripe', 'c');
      await svc.listPaymentMethods('stripe', 'c');
      await expect(svc.createCustomer({ provider: 'paypal' })).rejects.toThrow('not implemented');
      await expect(svc.getCustomer('paypal', 'c')).rejects.toThrow('not implemented');
      await expect(svc.listPaymentMethods('paypal', 'c')).rejects.toThrow('not implemented');
      await expect(svc.getCustomer('x', 'c')).rejects.toThrow();
      await expect(svc.listPaymentMethods('x', 'c')).rejects.toThrow();
      await expect(svc.createCustomer({ provider: 'x' })).rejects.toThrow();
    });
  });

  describe('validateProviderConfig', () => {
    const keys = ['STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET', 'PAYPAL_CLIENT_ID', 'PAYPAL_CLIENT_SECRET', 'PAYPAL_WEBHOOK_ID'];
    const saved = {};
    beforeEach(() => keys.forEach((k) => { saved[k] = process.env[k]; delete process.env[k]; }));
    afterEach(() => keys.forEach((k) => { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }));

    it('reports missing and present config for each provider', () => {
      expect(svc.validateProviderConfig('stripe').errors).toHaveLength(2);
      expect(svc.validateProviderConfig('paypal').errors).toHaveLength(3);
      expect(svc.validateProviderConfig().provider).toBe('stripe');
      keys.forEach((k) => { process.env[k] = 'x'; });
      expect(svc.validateProviderConfig('stripe').valid).toBe(true);
      expect(svc.validateProviderConfig('paypal').valid).toBe(true);
      expect(svc.validateProviderConfig('nope')).toMatchObject({ valid: false, errors: ['Unsupported payment provider: nope'] });
    });
  });

  it('formats and parses amounts per provider', () => {
    expect(svc.formatAmount('stripe', 10.5)).toBe(1050);
    expect(svc.formatAmount('paypal', 10.504)).toBe(10.5);
    expect(svc.formatAmount('other', 7)).toBe(7);
    expect(svc.parseAmount('stripe', 1050)).toBe(10.5);
    expect(svc.parseAmount('paypal', '10.50')).toBe(10.5);
    expect(svc.parseAmount('other', 7)).toBe(7);
  });

  it('fails loudly for unimplemented features', async () => {
    await expect(svc.createPaymentLink({ productId: 'p' })).rejects.toThrow('Stripe payment links');
    await expect(svc.createPaymentLink({ provider: 'paypal', productId: 'p' })).rejects.toThrow('not implemented');
    await expect(svc.getTransactionHistory('stripe', 'c')).rejects.toThrow('not yet implemented');
    await expect(svc.generateReport({})).rejects.toThrow('not yet implemented');
  });
});
