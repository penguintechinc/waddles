/**
 * Placeholder for the loyalty games admin page (gh-317). The MVP
 * core-currency schema (`services.community_loyalty`) has no
 * gambling/games tables or routes — that feature belonged to the separate
 * `loyalty-interaction` deployment being retired; see
 * `hub_api/blueprints/v1/community_loyalty.py`'s module docstring. Route
 * is kept so sidebar/tab navigation doesn't 404.
 */
import { CubeIcon } from '@heroicons/react/24/outline';
import { Card, EmptyState, PageHeader } from '../../components/ui';

function LoyaltyGames() {
  return (
    <div>
      <PageHeader title="Loyalty Games" />
      <Card>
        <EmptyState
          testId="loyalty-games-empty"
          icon={<CubeIcon />}
          title="Coming after the MVP launch"
          description="Currency games (slots, coinflip, roulette) are not part of the core-currency loyalty MVP (gh-317) yet. This page will return once games are rebuilt on the new schema."
        />
      </Card>
    </div>
  );
}

export default LoyaltyGames;
