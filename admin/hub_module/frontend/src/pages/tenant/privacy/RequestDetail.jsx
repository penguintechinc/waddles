import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Badge, Button, SelectField, TextareaField, TextField } from '../../../components/ui';
import { tenantDsarInboxApi } from '../../../services/tenantDsarInboxApi';
import {
  ACTOR_KIND_LABEL,
  ACT_FOR_TYPE,
  ADMIN_SETTABLE_STATUSES,
  DSAR_STALE_CODES,
  RESULT_STATUS,
  STATUS,
  EVENT_TYPE_LABEL,
  REGIME_LABEL,
  REQUEST_TYPE_LABEL,
  SOURCE_LABEL,
  STATUS_LABEL,
  dsarErrorCode,
  dsarErrorMessage,
  isClosedStatus,
} from '../../../services/dsarConstants';
import { UUID_RE, dueBadge, fmtDate } from './dsarFormat';
import { RunResults, STATUS_LABEL as RESULT_LABEL } from './runShared';
import { isSuccessStatus } from '../../../services/tenantPrivacyApi';

const ACT_LABEL = { export: 'Run export', erase: 'Erase user data', do_not_sell: 'Record do-not-sell' };
const SOURCE_LOGGED_IN = 1;
const OVERRIDE_PHRASE = 'OVERRIDE';

/** Warning text for a `partial` export: names the failed sources, never says completed. */
function partialWarning(result) {
  const sources = (result.incomplete ?? []).map((i) => i.source).filter((x) => typeof x === 'string' && x);
  const which = sources.length ? ` (${sources.join(', ')})` : '';
  return `Export incomplete: ${result.incomplete?.length ?? 0} data source(s) failed${which}. The data below is partial and must not be treated as the full record. The request stays in progress; re-run the export once the failed sources recover.`;
}

/** Single request view: timeline, notes, status, assignment, one-time extension and act buttons. */
export default function RequestDetail({ tenantSlug, requestId, onBack }) {
  const [loaded, setLoaded] = useState({ phase: 'loading', request: null, events: [], error: null });
  const [notes, setNotes] = useState('');
  const [status, setStatus] = useState('4');
  const [assignee, setAssignee] = useState('');
  const [reason, setReason] = useState('');
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(null);
  const [message, setMessage] = useState(null);
  const [problem, setProblem] = useState(null);
  const [actResult, setActResult] = useState(null);
  const [warning, setWarning] = useState(null);
  const [subject, setSubject] = useState('');
  const [subjectReason, setSubjectReason] = useState('');
  const [mismatch, setMismatch] = useState(false);
  const [overrideReason, setOverrideReason] = useState('');
  const [overrideTyped, setOverrideTyped] = useState('');

  const seq = useRef(0);
  const load = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const { request, events } = await tenantDsarInboxApi.get(tenantSlug, requestId);
      if (mine !== seq.current) return; // a newer load (or navigation) superseded this one
      setLoaded({ phase: 'ok', request, events, error: null });
      setNotes(request.responseNotes ?? '');
    } catch (err) {
      if (mine !== seq.current) return;
      setLoaded({ phase: 'error', request: null, events: [], error: dsarErrorMessage(err, 'The request could not be loaded.') });
    }
  }, [tenantSlug, requestId]);

  useEffect(() => {
    // A different request (or tenant) must never inherit another request's result/export payload.
    setActResult(null);
    setMessage(null);
    setProblem(null);
    setWarning(null);
    setTyped('');
    setSubject('');
    setSubjectReason('');
    setMismatch(false);
    setOverrideReason('');
    setOverrideTyped('');
    load();
    return () => {
      seq.current += 1; // invalidate any in-flight load for the old request
    };
  }, [load]);

  /** Runs one mutation, then reloads the detail; 409s are shown with their specific message. */
  async function run(key, fn, okMessage) {
    setBusy(key);
    setProblem(null);
    setMessage(null);
    setWarning(null);
    try {
      const out = await fn();
      if (out && out.result) {
        // The act endpoint answers 200 even when the per-user outcome failed: show the real status.
        setActResult(out.result);
        if (isSuccessStatus(out.result.status)) {
          setMessage(okMessage);
          setMismatch(false);
          setOverrideReason('');
          setOverrideTyped('');
        } else if (out.result.status === RESULT_STATUS.partial) setWarning(partialWarning(out.result));
        else setProblem(`${RESULT_LABEL[out.result.status] || out.result.status}${out.result.detail ? `: ${out.result.detail}` : ''}. The action did not complete.`);
      } else {
        setMessage(okMessage);
      }
    } catch (err) {
      const code = dsarErrorCode(err);
      setProblem(dsarErrorMessage(err, 'The change could not be saved.'));
      if (code === 'DSAR_SUBJECT_EMAIL_MISMATCH') setMismatch(true);
      if (code === 'DSAR_CONFLICT') {
        // Someone else moved the request: drop every pending confirmation, never retry blindly.
        setTyped('');
        setMismatch(false);
        setOverrideReason('');
        setOverrideTyped('');
        setSubjectReason('');
      }
      if (code && DSAR_STALE_CODES.includes(code)) {
        // State changed under us: refresh so the controls reflect it.
        await load().catch(() => {});
      }
    }
    await load().catch(() => {});
    setBusy(null);
  }

  if (loaded.phase === 'loading') return <p role="status" className="text-sm text-fg-muted">Loading request…</p>;
  if (loaded.phase === 'error') {
    return (
      <div className="space-y-3">
        <Alert tone="danger">{loaded.error}</Alert>
        <Button onClick={onBack}>Back to inbox</Button>
      </div>
    );
  }

  const r = loaded.request;
  const closed = isClosedStatus(r.status);
  const expired = r.status === STATUS.expired;
  const unverified = (r.unverified || r.status === STATUS.verificationPending) && !expired;
  const subjectLocked = r.source === SOURCE_LOGGED_IN;
  const relinking = Boolean(r.subjectUserId);
  const subjectTrim = subject.trim();
  const subjectInvalid = subjectTrim !== '' && !UUID_RE.test(subjectTrim);
  const due = dueBadge(r.dueState);
  const act = ACT_FOR_TYPE[r.requestType];
  const showOverride = mismatch && act === 'erase';
  const extended = Boolean(r.extendedUntil);

  let actBlock = null;
  if (closed) actBlock = 'This request is closed, so no action can be run.';
  else if (unverified) actBlock = 'The requester has not verified their email, so no action can be run yet.';
  else if (!act) actBlock = 'This request type has no automated action; handle it manually.';
  else if (!r.subjectUserId) actBlock = 'No user is linked to this request. Link a user to run the action.';

  const statusOptions = ADMIN_SETTABLE_STATUSES.filter((s) => !(unverified && (s === 4 || s === 5)));
  // Never submit a value the select is not offering (e.g. stale default 4 on an unverified request).
  const statusValue = statusOptions.includes(Number(status)) ? status : String(statusOptions[0] ?? '');
  const extendBlock = closed
    ? 'Closed requests cannot be extended.'
    : unverified
      ? 'Unverified requests have no deadline to extend.'
      : extended
        ? 'This request has already been extended once.'
        : null;

  return (
    <div className="space-y-5" data-testid="request-detail">
      <Button onClick={onBack}>Back to inbox</Button>
      <h3 className="text-lg font-semibold">
        {REQUEST_TYPE_LABEL[r.requestType] ?? 'Request'} <span className="text-sm text-fg-muted">#{r.id.slice(0, 8)}</span>
      </h3>
      <dl className="grid gap-2 text-sm sm:grid-cols-2">
        <div><dt className="inline font-medium">Status: </dt><dd className="inline"><Badge tone="sky">{STATUS_LABEL[r.status]}</Badge></dd></div>
        <div><dt className="inline font-medium">Regime: </dt><dd className="inline">{REGIME_LABEL[r.regime]}</dd></div>
        <div><dt className="inline font-medium">Source: </dt><dd className="inline">{SOURCE_LABEL[r.source]}</dd></div>
        <div><dt className="inline font-medium">Requested: </dt><dd className="inline">{fmtDate(r.requestedAt)}</dd></div>
        <div><dt className="inline font-medium">Due: </dt><dd className="inline">{fmtDate(r.effectiveDueAt ?? r.dueAt)} {due && <Badge tone={due.tone}>{due.label}</Badge>}</dd></div>
        <div><dt className="inline font-medium">Assignee: </dt><dd className="inline">{r.assigneeId ?? 'Unassigned'}</dd></div>
        {r.extendedUntil && <div><dt className="inline font-medium">Extended until: </dt><dd className="inline" data-testid="extended-until">{fmtDate(r.extendedUntil)}</dd></div>}
        {r.extensionReason && <div className="sm:col-span-2"><dt className="inline font-medium">Extension reason: </dt><dd className="inline">{r.extensionReason}</dd></div>}
        {expired ? (
          <div className="sm:col-span-2" data-testid="expired-note">
            <Badge tone="gray">Expired</Badge> The requester never confirmed their email, so this request expired and its contact data was removed. It can no longer be actioned.
          </div>
        ) : unverified ? (
          <div className="sm:col-span-2" data-testid="unverified-note">
            <Badge tone="gold">Unverified</Badge> The requester has not confirmed their email. Their email and message are hidden until they do; the request expires automatically if never confirmed.
          </div>
        ) : (
          <>
            {r.requesterEmail && <div><dt className="inline font-medium">Requester email: </dt><dd className="inline">{r.requesterEmail}</dd></div>}
            {r.requesterMessage && <div className="sm:col-span-2"><dt className="inline font-medium">Message: </dt><dd className="inline">{r.requesterMessage}</dd></div>}
          </>
        )}
      </dl>

      {message && <p role="status" className="text-sm text-emerald-300" data-testid="detail-message">{message}</p>}
      {warning && <div data-testid="detail-warning"><Alert tone="warning" title="Incomplete export">{warning}</Alert></div>}
      {problem && <div data-testid="detail-problem"><Alert tone="danger">{problem}</Alert></div>}

      <section aria-labelledby="dsar-act-title" className="space-y-2">
        <h4 id="dsar-act-title" className="font-medium">Run action</h4>
        {actBlock ? (
          <p data-testid="act-blocked" className="text-sm text-fg-muted">{actBlock}</p>
        ) : (
          <>
            {act === 'erase' && (
              <TextField label='Type "ERASE" to confirm erasure' value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
            )}
            <Button
              variant={act === 'erase' ? 'danger' : 'primary'}
              loading={busy === 'act'}
              disabled={busy !== null || (act === 'erase' && typed !== 'ERASE')}
              onClick={() => run('act', () => tenantDsarInboxApi.act(tenantSlug, r.id, act, act === 'erase'), 'Action completed.')}
            >
              {ACT_LABEL[act]}
            </Button>
          </>
        )}
        {!actBlock && showOverride && (
          <div data-testid="override-panel" className="space-y-2 rounded border border-line p-3">
            <h5 className="font-medium">Override the email check</h5>
            <p className="text-sm">
              The requester proved control of their own email, but it does not match the linked account, so the
              erasure was blocked. Only override if you have verified the requester&apos;s identity another way. The reason is
              recorded in the audit trail.
            </p>
            <TextareaField label="Override reason" value={overrideReason} maxLength={1000} onChange={(e) => setOverrideReason(e.target.value)} hint="Required." />
            <TextField label={`Type "${OVERRIDE_PHRASE}" to confirm`} value={overrideTyped} onChange={(e) => setOverrideTyped(e.target.value)} autoComplete="off" />
            <Button
              variant="danger"
              loading={busy === 'override'}
              disabled={busy !== null || !overrideReason.trim() || overrideTyped !== OVERRIDE_PHRASE || typed !== 'ERASE'}
              onClick={() => run('override', () => tenantDsarInboxApi.act(tenantSlug, r.id, 'erase', true, overrideReason.trim()), 'Action completed with an administrator override.')}
            >
              Erase with administrator override
            </Button>
            {typed !== 'ERASE' && <p className="text-xs text-fg-muted">Also type ERASE in the erasure confirmation above.</p>}
          </div>
        )}
        {actResult && (
          <div data-testid="act-result">
            <RunResults run={{ action: act, results: [actResult] }} />
          </div>
        )}
      </section>

      <section aria-labelledby="dsar-subject-title" className="space-y-2">
        <h4 id="dsar-subject-title" className="font-medium">Linked user</h4>
        {r.subjectUserId ? (
          <p className="text-sm" data-testid="subject-current">Linked user: {r.subjectUserId}</p>
        ) : (
          <p className="text-sm text-fg-muted">No user is linked yet.</p>
        )}
        {closed ? (
          <p className="text-sm text-fg-muted">The linked user cannot be changed on a closed request.</p>
        ) : subjectLocked ? (
          <p className="text-sm text-fg-muted" data-testid="subject-locked">
            This request was submitted by a signed-in user, so the linked account is fixed and cannot be changed.
          </p>
        ) : (
          <>
            <div className="flex flex-wrap items-end gap-3">
              <TextField
                label={relinking ? 'New linked user (UUID)' : 'User to link (UUID)'}
                value={subject}
                onChange={(e) => setSubject(e.target.value)}
                error={subjectInvalid ? 'Enter a user UUID.' : undefined}
              />
              <Button
                disabled={busy !== null || !subjectTrim || subjectInvalid || (relinking && !subjectReason.trim())}
                onClick={() =>
                  run(
                    'subject',
                    () => {
                      const body = { subjectUserId: subjectTrim };
                      if (relinking) body.subjectOverrideReason = subjectReason.trim();
                      return tenantDsarInboxApi.update(tenantSlug, r.id, body);
                    },
                    relinking ? 'Linked user changed.' : 'User linked.',
                  )
                }
              >
                {relinking ? 'Change linked user' : 'Link user'}
              </Button>
            </div>
            {relinking && (
              <TextareaField
                label="Reason for changing the linked user"
                value={subjectReason}
                maxLength={1000}
                onChange={(e) => setSubjectReason(e.target.value)}
                hint="Required to change an existing link. Recorded in the audit trail."
              />
            )}
          </>
        )}
      </section>

      <section aria-labelledby="dsar-status-title" className="space-y-2">
        <h4 id="dsar-status-title" className="font-medium">Status and assignment</h4>
        {closed ? (
          <p className="text-sm text-fg-muted" data-testid="closed-note">This request is closed and can no longer be changed.</p>
        ) : (
          <>
            <div className="flex flex-wrap items-end gap-3">
              <SelectField label="Set status" value={statusValue} onChange={(e) => setStatus(e.target.value)}>
                {statusOptions.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
              </SelectField>
              <Button disabled={busy !== null} onClick={() => run('status', () => tenantDsarInboxApi.update(tenantSlug, r.id, { status: Number(statusValue) }), 'Status updated.')}>Update status</Button>
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <TextField label="Assign to (user UUID)" value={assignee} onChange={(e) => setAssignee(e.target.value)} />
              <Button
                disabled={busy !== null}
                onClick={() => {
                  if (!UUID_RE.test(assignee.trim())) return setProblem('Assignee must be a user UUID.');
                  return run('assign', () => tenantDsarInboxApi.update(tenantSlug, r.id, { assigneeId: assignee.trim() }), 'Assignee updated.');
                }}
              >
                Assign
              </Button>
              {r.assigneeId && (
                <Button disabled={busy !== null} onClick={() => run('unassign', () => tenantDsarInboxApi.update(tenantSlug, r.id, { clearAssignee: true }), 'Assignee cleared.')}>Unassign</Button>
              )}
            </div>
          </>
        )}
      </section>

      <section aria-labelledby="dsar-notes-title" className="space-y-2">
        <h4 id="dsar-notes-title" className="font-medium">Response notes</h4>
        <TextareaField label="Notes" value={notes} maxLength={4000} onChange={(e) => setNotes(e.target.value)} disabled={closed} />
        <Button disabled={busy !== null || closed} onClick={() => run('notes', () => tenantDsarInboxApi.update(tenantSlug, r.id, { responseNotes: notes }), 'Notes saved.')}>Save notes</Button>
      </section>

      <section aria-labelledby="dsar-extend-title" className="space-y-2">
        <h4 id="dsar-extend-title" className="font-medium">Extend deadline</h4>
        {extendBlock ? (
          <p data-testid="extend-blocked" className="text-sm text-fg-muted">{extendBlock}</p>
        ) : (
          <>
            <TextareaField label="Extension reason" value={reason} maxLength={1000} onChange={(e) => setReason(e.target.value)} hint="Required. A request can be extended only once." />
            <Button
              disabled={busy !== null || !reason.trim()}
              onClick={() => run('extend', () => tenantDsarInboxApi.extend(tenantSlug, r.id, reason.trim()), 'Deadline extended.')}
            >
              Extend deadline
            </Button>
          </>
        )}
      </section>

      <section aria-labelledby="dsar-timeline-title">
        <h4 id="dsar-timeline-title" className="font-medium">Timeline</h4>
        {loaded.events.length === 0 ? (
          <p className="text-sm text-fg-muted">No events yet.</p>
        ) : (
          <ol className="mt-2 space-y-1 text-sm" data-testid="timeline">
            {loaded.events.map((ev) => (
              <li key={ev.id}>
                {fmtDate(ev.createdAt)} · {EVENT_TYPE_LABEL[ev.eventType] ?? `Event ${ev.eventType}`} · {ACTOR_KIND_LABEL[ev.actorKind] ?? 'Unknown'}
                {ev.toStatus ? ` · ${STATUS_LABEL[ev.fromStatus] ?? '—'} → ${STATUS_LABEL[ev.toStatus]}` : ''}
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
