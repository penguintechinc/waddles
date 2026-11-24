/** DSAR inbox: subject link/lock, email-mismatch override, conflict, partial export, extension, expired. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../services/tenantDsarInboxApi', () => ({
  tenantDsarInboxApi: { list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), extend: vi.fn(), act: vi.fn() },
}));
import { tenantDsarInboxApi as api } from '../../../services/tenantDsarInboxApi';
import TenantPrivacy from '../TenantPrivacy';

const ID1 = '11111111-1111-4111-8111-111111111111';
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

beforeEach(() => vi.clearAllMocks());

async function openDetail(request, events = []) {
  api.list.mockResolvedValue(page([request]));
  api.get.mockResolvedValue({ request, events });
  mount();
  fireEvent.click(await screen.findByRole('button', { name: /^Open/ }));
  return screen.findByTestId('request-detail');
}

const U = '123e4567-e89b-12d3-a456-426614174000';
const OTHER = '44444444-4444-4444-8444-444444444444';

describe('Linked user (subject)', () => {
  it('links an unlinked subject without asking for a reason', async () => {
    api.update.mockResolvedValue(req());
    const d = await openDetail(req({ subjectUserId: null, source: 3 }));
    expect(within(d).queryByLabelText(/reason for changing/i)).toBeNull();
    fireEvent.change(within(d).getByLabelText('User to link (UUID)'), { target: { value: U } });
    fireEvent.click(within(d).getByRole('button', { name: 'Link user' }));
    await waitFor(() => expect(api.update).toHaveBeenCalledWith('acme', ID1, { subjectUserId: U }));
  });

  it('changing an already-linked subject requires a reason and sends it', async () => {
    api.update.mockResolvedValue(req());
    const d = await openDetail(req({ source: 3 }));
    fireEvent.change(within(d).getByLabelText('New linked user (UUID)'), { target: { value: OTHER } });
    const btn = within(d).getByRole('button', { name: 'Change linked user' });
    expect(btn.disabled).toBe(true);
    fireEvent.change(within(d).getByLabelText(/reason for changing/i), { target: { value: 'wrong account' } });
    expect(btn.disabled).toBe(false);
    fireEvent.click(btn);
    await waitFor(() =>
      expect(api.update).toHaveBeenCalledWith('acme', ID1, { subjectUserId: OTHER, subjectOverrideReason: 'wrong account' }),
    );
  });

  it('rejects a non-UUID subject without calling the API', async () => {
    const d = await openDetail(req({ subjectUserId: null, source: 3 }));
    fireEvent.change(within(d).getByLabelText('User to link (UUID)'), { target: { value: 'nope' } });
    expect(within(d).getByRole('button', { name: 'Link user' }).disabled).toBe(true);
    expect(api.update).not.toHaveBeenCalled();
  });

  it('a signed-in request shows the lock note and no link control', async () => {
    const d = await openDetail(req({ source: 1 }));
    expect(within(d).getByTestId('subject-locked').textContent).toMatch(/fixed and cannot be changed/);
    expect(within(d).queryByLabelText(/linked user \(uuid\)/i)).toBeNull();
  });

  it('409 DSAR_SUBJECT_LOCKED explains it cannot be changed', async () => {
    api.update.mockRejectedValue(err(409, 'DSAR_SUBJECT_LOCKED'));
    const d = await openDetail(req({ subjectUserId: null, source: 3 }));
    fireEvent.change(within(d).getByLabelText('User to link (UUID)'), { target: { value: U } });
    fireEvent.click(within(d).getByRole('button', { name: 'Link user' }));
    expect((await screen.findByTestId('detail-problem')).textContent).toMatch(/cannot be changed/);
  });

  it('409 DSAR_SUBJECT_OVERRIDE_REQUIRED reloads so the reason field appears', async () => {
    api.update.mockRejectedValue(err(409, 'DSAR_SUBJECT_OVERRIDE_REQUIRED'));
    const d = await openDetail(req({ subjectUserId: null, source: 3 }));
    api.get.mockResolvedValue({ request: req({ source: 3 }), events: [] });
    fireEvent.change(within(d).getByLabelText('User to link (UUID)'), { target: { value: U } });
    fireEvent.click(within(d).getByRole('button', { name: 'Link user' }));
    expect((await screen.findByTestId('detail-problem')).textContent).toMatch(/needs a reason/);
    expect(await screen.findByLabelText(/reason for changing/i)).toBeTruthy();
  });
});

describe('Erase email mismatch override', () => {
  async function mismatched() {
    api.act.mockRejectedValueOnce(err(422, 'DSAR_SUBJECT_EMAIL_MISMATCH'));
    const d = await openDetail(req({ requestType: 2 }));
    fireEvent.change(within(d).getByLabelText(/type "erase"/i), { target: { value: 'ERASE' } });
    fireEvent.click(within(d).getByRole('button', { name: 'Erase user data' }));
    await screen.findByTestId('override-panel');
    return d;
  }

  it('explains the mismatch, shows no email in the message or panel, and gates the override', async () => {
    const d = await mismatched();
    const text = screen.getByTestId('detail-problem').textContent;
    expect(text).toMatch(/does not match the linked account/);
    expect(text + screen.getByTestId('override-panel').textContent).not.toMatch(/@/);
    const btn = within(d).getByRole('button', { name: 'Erase with administrator override' });
    expect(btn.disabled).toBe(true);
    fireEvent.change(within(d).getByLabelText('Override reason'), { target: { value: 'ID checked by phone' } });
    expect(btn.disabled).toBe(true); // typed confirm still missing
    fireEvent.change(within(d).getByLabelText(/type "override"/i), { target: { value: 'OVERRIDE' } });
    expect(btn.disabled).toBe(false);
  });

  it('sends overrideReason with confirm=true', async () => {
    const d = await mismatched();
    api.act.mockResolvedValueOnce({ request: req({ status: 5 }), result: { userId: U, status: 'completed' } });
    fireEvent.change(within(d).getByLabelText('Override reason'), { target: { value: 'ID checked by phone' } });
    fireEvent.change(within(d).getByLabelText(/type "override"/i), { target: { value: 'OVERRIDE' } });
    fireEvent.click(within(d).getByRole('button', { name: 'Erase with administrator override' }));
    await waitFor(() => expect(api.act).toHaveBeenLastCalledWith('acme', ID1, 'erase', true, 'ID checked by phone'));
  });

  it('never sends overrideReason on the normal path', async () => {
    api.act.mockResolvedValue({ request: req({ status: 5 }), result: { userId: U, status: 'completed' } });
    const d = await openDetail(req({ requestType: 2 }));
    fireEvent.change(within(d).getByLabelText(/type "erase"/i), { target: { value: 'ERASE' } });
    fireEvent.click(within(d).getByRole('button', { name: 'Erase user data' }));
    await waitFor(() => expect(api.act).toHaveBeenCalledWith('acme', ID1, 'erase', true));
    expect(screen.queryByTestId('override-panel')).toBeNull();
  });
});

describe('409 DSAR_CONFLICT', () => {
  it('reloads, tells the admin, clears pending confirmations and does not retry', async () => {
    api.act.mockRejectedValue(err(409, 'DSAR_CONFLICT'));
    const d = await openDetail(req({ requestType: 2 }));
    const loadsBefore = api.get.mock.calls.length;
    fireEvent.change(within(d).getByLabelText(/type "erase"/i), { target: { value: 'ERASE' } });
    fireEvent.click(within(d).getByRole('button', { name: 'Erase user data' }));
    expect((await screen.findByTestId('detail-problem')).textContent).toMatch(/Someone else changed or is acting/);
    await waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThan(loadsBefore));
    expect(api.act).toHaveBeenCalledTimes(1);
    expect(within(d).getByLabelText(/type "erase"/i).value).toBe('');
    expect(within(d).getByRole('button', { name: 'Erase user data' }).disabled).toBe(true);
  });

  it('applies to a status update too', async () => {
    api.update.mockRejectedValue(err(409, 'DSAR_CONFLICT'));
    const d = await openDetail(req());
    fireEvent.click(within(d).getByRole('button', { name: 'Update status' }));
    expect((await screen.findByTestId('detail-problem')).textContent).toMatch(/reloaded/);
    expect(api.update).toHaveBeenCalledTimes(1);
  });
});

describe('Partial export', () => {
  const partial = { userId: U, status: 'partial', detail: 'Export incomplete', data: { posts: [1] }, incomplete: [{ source: 'messages', reason: 'timeout' }] };

  it('is shown as INCOMPLETE with a warning, never as completed, and the request stays in progress', async () => {
    api.act.mockResolvedValue({ request: req({ status: 4 }), result: partial });
    api.get.mockResolvedValue({ request: req({ status: 4 }), events: [] });
    const d = await openDetail(req());
    fireEvent.click(within(d).getByRole('button', { name: 'Run export' }));
    const warn = await screen.findByTestId('detail-warning');
    expect(warn.textContent).toMatch(/Export incomplete/);
    expect(warn.textContent).toMatch(/messages/);
    expect(warn.textContent).toMatch(/stays in progress/);
    expect(screen.queryByTestId('detail-message')).toBeNull();
    const res = within(screen.getByTestId('act-result'));
    expect(res.getByText('Incomplete')).toBeTruthy();
    expect(res.queryByText('Completed')).toBeNull();
    expect(res.getByText(/1 source\(s\) incomplete/)).toBeTruthy();
    expect(within(d).getByText('In progress')).toBeTruthy();
  });
});

describe('Due dates and expiry', () => {
  it('detail shows extendedUntil', async () => {
    const d = await openDetail(req({ extendedUntil: '2027-01-15T10:00:00Z', effectiveDueAt: '2027-01-15T10:00:00Z', extensionReason: 'statutory_extension' }));
    expect(within(d).getByTestId('extended-until').textContent).not.toBe('—');
  });

  it('list shows extendedUntil next to the due date', async () => {
    api.list.mockResolvedValue(page([req({ extendedUntil: '2027-01-15T10:00:00Z', effectiveDueAt: '2027-01-15T10:00:00Z' })]));
    mount();
    expect((await screen.findByTestId(`extended-${ID1}`)).textContent).toMatch(/^Extended until /);
  });

  it('expired request: expired note, no unverified wording, nothing actionable, no contact data', async () => {
    const d = await openDetail(req({ status: 8, unverified: true, requesterEmail: null, requesterMessage: null, dueState: null, dueAt: null, effectiveDueAt: null }));
    expect(within(d).getByTestId('expired-note').textContent).toMatch(/expired/);
    expect(within(d).queryByTestId('unverified-note')).toBeNull();
    expect(within(d).getByTestId('act-blocked').textContent).toMatch(/closed/);
    expect(within(d).getByTestId('closed-note')).toBeTruthy();
  });

  it('expired row in the list is labelled Expired without the Unverified tag', async () => {
    api.list.mockResolvedValue(page([req({ status: 8, unverified: true, requesterEmail: null, dueState: null, dueAt: null, effectiveDueAt: null })]));
    mount();
    const table = within(await screen.findByTestId('inbox-table'));
    expect(table.getByText('Expired')).toBeTruthy();
    expect(table.queryByText('Unverified')).toBeNull();
  });
});
