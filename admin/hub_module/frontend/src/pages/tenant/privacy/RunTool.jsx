import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, CardBody, EmptyState, SelectField, TextField } from '../../../components/ui';
import { BULK_LIMITS, privacyErrorMessage, privacyErrorStatus, tenantPrivacyApi } from '../../../services/tenantPrivacyApi';
import { ACTIONS, ACTION_LABEL, RunResults, parseUserIds } from './runShared';

/**
 * Shared run-form used by Manual lookup (one user, every tier) and Bulk (many users, Enterprise).
 * Erasure always asks for explicit confirmation. A 402 on bulk is handed to `onUpgrade`.
 */
export default function RunTool({ tenantSlug, bulk, onUpgrade }) {
  const [action, setAction] = useState('export');
  const [idsText, setIdsText] = useState('');
  const [formError, setFormError] = useState(null);
  const [pending, setPending] = useState(null);
  const [running, setRunning] = useState(false);
  const [requestError, setRequestError] = useState(null);
  const [runs, setRuns] = useState([]);

  const tenantRef = useRef(tenantSlug);
  useEffect(() => {
    // Cleartext export results, typed IDs and a pending erasure confirmation belong to one tenant only.
    tenantRef.current = tenantSlug;
    setRuns([]);
    setPending(null);
    setIdsText('');
    setFormError(null);
    setRequestError(null);
    setRunning(false);
  }, [tenantSlug]);

  const clearErrors = () => {
    setFormError(null);
    setRequestError(null);
  };

  async function execute(act, ids) {
    const forTenant = tenantSlug;
    setRunning(true);
    clearErrors();
    try {
      let results;
      if (!bulk) {
        const [id] = ids;
        let r;
        if (act === 'export') r = await tenantPrivacyApi.exportUser(tenantSlug, id);
        else if (act === 'erase') r = await tenantPrivacyApi.eraseUser(tenantSlug, id, true);
        else r = await tenantPrivacyApi.doNotSell(tenantSlug, id);
        results = [r];
      } else {
        results = (await tenantPrivacyApi.bulk(tenantSlug, act, ids, act === 'erase')).results;
      }
      if (tenantRef.current !== forTenant) return; // tenant changed mid-flight: drop the result
      setRuns((prev) => [{ key: `${Date.now()}-${prev.length}`, action: act, results }, ...prev]);
      setIdsText('');
    } catch (err) {
      if (tenantRef.current !== forTenant) return;
      const status = privacyErrorStatus(err);
      if (bulk && status === 402) onUpgrade();
      else setRequestError({ status, message: privacyErrorMessage(err, 'Request failed.') });
    }
    setRunning(false);
  }

  function handleSubmit(e) {
    e.preventDefault();
    clearErrors();
    const parsed = parseUserIds(idsText);
    if (parsed.error) return setFormError(parsed.error);
    if (!bulk && parsed.ids.length !== 1) return setFormError('Manual lookup handles one user at a time. Use Bulk for several.');
    const limit = BULK_LIMITS[action];
    if (bulk && parsed.ids.length > limit) {
      return setFormError(`At most ${limit} users per ${ACTION_LABEL[action].toLowerCase()} request.`);
    }
    if (action === 'erase') return setPending(parsed.ids);
    return execute(action, parsed.ids);
  }

  async function confirmErase() {
    const ids = pending;
    setPending(null);
    await execute('erase', ids);
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardBody>
          <form onSubmit={handleSubmit} noValidate className="space-y-4">
            <SelectField
              label="Request type"
              value={action}
              onChange={(e) => {
                setAction(e.target.value);
                clearErrors();
              }}
            >
              {ACTIONS.map((a) => (
                <option key={a.value} value={a.value}>{a.label}</option>
              ))}
            </SelectField>
            <TextField
              label={bulk ? 'User IDs (UUIDs)' : 'User ID (UUID)'}
              hint={bulk ? `User UUIDs separated by commas or spaces (max ${BULK_LIMITS[action]}).` : 'The UUID of one user in this tenant.'}
              value={idsText}
              onChange={(e) => setIdsText(e.target.value)}
              error={formError || undefined}
            />
            {requestError && (
              <div data-testid="privacy-request-error">
                <Alert tone="danger">
                  {requestError.status === 403
                    ? `You do not have permission to run privacy requests for this tenant. ${requestError.message}`
                    : requestError.message}
                </Alert>
              </div>
            )}
            <Button type="submit" variant={action === 'erase' ? 'danger' : 'primary'} loading={running}>
              {`Run ${ACTION_LABEL[action].toLowerCase()} request`}
            </Button>
          </form>
        </CardBody>
      </Card>

      {pending && (
        <div role="alertdialog" aria-modal="true" aria-labelledby="privacy-erase-title" data-testid="privacy-erase-confirm">
          <Alert tone="warning" title={<span id="privacy-erase-title">Confirm erasure</span>}>
            <p className="mb-3">
              This permanently erases or anonymizes personal data for {pending.length} user{pending.length === 1 ? '' : 's'} (IDs: {pending.join(', ')}). It cannot be undone.
            </p>
            <div className="flex gap-3">
              <Button variant="danger" onClick={confirmErase}>Erase permanently</Button>
              <Button onClick={() => setPending(null)}>Cancel</Button>
            </div>
          </Alert>
        </div>
      )}

      <section aria-labelledby="privacy-runs-title" className="space-y-4">
        <h3 id="privacy-runs-title" className="text-lg font-semibold">Requests this session</h3>
        {runs.length === 0 && (
          <EmptyState
            title="No requests yet"
            description="Outcomes appear here after you run a request. Past requests are in the tenant audit log."
            testId="privacy-empty"
          />
        )}
        {runs.map((run) => (
          <Card key={run.key}>
            <CardBody>
              <h4 className="font-medium mb-2">{ACTION_LABEL[run.action]} request ({run.results.length} user{run.results.length === 1 ? '' : 's'})</h4>
              <RunResults run={run} />
            </CardBody>
          </Card>
        ))}
      </section>
    </div>
  );
}
