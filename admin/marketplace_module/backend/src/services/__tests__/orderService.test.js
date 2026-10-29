import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';

const payments = { createCheckout: jest.fn(), createRefund: jest.fn() };
jest.unstable_mockModule('../paymentService.js', () => ({ default: payments }));

const { default: svc } = await import('../orderService.js');
const OrderService = svc.constructor;

describe('orderService', () => {
  beforeEach(() => {
    payments.createCheckout.mockReset();
    payments.createRefund.mockReset();
    jest.spyOn(console, 'log').mockImplementation(() => {});
    jest.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => jest.restoreAllMocks());

  describe('createOrder', () => {
    const items = [{ name: 'a', price: 10, quantity: 2 }, { name: 'b', price: 5, description: 'd', currency: 'eur', images: ['i'] }];

    it('rejects empty orders without calling the provider', async () => {
      await expect(svc.createOrder({ items: [] })).rejects.toThrow('at least one item');
      await expect(svc.createOrder({})).rejects.toThrow('at least one item');
      expect(payments.createCheckout).not.toHaveBeenCalled();
    });

    it('totals the order with tax and opens a checkout carrying the user id', async () => {
      payments.createCheckout.mockResolvedValue({ sessionId: 's', url: 'u' });
      const out = await svc.createOrder({ userId: 7, items, customerEmail: 'e', customerName: 'n' });
      expect(out).toMatchObject({ success: true, checkoutUrl: 'u' });
      expect(out.order).toMatchObject({ subtotal: 25, tax: 2.5, total: 27.5, status: 'pending', paymentSessionId: 's', paymentProvider: 'stripe' });
      const arg = payments.createCheckout.mock.calls[0][0];
      expect(arg.metadata).toEqual({ orderId: out.order.id, userId: 7 });
      expect(arg.items[1]).toMatchObject({ quantity: 1, currency: 'eur', images: ['i'] });
      expect(arg.items[0]).toMatchObject({ currency: 'usd', images: [] });
    });

    it('propagates provider failures', async () => {
      payments.createCheckout.mockRejectedValue(new Error('down'));
      await expect(svc.createOrder({ items })).rejects.toThrow('down');
    });
  });

  describe('payment outcome processing', () => {
    it('processPaymentSuccess rejects a session id mismatch (placeholder order has no session)', async () => {
      await expect(svc.processPaymentSuccess({ orderId: 'o', sessionId: 's' })).rejects.toThrow('mismatch');
    });
    it('processPaymentSuccess completes when the session matches', async () => {
      const spy = jest.spyOn(svc, 'fulfillOrder');
      expect(await svc.processPaymentSuccess({ orderId: 'o', sessionId: undefined })).toEqual({ success: true, orderId: 'o', status: 'paid' });
      expect(spy).toHaveBeenCalledWith('o');
    });
    it('processPaymentFailure notifies and reports failure', async () => {
      const spy = jest.spyOn(svc, 'sendPaymentFailureNotification');
      expect(await svc.processPaymentFailure({ orderId: 'o', failureReason: 'x' })).toEqual({ success: true, orderId: 'o', status: 'payment_failed' });
      expect(spy).toHaveBeenCalledWith('o');
    });
    it('processPaymentFailure rethrows notification errors', async () => {
      jest.spyOn(svc, 'sendPaymentFailureNotification').mockRejectedValue(new Error('smtp'));
      await expect(svc.processPaymentFailure({ orderId: 'o' })).rejects.toThrow('smtp');
    });
  });

  describe('side-effect helpers', () => {
    it('fulfil/revoke resolve and rethrow when logging fails', async () => {
      await expect(svc.fulfillOrder('o')).resolves.toBeUndefined();
      await expect(svc.revokeOrderAccess('o')).resolves.toBeUndefined();
      console.log.mockImplementation(() => { throw new Error('log'); });
      await expect(svc.fulfillOrder('o')).rejects.toThrow('log');
      await expect(svc.revokeOrderAccess('o')).rejects.toThrow('log');
    });
    it('email helpers never throw', async () => {
      console.log.mockImplementation(() => { throw new Error('log'); });
      await expect(svc.sendOrderConfirmation('o')).resolves.toBeUndefined();
      await expect(svc.sendPaymentFailureNotification('o')).resolves.toBeUndefined();
      await expect(svc.sendRefundConfirmation('o')).resolves.toBeUndefined();
      expect(console.error).toHaveBeenCalledTimes(3);
    });
  });

  describe('processRefund', () => {
    it('refuses to refund an order that is not marked paid (current placeholder state)', async () => {
      await expect(svc.processRefund({ orderId: 'o', amount: 10 })).rejects.toThrow('Order must be paid');
      expect(payments.createRefund).not.toHaveBeenCalled();
    });
  });

  it('getOrder and getUserOrders return placeholders', async () => {
    expect(await svc.getOrder('o')).toEqual({ success: true, order: { id: 'o' } });
    expect(await svc.getUserOrders('u')).toEqual({ success: true, orders: [], total: 0 });
    expect(await svc.getUserOrders('u', { limit: 1, offset: 1, status: 'paid' })).toMatchObject({ success: true });
    jest.spyOn(console, 'error');
  });

  describe('webhook handlers', () => {
    it('route checkout completion to processPaymentSuccess only when an order id is present', async () => {
      const spy = jest.spyOn(OrderService.prototype, 'processPaymentSuccess').mockResolvedValue({});
      const h = OrderService.getWebhookHandlers();
      await h.onCheckoutCompleted({ metadata: {} });
      await h.onCheckoutCompleted({});
      expect(spy).not.toHaveBeenCalled();
      await h.onCheckoutCompleted({ metadata: { orderId: 'o' }, sessionId: 's', amountTotal: 5, customerEmail: 'e' });
      expect(spy).toHaveBeenCalledWith({ orderId: 'o', sessionId: 's', paymentProvider: 'stripe', amount: 5, customerEmail: 'e' });
    });
    it('route payment failure to processPaymentFailure only when an order id is present', async () => {
      const spy = jest.spyOn(OrderService.prototype, 'processPaymentFailure').mockResolvedValue({});
      const h = OrderService.getWebhookHandlers();
      await h.onPaymentFailed({});
      expect(spy).not.toHaveBeenCalled();
      await h.onPaymentFailed({ metadata: { orderId: 'o' }, failureMessage: 'declined' });
      expect(spy).toHaveBeenCalledWith({ orderId: 'o', failureReason: 'declined' });
    });
  });
});
