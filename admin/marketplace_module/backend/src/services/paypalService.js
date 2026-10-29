import checkoutNodeJssdk from '@paypal/checkout-server-sdk';

/**
 * PayPal Payment Service
 * Handles all PayPal payment operations including orders, webhooks, subscriptions, and refunds
 */
class PayPalService {
  constructor() {
    this._client = null;
  }

  /**
   * Get or create the PayPal HTTP client (lazy initialization)
   * @returns {Object} PayPal HTTP client
   */
  getClient() {
    if (!this._client) {
      this._client = new checkoutNodeJssdk.core.PayPalHttpClient(this.createEnvironment());
    }
    return this._client;
  }

  /**
   * Create PayPal environment based on configuration
   * @returns {Object} PayPal environment
   */
  createEnvironment() {
    const clientId = process.env.PAYPAL_CLIENT_ID;
    const clientSecret = process.env.PAYPAL_CLIENT_SECRET;
    const mode = process.env.PAYPAL_MODE || 'sandbox';

    if (!clientId || !clientSecret) {
      throw new Error('PayPal credentials not configured');
    }

    if (mode === 'production') {
      return new checkoutNodeJssdk.core.LiveEnvironment(clientId, clientSecret);
    } else {
      return new checkoutNodeJssdk.core.SandboxEnvironment(clientId, clientSecret);
    }
  }

  /**
   * Create a PayPal order for one-time payment
   * @param {Object} options - Order options
   * @param {Array} options.items - Line items for the order
   * @param {number} options.totalAmount - Total amount
   * @param {string} options.currency - Currency code
   * @param {string} options.returnUrl - URL to redirect on success
   * @param {string} options.cancelUrl - URL to redirect on cancel
   * @param {Object} options.metadata - Additional metadata
   * @returns {Promise<Object>} Order object
   */
  async createOrder({
    items,
    totalAmount,
    currency = 'USD',
    returnUrl,
    cancelUrl,
    metadata = {},
  }) {
    try {
      const request = new checkoutNodeJssdk.orders.OrdersCreateRequest();
      request.prefer('return=representation');

      const purchaseUnits = [{
        amount: {
          currency_code: currency,
          value: totalAmount.toFixed(2),
          breakdown: {
            item_total: {
              currency_code: currency,
              value: totalAmount.toFixed(2),
            },
          },
        },
        items: items.map(item => ({
          name: item.name,
          description: item.description || '',
          unit_amount: {
            currency_code: currency,
            value: item.price.toFixed(2),
          },
          quantity: item.quantity || 1,
          category: 'DIGITAL_GOODS',
        })),
        // custom_id is returned by OrdersGet and is used for ownership checks.
        // Keep the internal order reference separately for reconciliation.
        custom_id: metadata.userId ? String(metadata.userId) : '',
        invoice_id: metadata.orderId || undefined,
      }];

      request.requestBody({
        intent: 'CAPTURE',
        purchase_units: purchaseUnits,
        application_context: {
          brand_name: 'WaddleBot Marketplace',
          landing_page: 'NO_PREFERENCE',
          user_action: 'PAY_NOW',
          return_url: returnUrl,
          cancel_url: cancelUrl,
        },
      });

      const response = await this.getClient().execute(request);

      // Find the approval URL
      const approvalUrl = response.result.links.find(
        link => link.rel === 'approve'
      )?.href;

      return {
        success: true,
        orderId: response.result.id,
        status: response.result.status,
        approvalUrl,
        order: response.result,
      };
    } catch (error) {
      console.error('PayPal order creation error:', error);
      throw new Error(`Failed to create PayPal order: ${error.message}`);
    }
  }

  /**
   * Capture payment for an approved order
   * @param {string} orderId - PayPal order ID
   * @returns {Promise<Object>} Capture result
   */
  async captureOrder(orderId) {
    try {
      const request = new checkoutNodeJssdk.orders.OrdersCaptureRequest(orderId);
      request.prefer('return=representation');

      const response = await this.getClient().execute(request);

      const captureId = response.result.purchase_units[0]?.payments?.captures[0]?.id;

      return {
        success: true,
        orderId: response.result.id,
        status: response.result.status,
        captureId,
        payer: response.result.payer,
        order: response.result,
      };
    } catch (error) {
      console.error('PayPal order capture error:', error);
      throw new Error(`Failed to capture PayPal order: ${error.message}`);
    }
  }

  /**
   * Get order details
   * @param {string} orderId - PayPal order ID
   * @returns {Promise<Object>} Order details
   */
  async getOrder(orderId) {
    try {
      const request = new checkoutNodeJssdk.orders.OrdersGetRequest(orderId);
      const response = await this.getClient().execute(request);

      return {
        success: true,
        order: response.result,
      };
    } catch (error) {
      console.error('PayPal get order error:', error);
      throw new Error(`Failed to retrieve PayPal order: ${error.message}`);
    }
  }

  /**
   * PayPal REST API base URL for the configured mode.
   * @returns {string}
   */
  getApiBase() {
    return process.env.PAYPAL_MODE === 'production'
      ? 'https://api-m.paypal.com'
      : 'https://api-m.sandbox.paypal.com';
  }

  /**
   * Verify webhook signature via PayPal's verify-webhook-signature API (fail-closed)
   * @param {Object} headers - Request headers
   * @param {Object} body - Request body
   * @returns {Promise<boolean>} Verification result
   */
  async verifyWebhookSignature(headers, body) {
    try {
      // Extract required headers
      const transmissionId = headers['paypal-transmission-id'];
      const transmissionTime = headers['paypal-transmission-time'];
      const certUrl = headers['paypal-cert-url'];
      const transmissionSig = headers['paypal-transmission-sig'];
      const authAlgo = headers['paypal-auth-algo'];
      const webhookId = process.env.PAYPAL_WEBHOOK_ID;

      if (!transmissionId || !transmissionTime || !certUrl || !transmissionSig || !authAlgo) {
        throw new Error('Missing required webhook headers');
      }

      if (!webhookId) {
        throw new Error('PAYPAL_WEBHOOK_ID not configured');
      }

      // Defense in depth: the cert must be hosted by PayPal (PayPal re-checks it server-side).
      const certHost = new URL(certUrl);
      if (certHost.protocol !== 'https:' || !/(^|\.)paypal\.com$/.test(certHost.hostname)) {
        throw new Error('Untrusted PayPal cert_url');
      }

      const verifyRequest = {
        auth_algo: authAlgo,
        cert_url: certUrl,
        transmission_id: transmissionId,
        transmission_sig: transmissionSig,
        transmission_time: transmissionTime,
        webhook_id: webhookId,
        webhook_event: body,
      };

      const base = this.getApiBase();
      const clientId = process.env.PAYPAL_CLIENT_ID;
      const clientSecret = process.env.PAYPAL_CLIENT_SECRET;
      if (!clientId || !clientSecret) {
        throw new Error('PayPal credentials not configured');
      }

      const tokenResp = await globalThis.fetch(`${base}/v1/oauth2/token`, {
        method: 'POST',
        headers: {
          Authorization: `Basic ${Buffer.from(`${clientId}:${clientSecret}`).toString('base64')}`,
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: 'grant_type=client_credentials',
        redirect: 'error',
        signal: AbortSignal.timeout(10000),
      });
      if (!tokenResp.ok) {
        throw new Error(`PayPal token request failed (${tokenResp.status})`);
      }
      const { access_token: accessToken } = await tokenResp.json();

      const verifyResp = await globalThis.fetch(`${base}/v1/notifications/verify-webhook-signature`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(verifyRequest),
        redirect: 'error',
        signal: AbortSignal.timeout(10000),
      });
      if (!verifyResp.ok) {
        throw new Error(`PayPal verification request failed (${verifyResp.status})`);
      }
      const result = await verifyResp.json();

      return result.verification_status === 'SUCCESS';
    } catch (error) {
      console.error('PayPal webhook verification error:', error);
      return false;
    }
  }

  /**
   * Handle webhook events
   * @param {Object} event - PayPal webhook event
   * @returns {Promise<Object>} Processing result
   */
  async handleWebhook(event) {
    try {
      let result = { processed: false };

      switch (event.event_type) {
        case 'CHECKOUT.ORDER.APPROVED':
          result = await this.handleOrderApproved(event.resource);
          break;

        case 'CHECKOUT.ORDER.COMPLETED':
          result = await this.handleOrderCompleted(event.resource);
          break;

        case 'PAYMENT.CAPTURE.COMPLETED':
          result = await this.handleCaptureCompleted(event.resource);
          break;

        case 'PAYMENT.CAPTURE.DENIED':
          result = await this.handleCaptureDenied(event.resource);
          break;

        case 'PAYMENT.CAPTURE.REFUNDED':
          result = await this.handleCaptureRefunded(event.resource);
          break;

        case 'BILLING.SUBSCRIPTION.CREATED':
          result = await this.handleSubscriptionCreated(event.resource);
          break;

        case 'BILLING.SUBSCRIPTION.ACTIVATED':
          result = await this.handleSubscriptionActivated(event.resource);
          break;

        case 'BILLING.SUBSCRIPTION.UPDATED':
          result = await this.handleSubscriptionUpdated(event.resource);
          break;

        case 'BILLING.SUBSCRIPTION.EXPIRED':
        case 'BILLING.SUBSCRIPTION.CANCELLED':
          result = await this.handleSubscriptionCancelled(event.resource);
          break;

        case 'BILLING.SUBSCRIPTION.SUSPENDED':
          result = await this.handleSubscriptionSuspended(event.resource);
          break;

        case 'BILLING.SUBSCRIPTION.PAYMENT.FAILED':
          result = await this.handleSubscriptionPaymentFailed(event.resource);
          break;

        default:
          console.log(`Unhandled PayPal event type: ${event.event_type}`);
          result = { processed: false, message: 'Event type not handled' };
      }

      return {
        success: true,
        eventType: event.event_type,
        ...result,
      };
    } catch (error) {
      console.error('PayPal webhook handling error:', error);
      throw error;
    }
  }

  /**
   * Handle order approved event
   * @param {Object} resource - Order resource
   * @returns {Promise<Object>} Processing result
   */
  async handleOrderApproved(resource) {
    console.log('PayPal order approved:', resource.id);

    return {
      processed: true,
      orderId: resource.id,
      status: resource.status,
    };
  }

  /**
   * Handle order completed event
   * @param {Object} resource - Order resource
   * @returns {Promise<Object>} Processing result
   */
  async handleOrderCompleted(resource) {
    console.log('PayPal order completed:', resource.id);

    return {
      processed: true,
      orderId: resource.id,
      status: resource.status,
      payer: resource.payer,
    };
  }

  /**
   * Handle capture completed event
   * @param {Object} resource - Capture resource
   * @returns {Promise<Object>} Processing result
   */
  async handleCaptureCompleted(resource) {
    console.log('PayPal capture completed:', resource.id);

    return {
      processed: true,
      captureId: resource.id,
      amount: resource.amount,
      status: resource.status,
    };
  }

  /**
   * Handle capture denied event
   * @param {Object} resource - Capture resource
   * @returns {Promise<Object>} Processing result
   */
  async handleCaptureDenied(resource) {
    console.log('PayPal capture denied:', resource.id);

    return {
      processed: true,
      captureId: resource.id,
      status: resource.status,
    };
  }

  /**
   * Handle capture refunded event
   * @param {Object} resource - Refund resource
   * @returns {Promise<Object>} Processing result
   */
  async handleCaptureRefunded(resource) {
    console.log('PayPal capture refunded:', resource.id);

    return {
      processed: true,
      refundId: resource.id,
      amount: resource.amount,
      status: resource.status,
    };
  }

  /**
   * Handle subscription created event
   * @param {Object} resource - Subscription resource
   * @returns {Promise<Object>} Processing result
   */
  async handleSubscriptionCreated(resource) {
    console.log('PayPal subscription created:', resource.id);

    return {
      processed: true,
      subscriptionId: resource.id,
      status: resource.status,
      planId: resource.plan_id,
    };
  }

  /**
   * Handle subscription activated event
   * @param {Object} resource - Subscription resource
   * @returns {Promise<Object>} Processing result
   */
  async handleSubscriptionActivated(resource) {
    console.log('PayPal subscription activated:', resource.id);

    return {
      processed: true,
      subscriptionId: resource.id,
      status: resource.status,
    };
  }

  /**
   * Handle subscription updated event
   * @param {Object} resource - Subscription resource
   * @returns {Promise<Object>} Processing result
   */
  async handleSubscriptionUpdated(resource) {
    console.log('PayPal subscription updated:', resource.id);

    return {
      processed: true,
      subscriptionId: resource.id,
      status: resource.status,
    };
  }

  /**
   * Handle subscription cancelled event
   * @param {Object} resource - Subscription resource
   * @returns {Promise<Object>} Processing result
   */
  async handleSubscriptionCancelled(resource) {
    console.log('PayPal subscription cancelled:', resource.id);

    return {
      processed: true,
      subscriptionId: resource.id,
      status: resource.status,
    };
  }

  /**
   * Handle subscription suspended event
   * @param {Object} resource - Subscription resource
   * @returns {Promise<Object>} Processing result
   */
  async handleSubscriptionSuspended(resource) {
    console.log('PayPal subscription suspended:', resource.id);

    return {
      processed: true,
      subscriptionId: resource.id,
      status: resource.status,
    };
  }

  /**
   * Handle subscription payment failed event
   * @param {Object} resource - Subscription resource
   * @returns {Promise<Object>} Processing result
   */
  async handleSubscriptionPaymentFailed(resource) {
    console.log('PayPal subscription payment failed:', resource.id);

    return {
      processed: true,
      subscriptionId: resource.id,
    };
  }

  /**
   * Create a refund for a captured payment
   * @param {string} captureId - Capture ID
   * @param {Object} options - Refund options
   * @param {number} options.amount - Amount to refund
   * @param {string} options.currency - Currency code
   * @param {string} options.note - Note for the refund
   * @returns {Promise<Object>} Refund object
   */
  async createRefund(captureId, { amount, currency = 'USD', note = '' }) {
    try {
      const request = new checkoutNodeJssdk.payments.CapturesRefundRequest(captureId);

      const refundData = {
        note_to_payer: note,
      };

      if (amount) {
        refundData.amount = {
          currency_code: currency,
          value: amount.toFixed(2),
        };
      }

      request.requestBody(refundData);

      const response = await this.getClient().execute(request);

      return {
        success: true,
        refundId: response.result.id,
        status: response.result.status,
        amount: response.result.amount,
        refund: response.result,
      };
    } catch (error) {
      console.error('PayPal refund creation error:', error);
      throw new Error(`Failed to create PayPal refund: ${error.message}`);
    }
  }

  /**
   * Get refund details
   * @param {string} refundId - Refund ID
   * @returns {Promise<Object>} Refund details
   */
  async getRefund(refundId) {
    try {
      const request = new checkoutNodeJssdk.payments.RefundsGetRequest(refundId);
      const response = await this.getClient().execute(request);

      return {
        success: true,
        refund: response.result,
      };
    } catch (error) {
      console.error('PayPal get refund error:', error);
      throw new Error(`Failed to retrieve PayPal refund: ${error.message}`);
    }
  }

  /**
   * Get capture details
   * @param {string} captureId - Capture ID
   * @returns {Promise<Object>} Capture details
   */
  async getCapture(captureId) {
    try {
      const request = new checkoutNodeJssdk.payments.CapturesGetRequest(captureId);
      const response = await this.getClient().execute(request);

      return {
        success: true,
        capture: response.result,
      };
    } catch (error) {
      console.error('PayPal get capture error:', error);
      throw new Error(`Failed to retrieve PayPal capture: ${error.message}`);
    }
  }

  /**
   * Create a subscription plan (product and plan)
   * @param {Object} options - Plan options
   * @param {string} options.name - Plan name
   * @param {string} options.description - Plan description
   * @param {number} options.price - Monthly price
   * @param {string} options.currency - Currency code
   * @param {string} options.interval - Billing interval (DAY, WEEK, MONTH, YEAR)
   * @param {number} options.intervalCount - Interval count
   * @returns {Promise<Object>} Plan object
   */
  async createSubscriptionPlan({
    name,
    description,
    price,
    currency = 'USD',
    interval = 'MONTH',
    intervalCount = 1,
  }) {
    try {
      // Note: Creating products and plans requires REST API calls
      // The checkout-server-sdk doesn't include these endpoints
      // You would need to use axios or similar to make these calls

      console.log('Creating subscription plan:', { name, description, price, currency, interval });

      // This is a placeholder - implement using PayPal REST API
      throw new Error('Subscription plan creation requires REST API implementation');
    } catch (error) {
      console.error('PayPal plan creation error:', error);
      throw new Error(`Failed to create PayPal plan: ${error.message}`);
    }
  }

  /**
   * Create a subscription for a customer
   * @param {Object} options - Subscription options
   * @param {string} options.planId - Plan ID
   * @param {string} options.returnUrl - URL to redirect on success
   * @param {string} options.cancelUrl - URL to redirect on cancel
   * @param {Object} options.subscriber - Subscriber information
   * @returns {Promise<Object>} Subscription object
   */
  async createSubscription({
    planId,
    returnUrl,
    cancelUrl,
    subscriber = {},
  }) {
    try {
      // Note: Creating subscriptions requires REST API calls
      // The checkout-server-sdk doesn't include these endpoints
      // You would need to use axios or similar to make these calls

      console.log('Creating subscription:', { planId, returnUrl, cancelUrl, subscriber });

      // This is a placeholder - implement using PayPal REST API
      throw new Error('Subscription creation requires REST API implementation');
    } catch (error) {
      console.error('PayPal subscription creation error:', error);
      throw new Error(`Failed to create PayPal subscription: ${error.message}`);
    }
  }

  /**
   * Cancel a subscription
   * @param {string} subscriptionId - Subscription ID
   * @param {string} reason - Cancellation reason
   * @returns {Promise<Object>} Cancellation result
   */
  async cancelSubscription(subscriptionId, reason = 'Customer request') {
    try {
      // Note: Cancelling subscriptions requires REST API calls
      console.log('Cancelling subscription:', subscriptionId, reason);

      // This is a placeholder - implement using PayPal REST API
      throw new Error('Subscription cancellation requires REST API implementation');
    } catch (error) {
      console.error('PayPal subscription cancellation error:', error);
      throw new Error(`Failed to cancel PayPal subscription: ${error.message}`);
    }
  }

  /**
   * Get subscription details
   * @param {string} subscriptionId - Subscription ID
   * @returns {Promise<Object>} Subscription details
   */
  async getSubscription(subscriptionId) {
    try {
      // Note: Getting subscriptions requires REST API calls
      console.log('Getting subscription:', subscriptionId);

      // This is a placeholder - implement using PayPal REST API
      throw new Error('Get subscription requires REST API implementation');
    } catch (error) {
      console.error('PayPal get subscription error:', error);
      throw new Error(`Failed to retrieve PayPal subscription: ${error.message}`);
    }
  }
}

export default new PayPalService();
