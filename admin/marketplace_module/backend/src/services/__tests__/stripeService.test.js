import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';

import svc from '../stripeService.js';

describe('StripeService', () => {
  let fake;
  let realStripe;

  beforeEach(() => {
    jest.spyOn(console, 'error').mockImplementation(() => {});
    jest.spyOn(console, 'log').mockImplementation(() => {});
    fake = {
      checkout: { sessions: { create: jest.fn(), retrieve: jest.fn() } },
      paymentIntents: { create: jest.fn() },
      webhooks: { constructEvent: jest.fn() },
      refunds: { create: jest.fn(), retrieve: jest.fn() },
      subscriptions: { cancel: jest.fn(), update: jest.fn(), retrieve: jest.fn() },
      customers: { create: jest.fn(), retrieve: jest.fn() },
      paymentMethods: { list: jest.fn() },
    };
    realStripe = svc.stripe;
    svc.stripe = fake;
  });
  afterEach(() => {
    svc.stripe = realStripe;
    jest.restoreAllMocks();
  });

  describe('checkout sessions', () => {
    it('converts prices to cents with defaults', async () => {
      fake.checkout.sessions.create.mockResolvedValue({ id: 's', url: 'u' });
      const out = await svc.createCheckoutSession({
        items: [{ name: 'a', price: 1.99 }, { name: 'b', price: 2, quantity: 3, currency: 'eur', images: ['i'] }],
        customerEmail: 'e', successUrl: 's', cancelUrl: 'c', metadata: { userId: '1' },
      });
      const arg = fake.checkout.sessions.create.mock.calls[0][0];
      expect(arg.line_items[0]).toMatchObject({ quantity: 1, price_data: { currency: 'usd', unit_amount: 199 } });
      expect(arg.line_items[1]).toMatchObject({ quantity: 3, price_data: { currency: 'eur', unit_amount: 200 } });
      expect(arg.metadata).toEqual({ userId: '1', source: 'waddlebot_marketplace' });
      expect(out).toMatchObject({ success: true, sessionId: 's', url: 'u' });
    });

    it('wraps provider errors', async () => {
      fake.checkout.sessions.create.mockRejectedValue(new Error('down'));
      await expect(svc.createCheckoutSession({ items: [] })).rejects.toThrow('Failed to create checkout session: down');
    });

    it('creates subscription sessions with optional trial', async () => {
      fake.checkout.sessions.create.mockResolvedValue({ id: 's', url: 'u' });
      await svc.createSubscriptionSession({ priceId: 'p' });
      expect(fake.checkout.sessions.create.mock.calls[0][0].subscription_data.trial_period_days).toBeUndefined();
      await svc.createSubscriptionSession({ priceId: 'p', trialPeriodDays: 7 });
      expect(fake.checkout.sessions.create.mock.calls[1][0].subscription_data.trial_period_days).toBe(7);
      fake.checkout.sessions.create.mockRejectedValue(new Error('x'));
      await expect(svc.createSubscriptionSession({ priceId: 'p' })).rejects.toThrow('Failed to create subscription session');
    });

    it('retrieves sessions', async () => {
      fake.checkout.sessions.retrieve.mockResolvedValue({ id: 's' });
      expect(await svc.getCheckoutSession('s')).toEqual({ success: true, session: { id: 's' } });
      fake.checkout.sessions.retrieve.mockRejectedValue(new Error('x'));
      await expect(svc.getCheckoutSession('s')).rejects.toThrow('Failed to retrieve session');
    });

    it('creates payment intents', async () => {
      fake.paymentIntents.create.mockResolvedValue({ client_secret: 'cs' });
      const out = await svc.createPaymentIntent({ amount: 12.34 });
      expect(fake.paymentIntents.create.mock.calls[0][0]).toMatchObject({ amount: 1234, currency: 'usd' });
      expect(out.clientSecret).toBe('cs');
      fake.paymentIntents.create.mockRejectedValue(new Error('x'));
      await expect(svc.createPaymentIntent({ amount: 1, currency: 'eur' })).rejects.toThrow('Failed to create payment intent');
    });
  });

  describe('webhook signature verification', () => {
    it('returns the verified event', () => {
      fake.webhooks.constructEvent.mockReturnValue({ type: 't' });
      expect(svc.constructWebhookEvent('raw', 'sig')).toEqual({ type: 't' });
    });
    it('rejects invalid signatures', () => {
      fake.webhooks.constructEvent.mockImplementation(() => { throw new Error('bad sig'); });
      expect(() => svc.constructWebhookEvent('raw', 'sig')).toThrow('Webhook verification failed: bad sig');
    });
  });

  describe('handleWebhook', () => {
    const ev = (type, object) => ({ type, data: { object } });

    it('processes checkout completion for payment and subscription modes', async () => {
      const pay = await svc.handleWebhook(ev('checkout.session.completed', {
        id: 's', amount_total: 1500, mode: 'payment', payment_intent: 'pi', subscription: 'sub',
      }));
      expect(pay).toMatchObject({ success: true, eventType: 'checkout.session.completed', amountTotal: 15, paymentIntentId: 'pi' });
      expect(pay.subscriptionId).toBeUndefined();
      const sub = await svc.handleWebhook(ev('checkout.session.completed', { id: 's', amount_total: 0, mode: 'subscription', subscription: 'sub' }));
      expect(sub.subscriptionId).toBe('sub');
      expect(sub.paymentIntentId).toBeUndefined();
      const bare = await svc.handleCheckoutCompleted({ id: 's', amount_total: 100, mode: 'payment' });
      expect(bare.paymentIntentId).toBeUndefined();
      const bareSub = await svc.handleCheckoutCompleted({ id: 's', amount_total: 100, mode: 'subscription' });
      expect(bareSub.subscriptionId).toBeUndefined();
    });

    it.each([
      ['payment_intent.succeeded', { id: 'pi', amount: 250 }, { amount: 2.5 }],
      ['payment_intent.payment_failed', { id: 'pi', last_payment_error: { message: 'declined' } }, { failureMessage: 'declined' }],
      ['customer.subscription.created', { id: 'sub', status: 'active' }, { status: 'active' }],
      ['customer.subscription.updated', { id: 'sub', cancel_at_period_end: true }, { cancelAtPeriodEnd: true }],
      ['customer.subscription.deleted', { id: 'sub', canceled_at: 5 }, { canceledAt: 5 }],
      ['invoice.payment_succeeded', { id: 'in', amount_paid: 1000 }, { amountPaid: 10 }],
      ['invoice.payment_failed', { id: 'in', attempt_count: 3 }, { attemptCount: 3 }],
    ])('dispatches %s', async (type, obj, expected) => {
      const out = await svc.handleWebhook(ev(type, obj));
      expect(out).toMatchObject({ success: true, eventType: type, processed: true, ...expected });
    });

    it('payment failure without error detail leaves message undefined', async () => {
      expect((await svc.handlePaymentFailed({ id: 'pi' })).failureMessage).toBeUndefined();
    });

    it('reports unhandled events as not processed', async () => {
      expect(await svc.handleWebhook(ev('foo.bar', {}))).toMatchObject({ success: true, processed: false });
    });

    it('rethrows handler errors', async () => {
      await expect(svc.handleWebhook({ type: 'checkout.session.completed' })).rejects.toThrow();
    });
  });

  describe('refunds', () => {
    it('creates a full refund without amount', async () => {
      fake.refunds.create.mockResolvedValue({ id: 're', amount: 1000, status: 'succeeded' });
      const out = await svc.createRefund({ paymentIntentId: 'pi' });
      const arg = fake.refunds.create.mock.calls[0][0];
      expect(arg).toMatchObject({ payment_intent: 'pi', reason: 'requested_by_customer' });
      expect(arg.amount).toBeUndefined();
      expect(out).toMatchObject({ refundId: 're', amount: 10, status: 'succeeded' });
    });

    it('creates a partial refund in cents and tags metadata', async () => {
      fake.refunds.create.mockResolvedValue({ id: 're', amount: 550, status: 'pending' });
      await svc.createRefund({ paymentIntentId: 'pi', amount: 5.5, reason: 'duplicate', metadata: { refundedBy: 1 } });
      expect(fake.refunds.create.mock.calls[0][0]).toMatchObject({
        amount: 550, reason: 'duplicate', metadata: { refundedBy: 1, source: 'waddlebot_marketplace' },
      });
    });

    it('wraps refund failures', async () => {
      fake.refunds.create.mockRejectedValue(new Error('already refunded'));
      await expect(svc.createRefund({ paymentIntentId: 'pi' })).rejects.toThrow('Failed to create refund: already refunded');
    });

    it('retrieves refunds', async () => {
      fake.refunds.retrieve.mockResolvedValue({ id: 're' });
      expect(await svc.getRefund('re')).toEqual({ success: true, refund: { id: 're' } });
      fake.refunds.retrieve.mockRejectedValue(new Error('x'));
      await expect(svc.getRefund('re')).rejects.toThrow('Failed to retrieve refund');
    });
  });

  describe('subscriptions', () => {
    it('cancels immediately or at period end', async () => {
      fake.subscriptions.cancel.mockResolvedValue({ id: 'sub', status: 'canceled' });
      fake.subscriptions.update.mockResolvedValue({ id: 'sub', status: 'active', cancel_at_period_end: true });
      expect((await svc.cancelSubscription('sub', true)).status).toBe('canceled');
      expect(fake.subscriptions.cancel).toHaveBeenCalledWith('sub');
      expect((await svc.cancelSubscription('sub')).cancelAtPeriodEnd).toBe(true);
      expect(fake.subscriptions.update).toHaveBeenCalledWith('sub', { cancel_at_period_end: true });
      fake.subscriptions.update.mockRejectedValue(new Error('x'));
      await expect(svc.cancelSubscription('sub')).rejects.toThrow('Failed to cancel subscription');
    });

    it('reactivates, retrieves and updates', async () => {
      fake.subscriptions.update.mockResolvedValue({ id: 'sub', status: 'active' });
      fake.subscriptions.retrieve.mockResolvedValue({ id: 'sub' });
      expect((await svc.reactivateSubscription('sub')).success).toBe(true);
      expect((await svc.getSubscription('sub')).subscription).toEqual({ id: 'sub' });
      expect((await svc.updateSubscription('sub', { a: 1 })).success).toBe(true);
      fake.subscriptions.update.mockRejectedValue(new Error('x'));
      fake.subscriptions.retrieve.mockRejectedValue(new Error('x'));
      await expect(svc.reactivateSubscription('sub')).rejects.toThrow('Failed to reactivate');
      await expect(svc.getSubscription('sub')).rejects.toThrow('Failed to retrieve subscription');
      await expect(svc.updateSubscription('sub', {})).rejects.toThrow('Failed to update subscription');
    });
  });

  describe('customers', () => {
    it('creates, retrieves and lists payment methods', async () => {
      fake.customers.create.mockResolvedValue({ id: 'c' });
      fake.customers.retrieve.mockResolvedValue({ id: 'c' });
      fake.paymentMethods.list.mockResolvedValue({ data: [1] });
      expect((await svc.createCustomer({ email: 'e', name: 'n' })).customerId).toBe('c');
      expect((await svc.getCustomer('c')).customer).toEqual({ id: 'c' });
      expect((await svc.listPaymentMethods('c')).paymentMethods).toEqual([1]);
      fake.customers.create.mockRejectedValue(new Error('x'));
      fake.customers.retrieve.mockRejectedValue(new Error('x'));
      fake.paymentMethods.list.mockRejectedValue(new Error('x'));
      await expect(svc.createCustomer({ email: 'e', name: 'n' })).rejects.toThrow('Failed to create customer');
      await expect(svc.getCustomer('c')).rejects.toThrow('Failed to retrieve customer');
      await expect(svc.listPaymentMethods('c')).rejects.toThrow('Failed to list payment methods');
    });
  });
});
