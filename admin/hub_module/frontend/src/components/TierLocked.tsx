import { TIER_LABEL, normalizeTier } from '../services/communityTierConstants';

interface TierLockedProps {
  feature: string;
  requiredTier: number;
  currentTier: number;
}

/**
 * Informative locked state for a feature that needs a minimum community tier. This is a UX hint
 * only: the server's tier gates are authoritative, so a client that bypasses it still gets 402/403.
 */
function TierLocked({ feature, requiredTier, currentTier }: TierLockedProps) {
  const need = TIER_LABEL[normalizeTier(requiredTier)];
  const have = TIER_LABEL[normalizeTier(currentTier)];
  return (
    <div className="text-center py-12" data-testid="tier-locked">
      <div className="card p-8 max-w-2xl mx-auto" role="status">
        <h2 className="text-2xl font-bold text-sky-100 mb-2">{feature} requires {need}</h2>
        <p className="text-navy-400">
          This community is on the {have} tier. Ask your tenant administrator to allocate a {need} slot to this
          community to unlock {feature}.
        </p>
      </div>
    </div>
  );
}

export default TierLocked;
