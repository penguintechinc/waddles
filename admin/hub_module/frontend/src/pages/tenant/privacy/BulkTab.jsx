import { useState } from 'react';
import { Card, CardBody } from '../../../components/ui';
import RunTool from './RunTool';

/** Bulk tab (Enterprise): on a 402 the tool is replaced by an upgrade explanation, never an error. */
export default function BulkTab({ tenantSlug }) {
  const [needsUpgrade, setNeedsUpgrade] = useState(false);

  if (needsUpgrade) {
    return (
      <Card>
        <CardBody>
          <div data-testid="bulk-upgrade" className="space-y-3">
            <h3 className="text-lg font-semibold">Bulk requests are an Enterprise feature</h3>
            <p>
              Every plan can receive, track and action privacy requests one at a time. Enterprise adds automation and
              at-scale tools: run an export, erasure or do-not-sell for up to 100 users in a single operation, with
              per-user results and a full audit trail.
            </p>
            <p className="text-sm text-fg-muted">Contact your account owner to upgrade this tenant.</p>
          </div>
        </CardBody>
      </Card>
    );
  }
  return (
    <div className="space-y-3">
      <p className="text-sm text-fg-muted">Run one action over many users at once. Enterprise only.</p>
      <RunTool tenantSlug={tenantSlug} bulk onUpgrade={() => setNeedsUpgrade(true)} />
    </div>
  );
}
