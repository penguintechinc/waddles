import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import {
  StarIcon,
  CheckCircleIcon,
  XCircleIcon,
  CreditCardIcon,
  UserGroupIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';
import { unifiedMarketplaceApi } from '../../services/api';

const PLAN_STATUS_LABELS = {
  active: { label: 'Active', className: 'text-green-400 bg-green-900/30 border-green-700' },
  trialing: { label: 'Trialing', className: 'text-blue-400 bg-blue-900/30 border-blue-700' },
  inactive: { label: 'Inactive', className: 'text-gray-400 bg-gray-800 border-gray-600' },
  canceled: { label: 'Canceled', className: 'text-red-400 bg-red-900/30 border-red-700' },
};

function formatCents(cents) {
  if (cents == null) return '$0.00';
  return `$${(cents / 100).toFixed(2)}`;
}

export default function AdminCommunityPremium() {
  const { communityId } = useParams();
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [pricing, setPricing] = useState(null);
  const [subscription, setSubscription] = useState(null);
  const [seatCount, setSeatCount] = useState(1);
  const [confirmCancel, setConfirmCancel] = useState(false);

  useEffect(() => {
    loadData();
  }, [communityId]);

  const loadData = async () => {
    try {
      setLoading(true);
      setError(null);
      const [pricingRes, subscriptionRes] = await Promise.all([
        unifiedMarketplaceApi.getPricing({ communityId }),
        unifiedMarketplaceApi.getPremiumStatus(communityId),
      ]);
      setPricing(pricingRes.data);
      setSubscription(subscriptionRes.data);
      if (subscriptionRes.data?.seatCount) {
        setSeatCount(subscriptionRes.data.seatCount);
      }
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to load premium data');
    } finally {
      setLoading(false);
    }
  };

  const handleSubscribe = async () => {
    try {
      setActionLoading(true);
      setError(null);
      const response = await unifiedMarketplaceApi.subscribePremium({
        communityId,
        seatCount,
      });
      if (response.data?.checkoutUrl) {
        window.location.href = response.data.checkoutUrl;
      } else {
        setSuccess('Subscription activated successfully.');
        await loadData();
      }
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to start subscription');
    } finally {
      setActionLoading(false);
    }
  };

  const handleCancel = async () => {
    if (!confirmCancel) {
      setConfirmCancel(true);
      return;
    }
    try {
      setActionLoading(true);
      setError(null);
      await unifiedMarketplaceApi.cancelPremium({ communityId });
      setSuccess('Subscription canceled. Access continues until the end of the billing period.');
      setConfirmCancel(false);
      await loadData();
    } catch (err) {
      setError(err.response?.data?.error?.message || 'Failed to cancel subscription');
    } finally {
      setActionLoading(false);
    }
  };

  const computeTotal = () => {
    if (!pricing) return null;
    const basePrice = pricing.basePriceCents || 0;
    const baseSeats = pricing.baseSeatLimit || 0;
    const overageRate = pricing.overagePriceCents || 0;
    const extraSeats = Math.max(0, seatCount - baseSeats);
    return basePrice + extraSeats * overageRate;
  };

  const statusInfo = PLAN_STATUS_LABELS[subscription?.status] || PLAN_STATUS_LABELS.inactive;
  const isSubscribed = subscription?.status === 'active' || subscription?.status === 'trialing';
  const totalCents = computeTotal();

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-900">
        <div className="text-yellow-400">Loading premium details...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-900 text-white p-6">
      <div className="max-w-3xl mx-auto">
        {/* Header */}
        <div className="mb-8 flex items-center gap-3">
          <StarIcon className="w-8 h-8 text-yellow-400" />
          <div>
            <h1 className="text-3xl font-bold text-yellow-400">Community Premium</h1>
            <p className="text-gray-400">Manage your community's premium subscription</p>
          </div>
        </div>

        {/* Alerts */}
        {error && (
          <div className="mb-6 flex items-center gap-2 bg-red-900/20 border border-red-500 text-red-400 px-4 py-3 rounded-lg">
            <XCircleIcon className="w-5 h-5 flex-shrink-0" />
            <span>{error}</span>
            <button onClick={() => setError(null)} className="ml-auto font-bold">×</button>
          </div>
        )}
        {success && (
          <div className="mb-6 flex items-center gap-2 bg-green-900/20 border border-green-500 text-green-400 px-4 py-3 rounded-lg">
            <CheckCircleIcon className="w-5 h-5 flex-shrink-0" />
            <span>{success}</span>
            <button onClick={() => setSuccess(null)} className="ml-auto font-bold">×</button>
          </div>
        )}

        {/* Current Plan Status */}
        <div className="bg-gray-800 border border-gray-700 rounded-xl p-6 mb-6">
          <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
            <CreditCardIcon className="w-5 h-5 text-yellow-400" />
            Current Plan
          </h2>
          <div className="flex items-center gap-3">
            <span className={`inline-flex items-center gap-1 px-3 py-1 rounded-full border text-sm font-medium ${statusInfo.className}`}>
              {subscription?.status === 'active' && <CheckCircleIcon className="w-4 h-4" />}
              {statusInfo.label}
            </span>
            {subscription?.currentPeriodEnd && (
              <span className="text-gray-400 text-sm">
                {subscription.status === 'canceled'
                  ? `Access until ${new Date(subscription.currentPeriodEnd).toLocaleDateString()}`
                  : `Renews ${new Date(subscription.currentPeriodEnd).toLocaleDateString()}`}
              </span>
            )}
          </div>
          {subscription?.plan && (
            <p className="text-gray-300 mt-2 text-sm">Plan: {subscription.plan}</p>
          )}
        </div>

        {/* Pricing Breakdown */}
        {pricing && (
          <div className="bg-gray-800 border border-gray-700 rounded-xl p-6 mb-6">
            <h2 className="text-lg font-semibold mb-4">Pricing</h2>
            <div className="space-y-3 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-400">Base price (up to {pricing.baseSeatLimit} seats)</span>
                <span className="text-white font-medium">{formatCents(pricing.basePriceCents)} / mo</span>
              </div>
              {seatCount > (pricing.baseSeatLimit || 0) && (
                <div className="flex justify-between">
                  <span className="text-gray-400">
                    Overage seats ({Math.max(0, seatCount - (pricing.baseSeatLimit || 0))} × {formatCents(pricing.overagePriceCents)})
                  </span>
                  <span className="text-white font-medium">
                    {formatCents(Math.max(0, seatCount - (pricing.baseSeatLimit || 0)) * (pricing.overagePriceCents || 0))}
                  </span>
                </div>
              )}
              <div className="border-t border-gray-700 pt-3 flex justify-between font-semibold">
                <span className="text-gray-300">Estimated monthly total</span>
                <span className="text-yellow-400 text-base">{formatCents(totalCents)}</span>
              </div>
            </div>

            {/* Seat Count Input */}
            <div className="mt-5">
              <label htmlFor="admin-community-premium-input" className="block text-sm font-medium text-gray-300 mb-2 flex items-center gap-2">
                <UserGroupIcon className="w-4 h-4" />
                Seat count
              </label>
              <input
                id="admin-community-premium-input"
                type="number"
                min="1"
                value={seatCount}
                onChange={(e) => setSeatCount(Math.max(1, parseInt(e.target.value) || 1))}
                className="w-32 bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white focus:border-yellow-500"
              />
              <p className="text-gray-500 text-xs mt-1">
                First {pricing.baseSeatLimit} seats included in base price
              </p>
            </div>
          </div>
        )}

        {/* Seats Table */}
        {subscription?.seats && subscription.seats.length > 0 && (
          <div className="bg-gray-800 border border-gray-700 rounded-xl p-6 mb-6">
            <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <UserGroupIcon className="w-5 h-5 text-yellow-400" />
              Allocated Seats
            </h2>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-gray-400 border-b border-gray-700">
                    <th className="text-left pb-2">User</th>
                    <th className="text-left pb-2">Role</th>
                    <th className="text-left pb-2">Since</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-700">
                  {subscription.seats.map((seat, idx) => (
                    <tr key={idx}>
                      <td className="py-2 text-white">{seat.username || seat.userId}</td>
                      <td className="py-2 text-gray-400">{seat.role || '—'}</td>
                      <td className="py-2 text-gray-400">
                        {seat.since ? new Date(seat.since).toLocaleDateString() : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="bg-gray-800 border border-gray-700 rounded-xl p-6">
          <h2 className="text-lg font-semibold mb-4">Actions</h2>
          {!isSubscribed ? (
            <button
              onClick={handleSubscribe}
              disabled={actionLoading}
              className="flex items-center gap-2 px-6 py-3 bg-yellow-500 hover:bg-yellow-400 text-gray-900 font-semibold rounded-lg transition-colors disabled:opacity-50"
            >
              <CreditCardIcon className="w-5 h-5" />
              {actionLoading ? 'Redirecting...' : 'Subscribe to Premium'}
            </button>
          ) : (
            <div className="space-y-3">
              <p className="text-gray-400 text-sm">Your community is currently on a premium plan.</p>
              {confirmCancel ? (
                <div className="bg-red-900/20 border border-red-700 rounded-lg p-4">
                  <div className="flex items-center gap-2 text-red-400 mb-3">
                    <ExclamationTriangleIcon className="w-5 h-5" />
                    <span className="font-medium">Confirm cancellation?</span>
                  </div>
                  <p className="text-gray-400 text-sm mb-4">
                    Your premium access will continue until the end of the current billing period.
                  </p>
                  <div className="flex gap-3">
                    <button
                      onClick={handleCancel}
                      disabled={actionLoading}
                      className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors disabled:opacity-50 text-sm"
                    >
                      {actionLoading ? 'Canceling...' : 'Yes, Cancel Subscription'}
                    </button>
                    <button
                      onClick={() => setConfirmCancel(false)}
                      className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg transition-colors text-sm"
                    >
                      Keep Subscription
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  onClick={() => setConfirmCancel(true)}
                  className="px-4 py-2 bg-gray-700 hover:bg-red-700 text-gray-300 hover:text-white rounded-lg transition-colors text-sm"
                >
                  Cancel Subscription
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
