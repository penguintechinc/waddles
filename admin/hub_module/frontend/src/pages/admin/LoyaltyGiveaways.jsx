/**
 * Placeholder for the loyalty giveaways admin page (gh-317). The MVP
 * core-currency schema (`services.community_loyalty`) has no giveaway
 * tables or routes — the previous giveaways feature belonged to the
 * separate `loyalty-interaction` deployment being retired; see
 * `hub_api/blueprints/v1/community_loyalty.py`'s module docstring. Route
 * is kept so sidebar/tab navigation doesn't 404.
 */
import { GiftIcon } from '@heroicons/react/24/outline';
import { Card, EmptyState, PageHeader } from '../../components/ui';

function LoyaltyGiveaways() {
  return (
    <div>
      <PageHeader title="Loyalty Giveaways" />
      <Card>
        <EmptyState
          testId="loyalty-giveaways-empty"
          icon={<GiftIcon />}
          title="Coming after the MVP launch"
          description="Currency giveaways are not part of the core-currency loyalty MVP (gh-317) yet. This page will return once giveaways are rebuilt on the new schema."
        />
      </Card>
    </div>
  );
}

export default LoyaltyGiveaways;
