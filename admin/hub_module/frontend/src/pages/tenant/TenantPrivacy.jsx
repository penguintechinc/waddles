import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { PageHeader, Tabs } from '../../components/ui';
import BulkTab from './privacy/BulkTab';
import InboxTab from './privacy/InboxTab';
import RunTool from './privacy/RunTool';

const TABS = [
  { id: 'inbox', label: 'Inbox' },
  { id: 'lookup', label: 'Manual lookup' },
  { id: 'bulk', label: 'Bulk' },
];

/**
 * Tenant-admin data privacy console. Inbox: the DSAR request inbox (every tier). Manual lookup:
 * run an export, erasure or Do-Not-Sell for one user (every tier). Bulk: many users at once
 * (Enterprise). Never renders exported personal data inline.
 */
function TenantPrivacy() {
  const { tenantSlug } = useParams();
  const [tab, setTab] = useState('inbox');

  return (
    <div className="p-6 max-w-5xl space-y-6">
      <PageHeader
        title="Data privacy requests"
        description="Receive, track and action data-subject requests for this tenant. Every action is audit-logged."
      />
      <Tabs tabs={TABS} value={tab} onChange={setTab} label="Data privacy sections">
        {tab === 'inbox' && <InboxTab tenantSlug={tenantSlug} />}
        {tab === 'lookup' && <RunTool tenantSlug={tenantSlug} bulk={false} />}
        {tab === 'bulk' && <BulkTab tenantSlug={tenantSlug} />}
      </Tabs>
    </div>
  );
}

export default TenantPrivacy;
