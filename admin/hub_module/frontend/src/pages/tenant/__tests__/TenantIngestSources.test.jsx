/**
 * Tests for the tenant ingest-sources page: list/create/update/remove, error + 403,
 * empty list, remove confirm, and that no secret input exists or leaks into payloads.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { tenantApi } from '../../../services/api';
import { tenantIngestSourcesApi, validateIngestSource } from '../../../services/tenantIngestSourcesApi';
import TenantIngestSources from '../TenantIngestSources';

vi.mock('../../../services/api', () => ({ tenantApi: { getCommunities: vi.fn() } }));
vi.mock('../../../services/tenantIngestSourcesApi', async (orig) => {
  const actual = await orig();
  return { ...actual, tenantIngestSourcesApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() } };
});

const api = tenantIngestSourcesApi;
const SRC_UUID = '5b8f0a52-6c1d-4f6e-9a57-0d3b6f1c2e41';
const COMMUNITY_UUID = '9c3d7e10-2b4a-4d8f-8e65-1a7f4b9c0d22';
const SRC = { id: SRC_UUID, platform: 'twitch', sourceId: 'chan_1', label: 'Main chat', enabled: true, communityId: COMMUNITY_UUID, createdAt: '', updatedAt: '' };

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/tenant/acme/ingest-sources']}>
        <Routes><Route path="/tenant/:tenantSlug/ingest-sources" element={<TenantIngestSources />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
const forbidden = { response: { status: 403, data: { error: { message: 'nope' } } } };

beforeEach(() => {
  vi.clearAllMocks();
  tenantApi.getCommunities.mockResolvedValue({ data: { communities: [{ id: 3, publicId: COMMUNITY_UUID, name: 'alpha' }] } });
  api.list.mockResolvedValue({ sources: [SRC], nextCursor: null });
});

describe('TenantIngestSources', () => {
  it('lists sources for the selected community', async () => {
    mount();
    expect(await screen.findByText('Main chat')).toBeTruthy();
    expect(api.list).toHaveBeenCalledWith(COMMUNITY_UUID);
  });

  it('uses the community UUID (never the numeric id) for every request', async () => {
    api.update.mockResolvedValue({ ...SRC, enabled: false });
    mount();
    await screen.findByText('Main chat');
    expect(api.list.mock.calls.every((c) => c[0] === COMMUNITY_UUID)).toBe(true);
    expect(screen.getByLabelText('Community').value).toBe(COMMUNITY_UUID);
    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    await waitFor(() => expect(api.update).toHaveBeenCalled());
    expect(typeof api.update.mock.calls[0][0]).toBe('string');
    expect(api.update.mock.calls[0][0]).not.toBe('3');
  });

  it('shows empty state', async () => {
    api.list.mockResolvedValue({ sources: [], nextCursor: null });
    mount();
    expect(await screen.findByText('No ingest sources')).toBeTruthy();
  });

  it('shows list API error', async () => {
    api.list.mockRejectedValue({ response: { status: 500, data: { error: { message: 'boom' } } } });
    mount();
    expect((await screen.findByTestId('ingest-list-error')).textContent).toContain('boom');
  });

  it('shows 403 message on list', async () => {
    api.list.mockRejectedValue(forbidden);
    mount();
    expect((await screen.findByTestId('ingest-list-error')).textContent).toMatch(/permission/i);
  });

  it('creates a source with only platform/sourceId/label and resets the form', async () => {
    api.create.mockResolvedValue(SRC);
    mount();
    await screen.findByText('Main chat');
    fireEvent.change(screen.getByLabelText(/source id/i), { target: { value: ' chan_2 ' } });
    fireEvent.change(screen.getByLabelText(/^label/i), { target: { value: 'Alt' } });
    fireEvent.click(screen.getByRole('button', { name: /add source/i }));
    await waitFor(() => expect(api.create).toHaveBeenCalled());
    expect(api.create.mock.calls[0][0]).toBe(COMMUNITY_UUID);
    expect(Object.keys(api.create.mock.calls[0][1]).sort()).toEqual(['label', 'platform', 'sourceId']);
    await waitFor(() => expect(screen.getByLabelText(/source id/i).value).toBe(''));
  });

  it('blocks invalid create client-side', async () => {
    mount();
    await screen.findByText('Main chat');
    fireEvent.change(screen.getByLabelText(/source id/i), { target: { value: 'bad id!' } });
    fireEvent.click(screen.getByRole('button', { name: /add source/i }));
    expect(await screen.findByText(/letters, digits/i)).toBeTruthy();
    expect(api.create).not.toHaveBeenCalled();
  });

  it('surfaces create API error and 403', async () => {
    api.create.mockRejectedValue(forbidden);
    mount();
    await screen.findByText('Main chat');
    fireEvent.change(screen.getByLabelText(/source id/i), { target: { value: 'c' } });
    fireEvent.change(screen.getByLabelText(/^label/i), { target: { value: 'L' } });
    fireEvent.click(screen.getByRole('button', { name: /add source/i }));
    expect(await screen.findByText(/permission/i)).toBeTruthy();
  });

  it('toggles enabled via PATCH', async () => {
    api.update.mockResolvedValue({ ...SRC, enabled: false });
    mount();
    await screen.findByText('Main chat');
    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    await waitFor(() => expect(api.update).toHaveBeenCalledWith(COMMUNITY_UUID, SRC_UUID, { enabled: false }));
  });

  it('edits the label via PATCH and surfaces update error', async () => {
    api.update.mockRejectedValue({ response: { status: 500, data: { error: { message: 'upd fail' } } } });
    mount();
    await screen.findByText('Main chat');
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    const row = screen.getByTestId('ingest-source-' + SRC_UUID);
    fireEvent.change(within(row).getByLabelText('Label'), { target: { value: 'New' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.update).toHaveBeenCalledWith(COMMUNITY_UUID, SRC_UUID, { label: 'New' }));
    expect(await screen.findByText('upd fail')).toBeTruthy();
  });

  it('requires confirm before DELETE and can cancel', async () => {
    api.remove.mockResolvedValue(undefined);
    mount();
    await screen.findByText('Main chat');
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    expect(api.remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    fireEvent.click(screen.getByRole('button', { name: /confirm remove/i }));
    await waitFor(() => expect(api.remove).toHaveBeenCalledWith(COMMUNITY_UUID, SRC_UUID));
  });

  it('surfaces remove error', async () => {
    api.remove.mockRejectedValue(forbidden);
    mount();
    await screen.findByText('Main chat');
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    fireEvent.click(screen.getByRole('button', { name: /confirm remove/i }));
    expect(await screen.findByText(/permission/i)).toBeTruthy();
  });

  it('has no password/secret inputs and never renders secret-like fields', async () => {
    api.list.mockResolvedValue({ sources: [{ ...SRC, token: 'SUPERSECRET' }], nextCursor: null });
    const { container } = mount();
    await screen.findByText('Main chat');
    expect(container.querySelector('input[type="password"]')).toBeNull();
    expect(container.textContent).not.toContain('SUPERSECRET');
  });

  it('shows communities load error', async () => {
    tenantApi.getCommunities.mockRejectedValue(forbidden);
    mount();
    expect((await screen.findByTestId('ingest-communities-error')).textContent).toMatch(/permission/i);
  });
});

describe('validateIngestSource', () => {
  it('accepts valid input', () => {
    expect(validateIngestSource({ platform: 'kick', sourceId: 'a.b:c-d_1', label: 'x' })).toEqual({});
  });
  it('rejects bad platform, id, and label', () => {
    const e = validateIngestSource({ platform: 'irc', sourceId: '', label: ' ' });
    expect(Object.keys(e).sort()).toEqual(['label', 'platform', 'sourceId']);
    expect(validateIngestSource({ platform: 'kick', sourceId: 'a', label: 'x'.repeat(256) }).label).toBeTruthy();
  });
});
