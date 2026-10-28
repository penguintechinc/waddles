/**
 * Placeholder for the loyalty gear-shop admin page (gh-317). The MVP
 * core-currency schema exposes `loyalty_shop_items` at the service layer
 * (`services.community_loyalty.upsert_item()`/`list_items()`) but the
 * admin blueprint has no CRUD routes for it yet — only the internal
 * (service-to-service) blueprint can list enabled items for chat-command
 * display; see `hub_api/blueprints/v1/community_loyalty.py`. Route is kept
 * so sidebar/tab navigation doesn't 404.
 */
import { ShoppingBagIcon } from '@heroicons/react/24/outline';
import { Card, EmptyState, PageHeader } from '../../components/ui';

function LoyaltyGear() {
  return (
    <div>
      <PageHeader title="Loyalty Gear Shop" />
      <Card>
        <EmptyState
          testId="loyalty-gear-empty"
          icon={<ShoppingBagIcon />}
          title="Coming after the MVP launch"
          description="Gear shop management isn't exposed to admins yet. The core-currency loyalty MVP (gh-317) has the shop-item schema but no admin routes for it. This page will return once that surface is built."
        />
      </Card>
    </div>
  );
}

export default LoyaltyGear;
