import { InboxIcon } from '@heroicons/react/24/outline';
import { Badge, Button, Card, CardBody, CardHeader, DataTable, EmptyState, ErrorState, LoadingState, type Column } from '../../components/ui';

interface Row {
  id: number;
  name: string;
  platform: string;
  status: 'Active' | 'Paused';
}

const ROWS: Row[] = [
  { id: 1, name: 'Bar Citizen', platform: 'Discord', status: 'Active' },
  { id: 2, name: 'Penguinz Plays', platform: 'Twitch', status: 'Active' },
  { id: 3, name: 'Waddles HQ', platform: 'Slack', status: 'Paused' },
];

const COLUMNS: Column<Row>[] = [
  { key: 'name', header: 'Community', cell: (r) => <span className="font-medium">{r.name}</span> },
  { key: 'platform', header: 'Platform', cell: (r) => r.platform },
  { key: 'status', header: 'Status', cell: (r) => <Badge tone={r.status === 'Active' ? 'green' : 'gray'}>{r.status}</Badge> },
  { key: 'actions', header: 'Actions', hideHeader: true, align: 'right', cell: (r) => <Button size="sm" variant="ghost" aria-label={`Manage ${r.name}`}>Manage</Button> },
];

/** Data table in all four states plus the standalone loading/empty/error blocks. */
export function DataSection() {
  return (
    <Card as="section" aria-labelledby="ds-data">
      <CardHeader title="Data and states" description="Every list needs a loading, empty and error state, not just the happy path." />
      <CardBody className="space-y-8">
        <div>
          <h3 id="ds-data" className="eyebrow mb-3">Table</h3>
          <DataTable caption="Communities" columns={COLUMNS} rows={ROWS} getRowId={(r) => r.id} />
        </div>
        <div className="grid gap-6 lg:grid-cols-2">
          <div>
            <h3 className="eyebrow mb-3">Table loading</h3>
            <DataTable caption="Communities loading" columns={COLUMNS} rows={undefined} loading getRowId={(r) => r.id} />
          </div>
          <div>
            <h3 className="eyebrow mb-3">Table empty</h3>
            <DataTable
              caption="No communities"
              columns={COLUMNS}
              rows={[]}
              getRowId={(r) => r.id}
              emptyTitle="No communities yet"
              emptyDescription="Create a community or join one to see it here."
              emptyAction={<Button variant="primary">Create community</Button>}
            />
          </div>
          <div className="card">
            <EmptyState icon={<InboxIcon />} title="No bundles activated" description="Choose an app from the catalog to turn it on for this community." action={<Button variant="primary">Browse catalog</Button>} />
          </div>
          <div className="card">
            <ErrorState error={{ response: { data: { error: 'Discord rejected the bot token.' } } }} onRetry={() => undefined} />
          </div>
          <div className="card lg:col-span-2">
            <LoadingState label="Loading members" />
          </div>
        </div>
      </CardBody>
    </Card>
  );
}
