import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Badge, Button, EmptyState, SelectField, TextField, CheckboxField } from '../../../components/ui';
import { tenantDsarInboxApi } from '../../../services/tenantDsarInboxApi';
import {
  DUE_STATES,
  DUE_STATE_LABEL,
  REGIME_LABEL,
  REQUEST_TYPE_LABEL,
  SOURCE_LABEL,
  STATUS_LABEL,
  dsarErrorCode,
  dsarErrorMessage,
  dsarErrorStatus,
} from '../../../services/dsarConstants';
import { UUID_RE, dueBadge, fmtDate } from './dsarFormat';
import NewRequestForm from './NewRequestForm';
import RequestDetail from './RequestDetail';

const PAGE_SIZE = 25;
const EMPTY_DRAFT = { status: '', type: '', regime: '', dueState: '', assignee: '', sort: 'requestedAt', order: 'desc', unverified: false };

/** Turns the draft filter form into API filters (only set values). */
function toFilters(draft, offset) {
  const f = { sort: draft.sort, order: draft.order, limit: PAGE_SIZE, offset };
  if (draft.unverified) f.status = 2;
  else if (draft.status) f.status = Number(draft.status);
  if (draft.type) f.type = Number(draft.type);
  if (draft.regime) f.regime = Number(draft.regime);
  if (draft.dueState) f.dueState = draft.dueState;
  if (draft.assignee) f.assignee = draft.assignee;
  return f;
}

/** Inbox tab: filterable, paginated request table plus detail view and admin-entered requests. */
export default function InboxTab({ tenantSlug }) {
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [applied, setApplied] = useState(EMPTY_DRAFT);
  const [offset, setOffset] = useState(0);
  const [state, setState] = useState({ phase: 'loading', data: null, error: null });
  const [filterError, setFilterError] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [showNew, setShowNew] = useState(false);

  const seq = useRef(0);
  const load = useCallback(async () => {
    const mine = ++seq.current;
    setState((s) => ({ ...s, phase: 'loading' }));
    try {
      const data = await tenantDsarInboxApi.list(tenantSlug, toFilters(applied, offset));
      if (mine !== seq.current) return; // out-of-order response: a newer fetch owns the state
      setState({ phase: 'ok', data, error: null });
    } catch (err) {
      if (mine !== seq.current) return;
      if (dsarErrorCode(err) === 'DSAR_INBOX_DISABLED') setState({ phase: 'disabled', data: null, error: null });
      else if (dsarErrorStatus(err) === 403) setState({ phase: 'forbidden', data: null, error: null });
      else setState({ phase: 'error', data: null, error: dsarErrorMessage(err, 'The inbox could not be loaded.') });
    }
  }, [tenantSlug, applied, offset]);

  useEffect(() => {
    load();
    return () => {
      seq.current += 1; // drop the in-flight response on filter/page/tenant change or unmount
    };
  }, [load]);

  const set = (k) => (e) => setDraft((d) => ({ ...d, [k]: e.target.value }));

  function apply(e) {
    e.preventDefault();
    if (draft.assignee && !UUID_RE.test(draft.assignee.trim())) {
      setFilterError('Assignee must be a user UUID.');
      return;
    }
    setFilterError(null);
    setOffset(0);
    setApplied({ ...draft, assignee: draft.assignee.trim() });
  }

  function clear() {
    setDraft(EMPTY_DRAFT);
    setApplied(EMPTY_DRAFT);
    setFilterError(null);
    setOffset(0);
  }

  if (state.phase === 'disabled') {
    return (
      <EmptyState
        testId="inbox-disabled"
        title="Privacy request inbox is not enabled"
        description="The request inbox has not been switched on for this deployment yet. You can still run single-user requests from the Manual lookup tab."
      />
    );
  }
  if (state.phase === 'forbidden') {
    return <Alert tone="danger">You do not have permission to view privacy requests for this tenant.</Alert>;
  }

  if (selectedId) {
    return (
      <RequestDetail
        tenantSlug={tenantSlug}
        requestId={selectedId}
        onBack={() => {
          setSelectedId(null);
          load();
        }}
      />
    );
  }

  const data = state.data;
  const total = data?.total ?? 0;

  return (
    <div className="space-y-5">
      <form onSubmit={apply} noValidate aria-label="Inbox filters" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <SelectField label="Status" value={draft.status} onChange={set('status')} disabled={draft.unverified}>
          <option value="">Active (default)</option>
          {Object.entries(STATUS_LABEL).map(([v, l]) => (
            <option key={v} value={v}>{l}</option>
          ))}
        </SelectField>
        <SelectField label="Request type" value={draft.type} onChange={set('type')}>
          <option value="">All types</option>
          {Object.entries(REQUEST_TYPE_LABEL).map(([v, l]) => (
            <option key={v} value={v}>{l}</option>
          ))}
        </SelectField>
        <SelectField label="Regime" value={draft.regime} onChange={set('regime')}>
          <option value="">All regimes</option>
          {Object.entries(REGIME_LABEL).map(([v, l]) => (
            <option key={v} value={v}>{l}</option>
          ))}
        </SelectField>
        <SelectField label="Due state" value={draft.dueState} onChange={set('dueState')}>
          <option value="">Any</option>
          {DUE_STATES.map((d) => (
            <option key={d} value={d}>{DUE_STATE_LABEL[d]}</option>
          ))}
        </SelectField>
        <TextField label="Assignee (user UUID)" value={draft.assignee} onChange={set('assignee')} error={filterError || undefined} />
        <SelectField label="Sort by" value={draft.sort} onChange={set('sort')}>
          <option value="requestedAt">Requested date</option>
          <option value="dueAt">Due date</option>
        </SelectField>
        <SelectField label="Order" value={draft.order} onChange={set('order')}>
          <option value="desc">Newest / latest first</option>
          <option value="asc">Oldest / earliest first</option>
        </SelectField>
        <div className="flex items-end">
          <CheckboxField
            label="Show unverified requests"
            checked={draft.unverified}
            onChange={(e) => setDraft((d) => ({ ...d, unverified: e.target.checked }))}
          />
        </div>
        <div className="flex gap-3 sm:col-span-2 lg:col-span-4">
          <Button type="submit" variant="primary">Apply filters</Button>
          <Button type="button" onClick={clear}>Clear filters</Button>
          <Button type="button" onClick={() => setShowNew((v) => !v)} aria-expanded={showNew}>New request</Button>
        </div>
      </form>

      {showNew && (
        <NewRequestForm
          tenantSlug={tenantSlug}
          onCreated={() => {
            setShowNew(false);
            load();
          }}
        />
      )}

      {state.phase === 'loading' && <p role="status" className="text-sm text-fg-muted">Loading requests…</p>}
      {state.phase === 'error' && (
        <div data-testid="inbox-error"><Alert tone="danger">{state.error}</Alert></div>
      )}
      {state.phase === 'ok' && data.items.length === 0 && (
        <EmptyState testId="inbox-empty" title="No requests" description="No privacy requests match these filters." />
      )}
      {state.phase === 'ok' && data.items.length > 0 && (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm" data-testid="inbox-table">
              <caption className="sr-only">Privacy requests</caption>
              <thead>
                <tr>
                  <th scope="col" className="p-2">Type</th>
                  <th scope="col" className="p-2">Status</th>
                  <th scope="col" className="p-2">Regime</th>
                  <th scope="col" className="p-2">Due state</th>
                  <th scope="col" className="p-2">Requested</th>
                  <th scope="col" className="p-2">Due</th>
                  <th scope="col" className="p-2">Assignee</th>
                  <th scope="col" className="p-2"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((r) => {
                  const due = dueBadge(r.dueState);
                  return (
                    <tr key={r.id} data-testid={`inbox-row-${r.id}`} className="border-t border-line">
                      <td className="p-2">{REQUEST_TYPE_LABEL[r.requestType] ?? r.requestType}</td>
                      <td className="p-2">
                        <Badge tone={r.unverified ? 'gold' : 'sky'}>{STATUS_LABEL[r.status] ?? r.status}</Badge>
                        {r.unverified && r.status === 2 && <span className="ml-2 text-xs">Unverified</span>}
                      </td>
                      <td className="p-2">{REGIME_LABEL[r.regime] ?? r.regime}</td>
                      <td className="p-2">{due ? <Badge tone={due.tone}>{due.label}</Badge> : <span className="text-fg-muted">No deadline</span>}</td>
                      <td className="p-2">{fmtDate(r.requestedAt)}</td>
                      <td className="p-2">
                        {fmtDate(r.effectiveDueAt ?? r.dueAt)}
                        {r.extendedUntil && <span className="block text-xs text-fg-muted" data-testid={`extended-${r.id}`}>Extended until {fmtDate(r.extendedUntil)}</span>}
                      </td>
                      <td className="p-2">{r.assigneeId ? r.assigneeId.slice(0, 8) : 'Unassigned'}</td>
                      <td className="p-2">
                        <Button size="sm" onClick={() => setSelectedId(r.id)} aria-label={`Open ${REQUEST_TYPE_LABEL[r.requestType] ?? 'request'} (${SOURCE_LABEL[r.source] ?? 'source'}) ${r.id.slice(0, 8)}`}>
                          Open
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <nav aria-label="Pagination" className="flex items-center gap-3">
            <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</Button>
            <span data-testid="inbox-range" className="text-sm">
              {offset + 1}–{Math.min(offset + data.items.length, total)} of {total}
            </span>
            <Button disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</Button>
          </nav>
        </>
      )}
    </div>
  );
}
