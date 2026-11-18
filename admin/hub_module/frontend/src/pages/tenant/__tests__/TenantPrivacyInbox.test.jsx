/** DSAR inbox tab: list, every filter, pagination, states, detail actions. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../services/tenantDsarInboxApi', () => ({
  tenantDsarInboxApi: { list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), extend: vi.fn(), act: vi.fn() },
}));
import { tenantDsarInboxApi as api } from '../../../services/tenantDsarInboxApi';
import TenantPrivacy from '../TenantPrivacy';

const ID1 = '11111111-1111-4111-8111-111111111111';
const ID2 = '22222222-2222-4222-8222-222222222222';
const USER = '33333333-3333-4333-8333-333333333333';

const req = (over = {}) => ({
  id: ID1, requestType: 1, status: 3, regime: 1, source: 2, requestedAt: '2026-11-01T10:00:00Z',
  verifiedAt: '2026-11-01T10:05:00Z', dueAt: '2026-12-01T10:00:00Z', extendedUntil: null, effectiveDueAt: '2026-12-01T10:00:00Z',
  extensionReason: null, dueState: 'ok', completedAt: null, expiresAt: null, assigneeId: null, subjectUserId: USER,
  responseNotes: null, requesterEmail: 'req@example.test', requesterMessage: 'please help', contactPurged: false, unverified: false, ...over,
});
const page = (items, total = items.length) => ({ items, total, limit: 25, offset: 0 });
const err = (status, code, message = 'x') => ({ isAxiosError: true, response: { status, data: { error: { code, message } } } });

function mount() {
  render(
    <MemoryRouter initialEntries={['/tenant/acme/privacy']}>
      <Routes>
        <Route path="/tenant/:tenantSlug/privacy" element={<TenantPrivacy />} />
      </Routes>
    </MemoryRouter>,
  );
}

const lastFilters = () => api.list.mock.calls.at(-1)[1];

beforeEach(() => {
  vi.clearAllMocks();
});

describe('Inbox list', () => {
  it('renders rows with status, due-state text and dates', async () => {
    api.list.mockResolvedValue(page([req({ dueState: 'overdue' }), req({ id: ID2, dueState: 'due_soon', requestType: 2 })]));
    mount();
    await screen.findByTestId('inbox-table');
    expect(api.list).toHaveBeenCalledWith('acme', expect.objectContaining({ limit: 25, offset: 0, sort: 'requestedAt', order: 'desc' }));
    const table = within(screen.getByTestId('inbox-table'));
    expect(table.getByText('Overdue')).toBeTruthy();
    expect(table.getByText('Due soon')).toBeTruthy();
    expect(screen.getByTestId('inbox-range').textContent).toMatch(/1.2 of 2/);
  });

  it('marks unverified rows and shows no email in the table', async () => {
    api.list.mockResolvedValue(page([req({ status: 2, unverified: true, requesterEmail: null, dueState: null, dueAt: null, effectiveDueAt: null })]));
    mount();
    await screen.findByTestId('inbox-table');
    expect(screen.getByText('Unverified')).toBeTruthy();
    expect(screen.getByText('No deadline')).toBeTruthy();
    expect(screen.queryByText(/req@example.test/)).toBeNull();
  });

  it('passes every filter to the API', async () => {
    api.list.mockResolvedValue(page([req()]));
    mount();
    await screen.findByTestId('inbox-table');
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: '4' } });
    fireEvent.change(screen.getByLabelText('Request type'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('Regime'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('Due state'), { target: { value: 'overdue' } });
    fireEvent.change(screen.getByLabelText(/assignee/i), { target: { value: USER } });
    fireEvent.change(screen.getByLabelText('Sort by'), { target: { value: 'dueAt' } });
    fireEvent.change(screen.getByLabelText('Order'), { target: { value: 'asc' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() =>
      expect(lastFilters()).toEqual({ status: 4, type: 2, regime: 2, dueState: 'overdue', assignee: USER, sort: 'dueAt', order: 'asc', limit: 25, offset: 0 }),
    );
  });

  it('rejects a non-UUID assignee without calling the API', async () => {
    api.list.mockResolvedValue(page([req()]));
    mount();
    await screen.findByTestId('inbox-table');
    const calls = api.list.mock.calls.length;
    fireEvent.change(screen.getByLabelText(/assignee/i), { target: { value: 'bob' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    expect(await screen.findByText(/must be a user uuid/i)).toBeTruthy();
    expect(api.list.mock.calls.length).toBe(calls);
  });

  it('show-unverified toggle requests status 2; clear resets', async () => {
    api.list.mockResolvedValue(page([req()]));
    mount();
    await screen.findByTestId('inbox-table');
    fireEvent.click(screen.getByLabelText('Show unverified requests'));
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(lastFilters().status).toBe(2));
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    await waitFor(() => expect(lastFilters().status).toBeUndefined());
  });

  it('paginates with next / previous', async () => {
    api.list.mockResolvedValue(page([req()], 60));
    mount();
    await screen.findByTestId('inbox-table');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(lastFilters().offset).toBe(25));
    fireEvent.click(await screen.findByRole('button', { name: 'Previous' }));
    await waitFor(() => expect(lastFilters().offset).toBe(0));
  });

  it('empty state', async () => {
    api.list.mockResolvedValue(page([]));
    mount();
    expect(await screen.findByTestId('inbox-empty')).toBeTruthy();
  });

  it('403 state', async () => {
    api.list.mockRejectedValue(err(403, null));
    mount();
    expect(await screen.findByText(/do not have permission to view privacy requests/i)).toBeTruthy();
  });

  it('inbox-disabled state', async () => {
    api.list.mockRejectedValue(err(404, 'DSAR_INBOX_DISABLED'));
    mount();
    expect(await screen.findByTestId('inbox-disabled')).toBeTruthy();
  });

  it('generic error state', async () => {
    api.list.mockRejectedValue(err(500, null, 'kaput'));
    mount();
    expect((await screen.findByTestId('inbox-error')).textContent).toMatch(/kaput/);
  });

  it('creates an admin-entered request', async () => {
    api.list.mockResolvedValue(page([]));
    api.create.mockResolvedValue(req());
    mount();
    await screen.findByTestId('inbox-empty');
    fireEvent.click(screen.getByRole('button', { name: 'New request' }));
    fireEvent.change(screen.getByLabelText('Requester email'), { target: { value: 'a@example.test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create request' }));
    await waitFor(() => expect(api.create).toHaveBeenCalledWith('acme', { requestType: 1, regime: 1, requesterEmail: 'a@example.test' }));
  });
});

async function openDetail(request, events = []) {
  api.list.mockResolvedValue(page([request]));
  api.get.mockResolvedValue({ request, events });
  mount();
  fireEvent.click(await screen.findByRole('button', { name: /^Open/ }));
  return screen.findByTestId('request-detail');
}

describe('Request detail', () => {
  it('shows the timeline and requester details when verified', async () => {
    const d = await openDetail(req(), [{ id: 'e1', eventType: 4, actorKind: 4, actorId: null, fromStatus: 2, toStatus: 3, detail: null, createdAt: '2026-11-01T10:05:00Z' }]);
    expect(within(d).getByTestId('timeline').textContent).toMatch(/Verified/);
    expect(within(d).getByText('req@example.test')).toBeTruthy();
  });

  it('changes status', async () => {
    api.update.mockResolvedValue(req({ status: 5 }));
    const d = await openDetail(req());
    fireEvent.change(within(d).getByLabelText('Set status'), { target: { value: '5' } });
    fireEvent.click(within(d).getByRole('button', { name: 'Update status' }));
    await waitFor(() => expect(api.update).toHaveBeenCalledWith('acme', ID1, { status: 5 }));
  });

  it('assigns and unassigns; validates the UUID', async () => {
    api.update.mockResolvedValue(req());
    const d = await openDetail(req({ assigneeId: USER }));
    fireEvent.change(within(d).getByLabelText(/assign to/i), { target: { value: 'nope' } });
    fireEvent.click(within(d).getByRole('button', { name: 'Assign' }));
    expect(await screen.findByText(/must be a user uuid/i)).toBeTruthy();
    expect(api.update).not.toHaveBeenCalled();
    fireEvent.change(within(d).getByLabelText(/assign to/i), { target: { value: ID2 } });
    fireEvent.click(within(d).getByRole('button', { name: 'Assign' }));
    await waitFor(() => expect(api.update).toHaveBeenCalledWith('acme', ID1, { assigneeId: ID2 }));
    fireEvent.click(within(d).getByRole('button', { name: 'Unassign' }));
    await waitFor(() => expect(api.update).toHaveBeenCalledWith('acme', ID1, { clearAssignee: true }));
  });

  it('saves notes', async () => {
    api.update.mockResolvedValue(req());
    const d = await openDetail(req());
    fireEvent.change(within(d).getByLabelText('Notes'), { target: { value: 'done soon' } });
    fireEvent.click(within(d).getByRole('button', { name: 'Save notes' }));
    await waitFor(() => expect(api.update).toHaveBeenCalledWith('acme', ID1, { responseNotes: 'done soon' }));
  });

  it('extends once: reason required, then blocked after', async () => {
    api.extend.mockResolvedValue(req({ extendedUntil: '2027-01-01T00:00:00Z' }));
    const d = await openDetail(req());
    const btn = within(d).getByRole('button', { name: 'Extend deadline' });
    expect(btn.disabled).toBe(true);
    fireEvent.change(within(d).getByLabelText(/extension reason/i), { target: { value: 'complex' } });
    expect(btn.disabled).toBe(false);
    api.get.mockResolvedValue({ request: req({ extendedUntil: '2027-01-01T00:00:00Z', extensionReason: 'complex' }), events: [] });
    fireEvent.click(btn);
    await waitFor(() => expect(api.extend).toHaveBeenCalledWith('acme', ID1, 'complex'));
    expect(await screen.findByTestId('extend-blocked')).toBeTruthy();
  });

  it('shows the already-extended server error', async () => {
    api.extend.mockRejectedValue(err(409, 'DSAR_ALREADY_EXTENDED'));
    const d = await openDetail(req());
    fireEvent.change(within(d).getByLabelText(/extension reason/i), { target: { value: 'again' } });
    fireEvent.click(within(d).getByRole('button', { name: 'Extend deadline' }));
    expect((await screen.findByTestId('detail-problem')).textContent).toMatch(/already been extended/);
  });

  it('erase needs the typed confirmation then sends confirm=true', async () => {
    api.act.mockResolvedValue({ request: req({ status: 5 }), result: { userId: '123e4567-e89b-12d3-a456-426614174000', status: 'completed' } });
    const d = await openDetail(req({ requestType: 2 }));
    const btn = within(d).getByRole('button', { name: 'Erase user data' });
    expect(btn.disabled).toBe(true);
    fireEvent.change(within(d).getByLabelText(/type "erase"/i), { target: { value: 'ERASE' } });
    fireEvent.click(btn);
    await waitFor(() => expect(api.act).toHaveBeenCalledWith('acme', ID1, 'erase', true));
  });

  it('export act sends confirm=false', async () => {
    api.act.mockResolvedValue({ request: req({ status: 4 }), result: { userId: '123e4567-e89b-12d3-a456-426614174000', status: 'completed' } });
    const d = await openDetail(req());
    fireEvent.click(within(d).getByRole('button', { name: 'Run export' }));
    await waitFor(() => expect(api.act).toHaveBeenCalledWith('acme', ID1, 'export', false));
  });

  it('act is disabled with an explanation while unverified; contact is hidden', async () => {
    const d = await openDetail(req({ status: 2, unverified: true, requesterEmail: null, requesterMessage: null, dueState: null, dueAt: null, effectiveDueAt: null }));
    expect(within(d).queryByRole('button', { name: /run export|erase user data/i })).toBeNull();
    expect(within(d).getByTestId('act-blocked').textContent).toMatch(/not verified/);
    expect(within(d).getByTestId('unverified-note')).toBeTruthy();
    // in_progress / completed are not offered for unverified requests
    const options = Array.from(within(d).getByLabelText('Set status').querySelectorAll('option')).map((o) => o.textContent);
    expect(options).toEqual(['Rejected', 'Withdrawn']);
  });

  it('closed requests are read-only', async () => {
    const d = await openDetail(req({ status: 5, dueState: null }));
    expect(within(d).getByTestId('act-blocked').textContent).toMatch(/closed/);
    expect(within(d).getByTestId('closed-note')).toBeTruthy();
    expect(within(d).getByRole('button', { name: 'Save notes' }).disabled).toBe(true);
  });

  it('409 DSAR_UNVERIFIED from the server shows its message and refreshes', async () => {
    api.update.mockRejectedValue(err(409, 'DSAR_UNVERIFIED'));
    const d = await openDetail(req());
    fireEvent.click(within(d).getByRole('button', { name: 'Update status' }));
    expect((await screen.findByTestId('detail-problem')).textContent).toMatch(/not verified/);
    expect(api.get.mock.calls.length).toBeGreaterThan(1);
  });

  it('409 DSAR_REQUEST_CLOSED shows its message', async () => {
    api.act.mockRejectedValue(err(409, 'DSAR_REQUEST_CLOSED'));
    const d = await openDetail(req());
    fireEvent.click(within(d).getByRole('button', { name: 'Run export' }));
    expect((await screen.findByTestId('detail-problem')).textContent).toMatch(/already closed/);
  });

  const U = '123e4567-e89b-12d3-a456-426614174000';
  it.each([
    ['not_found', 'No such user'],
    ['failed', 'boom'],
    ['forbidden', 'nope'],
    ['conflict', 'busy'],
    ['audit_unavailable', 'audit down'],
  ])('act result %s is shown as a failure, never "Action completed"', async (status, detail) => {
    api.act.mockResolvedValue({ request: req(), result: { userId: U, status, detail } });
    const d = await openDetail(req());
    fireEvent.click(within(d).getByRole('button', { name: 'Run export' }));
    const problem = await screen.findByTestId('detail-problem');
    expect(problem.textContent).toContain(detail);
    expect(problem.textContent).toMatch(/did not complete/);
    expect(screen.queryByTestId('detail-message')).toBeNull();
    expect(within(screen.getByTestId('act-result')).getByTestId(`privacy-result-${U}`)).toBeTruthy();
  });

  it('act success shows the real status and refreshes the request', async () => {
    api.act.mockResolvedValue({ request: req(), result: { userId: U, status: 'already_done', detail: null } });
    const d = await openDetail(req({ requestType: 3 }));
    const before = api.get.mock.calls.length;
    fireEvent.click(within(d).getByRole('button', { name: 'Record do-not-sell' }));
    expect((await screen.findByTestId('detail-message')).textContent).toMatch(/Action completed/);
    expect(screen.getByTestId('act-result').textContent).toMatch(/Already done/);
    expect(api.get.mock.calls.length).toBeGreaterThan(before);
  });

  it('export act offers the returned payload as a download', async () => {
    api.act.mockResolvedValue({
      request: req(),
      result: { userId: U, status: 'completed', detail: null, data: { profile: [{ a: 1 }] }, incomplete: null },
    });
    const create = vi.fn(() => 'blob:x');
    globalThis.URL.createObjectURL = create;
    globalThis.URL.revokeObjectURL = vi.fn();
    const d = await openDetail(req());
    fireEvent.click(within(d).getByRole('button', { name: 'Run export' }));
    const btn = await screen.findByRole('button', { name: /download export for user/i });
    fireEvent.click(btn);
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('unverified request never submits forbidden status 4/5', async () => {
    api.update.mockResolvedValue(req({ status: 6 }));
    const d = await openDetail(req({ status: 2, unverified: true }));
    fireEvent.click(within(d).getByRole('button', { name: 'Update status' }));
    await waitFor(() => expect(api.update).toHaveBeenCalledWith('acme', ID1, { status: 6 }));
  });
});

describe('Tenant switch state bleed', () => {
  it('RunTool drops runs and pending erasure when the tenant slug changes', async () => {
    const RunTool = (await import('../privacy/RunTool')).default;
    const { tenantPrivacyApi } = await import('../../../services/tenantPrivacyApi');
    const exp = vi.spyOn(tenantPrivacyApi, 'exportUser').mockResolvedValue({
      userId: '123e4567-e89b-12d3-a456-426614174000', status: 'completed', detail: null, data: { p: [{}] }, incomplete: null,
    });
    const { rerender } = render(<RunTool tenantSlug="acme" bulk={false} onUpgrade={() => {}} />);
    fireEvent.change(screen.getByLabelText(/user id/i), { target: { value: '123e4567-e89b-12d3-a456-426614174000' } });
    fireEvent.click(screen.getByRole('button', { name: /run export request/i }));
    expect(await screen.findByText(/download export for user/i)).toBeTruthy();
    rerender(<RunTool tenantSlug="other" bulk={false} onUpgrade={() => {}} />);
    await waitFor(() => expect(screen.queryByText(/download export for user/i)).toBeNull());
    expect(screen.getByTestId('privacy-empty')).toBeTruthy();
    exp.mockRestore();
  });
});
