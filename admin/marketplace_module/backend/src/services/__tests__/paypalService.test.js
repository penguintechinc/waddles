import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';

import svc from '../paypalService.js';

const ENV_KEYS = ['PAYPAL_CLIENT_ID', 'PAYPAL_CLIENT_SECRET', 'PAYPAL_MODE', 'PAYPAL_WEBHOOK_ID'];

describe('PayPalService', () => {
  const saved = {};
  let client;

  beforeEach(() => {
    ENV_KEYS.forEach((k) => { saved[k] = process.env[k]; delete process.env[k]; });
    jest.spyOn(console, 'error').mockImplementation(() => {});
    jest.spyOn(console, 'log').mockImplementation(() => {});
    client = { execute: jest.fn() };
    svc._client = client;
  });
  afterEach(() => {
    ENV_KEYS.forEach((k) => { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; });
    svc._client = null;
    jest.restoreAllMocks();
  });

  describe('client/environment', () => {
    it('refuses to build an environment without credentials', () => {
      expect(() => svc.createEnvironment()).toThrow('PayPal credentials not configured');
      process.env.PAYPAL_CLIENT_ID = 'id';
      expect(() => svc.createEnvironment()).toThrow('PayPal credentials not configured');
    });

    it('builds sandbox by default and live in production mode', () => {
      process.env.PAYPAL_CLIENT_ID = 'id';
      process.env.PAYPAL_CLIENT_SECRET = 'secret';
      expect(svc.createEnvironment().constructor.name).toBe('SandboxEnvironment');
      process.env.PAYPAL_MODE = 'production';
      expect(svc.createEnvironment().constructor.name).toBe('LiveEnvironment');
    });

    it('lazily creates and caches the HTTP client', () => {
      svc._client = null;
      process.env.PAYPAL_CLIENT_ID = 'id';
      process.env.PAYPAL_CLIENT_SECRET = 'secret';
      const a = svc.getClient();
      expect(svc.getClient()).toBe(a);
    });
  });

  describe('createOrder', () => {
    const order = {
      items: [{ name: 'x', price: 5, quantity: 2 }, { name: 'y', price: 1.5, description: 'd' }],
      totalAmount: 11.5, returnUrl: 'r', cancelUrl: 'c',
    };

    it('records the owner in custom_id and returns the approval url', async () => {
      client.execute.mockResolvedValue({ result: { id: 'O1', status: 'CREATED', links: [{ rel: 'self', href: 'a' }, { rel: 'approve', href: 'http://approve' }] } });
      const out = await svc.createOrder({ ...order, metadata: { userId: 42, orderId: 'ord' } });
      expect(out).toMatchObject({ success: true, orderId: 'O1', approvalUrl: 'http://approve' });
      const body = client.execute.mock.calls[0][0].body;
      expect(body.purchase_units[0]).toMatchObject({ custom_id: '42', invoice_id: 'ord' });
      expect(body.purchase_units[0].amount.value).toBe('11.50');
      expect(body.purchase_units[0].items[0].description).toBe('');
      expect(body.purchase_units[0].items[1].quantity).toBe(1);
    });

    it('defaults custom_id to empty and tolerates a missing approval link', async () => {
      client.execute.mockResolvedValue({ result: { id: 'O1', status: 'CREATED', links: [] } });
      const out = await svc.createOrder(order);
      expect(out.approvalUrl).toBeUndefined();
      expect(client.execute.mock.calls[0][0].body.purchase_units[0].custom_id).toBe('');
    });

    it('wraps errors', async () => {
      client.execute.mockRejectedValue(new Error('api'));
      await expect(svc.createOrder(order)).rejects.toThrow('Failed to create PayPal order: api');
    });
  });

  describe('orders', () => {
    it('captures orders and exposes the capture id', async () => {
      client.execute.mockResolvedValue({ result: { id: 'O', status: 'COMPLETED', payer: { p: 1 }, purchase_units: [{ payments: { captures: [{ id: 'CAP' }] } }] } });
      expect((await svc.captureOrder('O')).captureId).toBe('CAP');
      client.execute.mockResolvedValue({ result: { id: 'O', purchase_units: [] } });
      expect((await svc.captureOrder('O')).captureId).toBeUndefined();
      client.execute.mockRejectedValue(new Error('x'));
      await expect(svc.captureOrder('O')).rejects.toThrow('Failed to capture PayPal order');
    });

    it('gets orders', async () => {
      client.execute.mockResolvedValue({ result: { id: 'O' } });
      expect(await svc.getOrder('O')).toEqual({ success: true, order: { id: 'O' } });
      client.execute.mockRejectedValue(new Error('x'));
      await expect(svc.getOrder('O')).rejects.toThrow('Failed to retrieve PayPal order');
    });
  });

  describe('verifyWebhookSignature', () => {
    const headers = {
      'paypal-transmission-id': 'i', 'paypal-transmission-time': 't', 'paypal-cert-url': 'u',
      'paypal-transmission-sig': 's', 'paypal-auth-algo': 'a',
    };

    it('fails closed when any required header is missing', async () => {
      for (const missing of Object.keys(headers)) {
        const h = { ...headers };
        delete h[missing];
        expect(await svc.verifyWebhookSignature(h, {})).toBe(false);
      }
    });

    describe('against the PayPal verify API', () => {
      const realFetch = globalThis.fetch;
      const okHeaders = { ...headers, 'paypal-cert-url': 'https://api.paypal.com/v1/notifications/certs/CERT' };
      let fetchMock;
      beforeEach(() => {
        process.env.PAYPAL_WEBHOOK_ID = 'wh';
        process.env.PAYPAL_CLIENT_ID = 'cid';
        process.env.PAYPAL_CLIENT_SECRET = 'sec';
        fetchMock = jest.fn();
        globalThis.fetch = fetchMock;
      });
      afterEach(() => {
        globalThis.fetch = realFetch;
        delete process.env.PAYPAL_MODE;
      });

      it('accepts only when PayPal answers SUCCESS (and sends the event + webhook id)', async () => {
        fetchMock
          .mockResolvedValueOnce({ ok: true, json: async () => ({ access_token: 'T' }) })
          .mockResolvedValueOnce({ ok: true, json: async () => ({ verification_status: 'SUCCESS' }) });
        expect(await svc.verifyWebhookSignature(okHeaders, { e: 1 })).toBe(true);
        const [url, init] = fetchMock.mock.calls[1];
        expect(url).toBe('https://api-m.sandbox.paypal.com/v1/notifications/verify-webhook-signature');
        expect(init.redirect).toBe('error');
        expect(JSON.parse(init.body)).toMatchObject({ webhook_id: 'wh', webhook_event: { e: 1 } });
      });

      it('rejects a FAILURE verdict, forged signature never returns true', async () => {
        fetchMock
          .mockResolvedValueOnce({ ok: true, json: async () => ({ access_token: 'T' }) })
          .mockResolvedValueOnce({ ok: true, json: async () => ({ verification_status: 'FAILURE' }) });
        expect(await svc.verifyWebhookSignature(okHeaders, {})).toBe(false);
      });

      it('uses the live API in production mode', async () => {
        process.env.PAYPAL_MODE = 'production';
        fetchMock
          .mockResolvedValueOnce({ ok: true, json: async () => ({ access_token: 'T' }) })
          .mockResolvedValueOnce({ ok: true, json: async () => ({ verification_status: 'SUCCESS' }) });
        await svc.verifyWebhookSignature(okHeaders, {});
        expect(fetchMock.mock.calls[0][0]).toBe('https://api-m.paypal.com/v1/oauth2/token');
      });

      it.each([
        ['token endpoint fails', () => fetchMock.mockResolvedValueOnce({ ok: false, status: 500 })],
        ['verify endpoint fails', () => fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({ access_token: 'T' }) }).mockResolvedValueOnce({ ok: false, status: 500 })],
        ['network error', () => fetchMock.mockRejectedValue(new Error('net'))],
      ])('fails closed when the %s', async (_n, arrange) => {
        arrange();
        expect(await svc.verifyWebhookSignature(okHeaders, {})).toBe(false);
      });

      it('fails closed without PAYPAL_WEBHOOK_ID, credentials, or with a non-PayPal cert_url', async () => {
        delete process.env.PAYPAL_WEBHOOK_ID;
        expect(await svc.verifyWebhookSignature(okHeaders, {})).toBe(false);
        process.env.PAYPAL_WEBHOOK_ID = 'wh';
        delete process.env.PAYPAL_CLIENT_ID;
        expect(await svc.verifyWebhookSignature(okHeaders, {})).toBe(false);
        process.env.PAYPAL_CLIENT_ID = 'cid';
        for (const bad of ['https://evil.example.com/cert', 'http://api.paypal.com/c', 'https://paypal.com.evil.io/c']) {
          expect(await svc.verifyWebhookSignature({ ...headers, 'paypal-cert-url': bad }, {})).toBe(false);
        }
        expect(fetchMock).not.toHaveBeenCalled();
      });
    });
  });

  describe('handleWebhook', () => {
    it.each([
      ['CHECKOUT.ORDER.APPROVED', { orderId: 'R' }],
      ['CHECKOUT.ORDER.COMPLETED', { orderId: 'R' }],
      ['PAYMENT.CAPTURE.COMPLETED', { captureId: 'R' }],
      ['PAYMENT.CAPTURE.DENIED', { captureId: 'R' }],
      ['PAYMENT.CAPTURE.REFUNDED', { refundId: 'R' }],
      ['BILLING.SUBSCRIPTION.CREATED', { subscriptionId: 'R' }],
      ['BILLING.SUBSCRIPTION.ACTIVATED', { subscriptionId: 'R' }],
      ['BILLING.SUBSCRIPTION.UPDATED', { subscriptionId: 'R' }],
      ['BILLING.SUBSCRIPTION.EXPIRED', { subscriptionId: 'R' }],
      ['BILLING.SUBSCRIPTION.CANCELLED', { subscriptionId: 'R' }],
      ['BILLING.SUBSCRIPTION.SUSPENDED', { subscriptionId: 'R' }],
      ['BILLING.SUBSCRIPTION.PAYMENT.FAILED', { subscriptionId: 'R' }],
    ])('dispatches %s', async (type, expected) => {
      const out = await svc.handleWebhook({ event_type: type, resource: { id: 'R', status: 'S', plan_id: 'P' } });
      expect(out).toMatchObject({ success: true, eventType: type, processed: true, ...expected });
    });

    it('reports unknown events as unprocessed', async () => {
      expect(await svc.handleWebhook({ event_type: 'NOPE' })).toMatchObject({ processed: false });
    });

    it('rethrows handler failures', async () => {
      await expect(svc.handleWebhook({ event_type: 'CHECKOUT.ORDER.APPROVED' })).rejects.toThrow();
    });
  });

  describe('refunds and captures', () => {
    it('creates a full refund without amount', async () => {
      client.execute.mockResolvedValue({ result: { id: 'RF', status: 'COMPLETED', amount: { value: '5.00' } } });
      const out = await svc.createRefund('CAP', {});
      const req = client.execute.mock.calls[0][0];
      expect(req.path).toContain('CAP');
      expect(req.body).toEqual({ note_to_payer: '' });
      expect(out).toMatchObject({ success: true, refundId: 'RF' });
    });

    it('creates a partial refund with a formatted amount', async () => {
      client.execute.mockResolvedValue({ result: { id: 'RF' } });
      await svc.createRefund('CAP', { amount: 5, currency: 'EUR', note: 'n' });
      expect(client.execute.mock.calls[0][0].body).toEqual({ note_to_payer: 'n', amount: { currency_code: 'EUR', value: '5.00' } });
    });

    it('wraps refund/get failures', async () => {
      client.execute.mockRejectedValue(new Error('x'));
      await expect(svc.createRefund('CAP', {})).rejects.toThrow('Failed to create PayPal refund');
      await expect(svc.getRefund('RF')).rejects.toThrow('Failed to retrieve PayPal refund');
      await expect(svc.getCapture('CAP')).rejects.toThrow('Failed to retrieve PayPal capture');
    });

    it('gets refunds and captures', async () => {
      client.execute.mockResolvedValue({ result: { id: 'Z' } });
      expect((await svc.getRefund('RF')).refund).toEqual({ id: 'Z' });
      expect((await svc.getCapture('CAP')).capture).toEqual({ id: 'Z' });
    });
  });

  describe('unimplemented REST operations fail loudly', () => {
    it.each([
      ['createSubscriptionPlan', [{ name: 'n' }], 'Failed to create PayPal plan'],
      ['createSubscription', [{ planId: 'p' }], 'Failed to create PayPal subscription'],
      ['createSubscription', [{}], 'Failed to create PayPal subscription'],
      ['cancelSubscription', ['s'], 'Failed to cancel PayPal subscription'],
      ['cancelSubscription', ['s', 'why'], 'Failed to cancel PayPal subscription'],
      ['getSubscription', ['s'], 'Failed to retrieve PayPal subscription'],
    ])('%s', async (method, args, message) => {
      await expect(svc[method](...args)).rejects.toThrow(message);
    });
  });
});
