import { useState } from 'react';
import { PlusIcon, TrashIcon } from '@heroicons/react/24/outline';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CheckboxField,
  IconButton,
  SelectField,
  SwitchField,
  Tabs,
  TextareaField,
  TextField,
} from '../../components/ui';

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'members', label: 'Members', badge: 128 },
  { id: 'audit', label: 'Audit log' },
];

/** Buttons, form controls, feedback and tabs, in their key states. */
export function ControlsSection() {
  const [tab, setTab] = useState('overview');
  return (
    <Card as="section" aria-labelledby="ds-controls">
      <CardHeader title="Controls" description="Every control is labelled, keyboard operable and has a visible focus ring." />
      <CardBody className="space-y-10">
        <div>
          <h3 id="ds-controls" className="eyebrow mb-3">Buttons</h3>
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" leadingIcon={<PlusIcon className="h-4 w-4" />}>Create</Button>
            <Button variant="secondary">Secondary</Button>
            <Button variant="outline">Outline</Button>
            <Button variant="ghost">Ghost</Button>
            <Button variant="success">Approve</Button>
            <Button variant="danger">Delete</Button>
            <Button variant="primary" loading>Saving</Button>
            <Button variant="secondary" disabled>Disabled</Button>
            <Button size="sm">Small</Button>
            <IconButton label="Delete item" icon={<TrashIcon />} tone="danger" />
          </div>
        </div>

        <div>
          <h3 className="eyebrow mb-3">Form fields</h3>
          <div className="grid gap-5 md:grid-cols-2">
            <TextField label="Display name" hint="Shown on your public profile." placeholder="Waddles Fan" />
            <TextField label="Handle" required defaultValue="taken_name" error="That handle is already taken." />
            <SelectField label="Platform" defaultValue="discord">
              <option value="discord">Discord</option>
              <option value="twitch">Twitch</option>
              <option value="slack">Slack</option>
            </SelectField>
            <TextareaField label="Bio" rows={3} hint="Up to 280 characters." />
            <CheckboxField label="Email me weekly digests" hint="You can unsubscribe at any time." />
            <SwitchField label="Event sync" description="Mirror Waddles events to Discord." defaultChecked />
          </div>
        </div>

        <div>
          <h3 className="eyebrow mb-3">Feedback</h3>
          <div className="grid gap-3">
            <Alert tone="success" title="Saved">Your changes are live.</Alert>
            <Alert tone="info" title="Heads up">Role sync runs every five minutes.</Alert>
            <Alert tone="warning" title="Almost at quota">You have used 90% of your seats.</Alert>
            <Alert tone="danger" title="Connection failed" onDismiss={() => undefined}>Discord rejected the bot token.</Alert>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Badge tone="green">Active</Badge>
            <Badge tone="gold">Pro</Badge>
            <Badge tone="sky">Beta</Badge>
            <Badge tone="red">Failed</Badge>
            <Badge>Draft</Badge>
          </div>
        </div>

        <div>
          <h3 className="eyebrow mb-3">Tabs</h3>
          <Tabs tabs={TABS} value={tab} onChange={setTab} label="Example sections">
            <p className="text-sm text-fg-secondary">Panel content for the {tab} tab.</p>
          </Tabs>
        </div>
      </CardBody>
    </Card>
  );
}
