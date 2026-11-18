/** Tenant settings page: load, edit + save, add key validation, error surfacing. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../services/api', () => ({
  tenantApi: { getSettings: vi.fn(), updateSettings: vi.fn() },
}));
import { tenantApi } from '../../../services/api';
import TenantSettings from '../TenantSettings';

function mount() {
  return render(
    <MemoryRouter initialEntries={['/tenant/acme/settings']}>
      <Routes>
        <Route path="/tenant/:tenantSlug/settings" element={<TenantSettings />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  tenantApi.getSettings.mockResolvedValue({ data: { success: true, settings: [{ key: 'theme', value: 'dark' }] } });
  tenantApi.updateSettings.mockResolvedValue({ data: { success: true } });
});

describe('TenantSettings', () => {
  it('loads settings for the URL slug and saves edits', async () => {
    mount();
    const input = await screen.findByLabelText('Value theme');
    expect(tenantApi.getSettings).toHaveBeenCalledWith('acme');
    fireEvent.change(input, { target: { value: 'light' } });
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    await waitFor(() =>
      expect(tenantApi.updateSettings).toHaveBeenCalledWith('acme', [{ key: 'theme', value: 'light' }]),
    );
    expect(await screen.findByText('Tenant settings saved.')).toBeTruthy();
  });

  it('rejects a blank new key without calling the API', async () => {
    mount();
    await screen.findByLabelText('Value theme');
    fireEvent.click(screen.getByRole('button', { name: /add setting/i }));
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    expect(await screen.findByText('Every setting needs a key.')).toBeTruthy();
    expect(tenantApi.updateSettings).not.toHaveBeenCalled();
  });

  it('rejects duplicate keys', async () => {
    mount();
    await screen.findByLabelText('Value theme');
    fireEvent.click(screen.getByRole('button', { name: /add setting/i }));
    fireEvent.change(screen.getByLabelText('New setting key'), { target: { value: 'theme' } });
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    expect(await screen.findByText('Setting keys must be unique.')).toBeTruthy();
  });

  it('shows a permission message on 403 load', async () => {
    tenantApi.getSettings.mockRejectedValue({ response: { status: 403, data: { error: { message: 'nope' } } } });
    mount();
    expect(await screen.findByText(/do not have permission to manage tenant settings/i)).toBeTruthy();
  });

  it('shows the API error when save fails', async () => {
    tenantApi.updateSettings.mockRejectedValue({ response: { status: 500, data: { error: { message: 'boom' } } } });
    mount();
    await screen.findByLabelText('Value theme');
    fireEvent.click(screen.getByRole('button', { name: /save settings/i }));
    expect(await screen.findByText('boom')).toBeTruthy();
  });
});

describe('TenantSettings race', () => {
  it('ignores a stale response from the previous tenant slug', async () => {
    let resolveA;
    tenantApi.getSettings.mockImplementation((slug) =>
      slug === 'a'
        ? new Promise((r) => { resolveA = () => r({ data: { settings: [{ key: 'stale-a', value: '1' }] } }); })
        : Promise.resolve({ data: { settings: [{ key: 'fresh-b', value: '2' }] } }),
    );
    render(
      <MemoryRouter initialEntries={['/tenant/a/settings']}>
        <Link to="/tenant/b/settings">go-b</Link>
        <Routes><Route path="/tenant/:tenantSlug/settings" element={<TenantSettings />} /></Routes>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText('go-b'));
    expect(await screen.findByDisplayValue('2')).toBeTruthy();
    resolveA();
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByDisplayValue('1')).toBeNull();
    expect(screen.getByDisplayValue('2')).toBeTruthy();
  });
});
