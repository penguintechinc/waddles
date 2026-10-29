import * as premiumService from '../services/premiumService.js';
import { errors } from '../middleware/errorHandler.js';

/** Returns the community id normalized by requireCommunityAdmin; fails loudly if the gate was skipped. */
function authorizedCommunityId(req) {
  if (!Number.isSafeInteger(req.communityId)) {
    throw errors.badRequest('Invalid community ID');
  }
  return req.communityId;
}

export async function getPricing(req, res, next) {
  try {
    const { basePriceCents, baseSeatLimit, overagePriceCents } =
      await premiumService.getPricingConfig();

    const pricing = { basePriceCents, baseSeatLimit, overagePriceCents };

    if (req.query.communityId) {
      const communityId = parseInt(req.query.communityId);
      const seatCount = await premiumService.getCurrentSeatCount(communityId);
      const estimatedCents = premiumService.calculateMonthlyBill(
        seatCount,
        basePriceCents,
        baseSeatLimit,
        overagePriceCents
      );
      pricing.seatCount = seatCount;
      pricing.estimatedCents = estimatedCents;
    }

    return res.json({ success: true, pricing });
  } catch (err) {
    next(err);
  }
}

export async function getSubscriptionStatus(req, res, next) {
  try {
    const communityId = authorizedCommunityId(req);
    const [subscription, currentSeatCount] = await Promise.all([
      premiumService.getSubscriptionStatus(communityId),
      premiumService.getCurrentSeatCount(communityId),
    ]);

    return res.json({ success: true, subscription, currentSeatCount });
  } catch (err) {
    next(err);
  }
}

export async function subscribePremium(req, res, next) {
  try {
    const communityId = authorizedCommunityId(req);
    const { provider = 'stripe', successUrl, cancelUrl } = req.body;
    const result = await premiumService.subscribeCommunityPremium(
      communityId,
      provider,
      successUrl,
      cancelUrl
    );

    return res.json({ success: true, ...result });
  } catch (err) {
    next(err);
  }
}

export async function cancelPremium(req, res, next) {
  try {
    const communityId = authorizedCommunityId(req);
    const { immediately = false } = req.body;
    const result = await premiumService.cancelCommunityPremium(communityId, immediately);

    return res.json({ success: true, ...result });
  } catch (err) {
    next(err);
  }
}
