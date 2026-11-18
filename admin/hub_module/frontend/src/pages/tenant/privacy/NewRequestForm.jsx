import { useState } from 'react';
import { Alert, Button, SelectField, TextareaField, TextField } from '../../../components/ui';
import { tenantDsarInboxApi } from '../../../services/tenantDsarInboxApi';
import { REGIME_LABEL, REQUEST_TYPE_LABEL, dsarErrorMessage } from '../../../services/dsarConstants';
import { UUID_RE } from './dsarFormat';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Admin-entered request (letter, phone, email received out of band); starts as `received`. */
export default function NewRequestForm({ tenantSlug, onCreated }) {
  const [requestType, setRequestType] = useState('1');
  const [regime, setRegime] = useState('1');
  const [email, setEmail] = useState('');
  const [subject, setSubject] = useState('');
  const [notes, setNotes] = useState('');
  const [receivedAt, setReceivedAt] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    setError(null);
    if (email && !EMAIL_RE.test(email.trim())) return setError('Enter a valid requester email or leave it blank.');
    if (subject && !UUID_RE.test(subject.trim())) return setError('Linked user must be a user UUID.');
    if (!email && !subject) return setError('Provide a requester email or a linked user UUID.');
    const body = { requestType: Number(requestType), regime: Number(regime) };
    if (email) body.requesterEmail = email.trim();
    if (subject) body.subjectUserId = subject.trim();
    if (notes.trim()) body.notes = notes.trim();
    if (receivedAt) body.receivedAt = new Date(receivedAt).toISOString();
    setBusy(true);
    try {
      await tenantDsarInboxApi.create(tenantSlug, body);
      onCreated();
    } catch (err) {
      setError(dsarErrorMessage(err, 'The request could not be created.'));
    }
    setBusy(false);
  }

  return (
    <form onSubmit={submit} noValidate aria-label="New privacy request" className="space-y-3 rounded border border-line p-4">
      <h3 className="font-medium">Enter a request received out of band</h3>
      <div className="grid gap-3 sm:grid-cols-2">
        <SelectField label="New request type" value={requestType} onChange={(e) => setRequestType(e.target.value)}>
          {Object.entries(REQUEST_TYPE_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </SelectField>
        <SelectField label="New request regime" value={regime} onChange={(e) => setRegime(e.target.value)}>
          {Object.entries(REGIME_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </SelectField>
        <TextField label="Requester email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <TextField label="Linked user (UUID)" value={subject} onChange={(e) => setSubject(e.target.value)} hint="Needed to run export, erase or do-not-sell." />
        <TextField label="Date received" type="date" value={receivedAt} onChange={(e) => setReceivedAt(e.target.value)} hint="Optional; back-dates the deadline clock (max 1 year)." />
      </div>
      <TextareaField label="Notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
      {error && <div data-testid="new-request-error"><Alert tone="danger">{error}</Alert></div>}
      <Button type="submit" variant="primary" loading={busy}>Create request</Button>
    </form>
  );
}
