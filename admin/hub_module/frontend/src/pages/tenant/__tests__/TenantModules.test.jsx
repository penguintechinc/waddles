/**
 * Tests for the tenant Module Permissions page: allow-all vs allowlist mode,
 * category grouping, per-module toggles, selection summary, dirty-gated save
 * payload (null vs id list) and error/success states.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import TenantModules from '../TenantModules';
import { tenantApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  tenantApi: { getModules: vi.fn(), updateModules: vi.fn() },
}));

const MODULES = [
  { id: 1, name: 'ping', displayName: 'Ping', description: 'Replies pong', category: 'Utility', isCore: true },
  { id: 2, name: 'karaoke', displayName: 'Karaoke', category: 'Fun' },
  { id: 3, name: 'raw-name', description: undefined },
];

function mount() {
  return render(
    <MemoryRouter initialEntries={['/tenant/acme/modules']}>
      <Routes>
        <Route path="/tenant/:tenantSlug/modules" element={<TenantModules />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function loaded(allowedModuleIds = null) {
  tenantApi.getModules.mockResolvedValue({ data: { modules: MODULES, allowedModuleIds } });
  const view = mount();
  await screen.findByText('Module Permissions');
  return view;
}

const allowAllBox = () => screen.getByRole('checkbox', { name: /Allow All Modules/ });
const moduleBox = (name) => screen.getByRole('checkbox', { name: new RegExp(name) });
const save = () => screen.getByRole('button', { name: /Save Changes|Saving/ });

beforeEach(() => {
  vi.clearAllMocks();
  tenantApi.updateModules.mockResolvedValue({ data: { success: true } });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('TenantModules loading', () => {
  it('shows a spinner then loads modules for the tenant slug', async () => {
    tenantApi.getModules.mockResolvedValue({ data: { modules: MODULES, allowedModuleIds: null } });
    const { container } = mount();
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();
    await screen.findByText('Module Permissions');
    expect(tenantApi.getModules).toHaveBeenCalledWith('acme');
  });

  it.each([
    ['the server error string', { response: { data: { error: 'tenant gone' } } }, 'tenant gone'],
    ['a generic fallback', new Error('x'), 'Failed to load modules.'],
  ])('shows %s when loading fails', async (_label, err, text) => {
    tenantApi.getModules.mockRejectedValue(err);
    mount();
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('defaults to allow-all (hiding the module list) when no allowlist exists', async () => {
    await loaded(null);
    expect(allowAllBox()).toBeChecked();
    expect(screen.getByText(/All current and future modules/)).toBeInTheDocument();
    expect(screen.queryByText('Karaoke')).not.toBeInTheDocument();
    expect(save()).toBeDisabled();
  });

  it('treats an undefined allowlist like allow-all too', async () => {
    tenantApi.getModules.mockResolvedValue({ data: {} });
    mount();
    await screen.findByText('Module Permissions');
    expect(allowAllBox()).toBeChecked();
  });

  it('shows the bundle-apps follow-up placeholder', async () => {
    await loaded();
    expect(screen.getByTestId('tenant-bundle-apps-followup')).toHaveTextContent('Bundle Apps (Coming Soon)');
  });
});

describe('TenantModules allowlist mode', () => {
  it('groups modules by category and ticks only the allowed ones', async () => {
    await loaded([1]);
    expect(allowAllBox()).not.toBeChecked();
    expect(screen.getByText('Utility')).toBeInTheDocument();
    expect(screen.getByText('Fun')).toBeInTheDocument();
    expect(screen.getByText('General')).toBeInTheDocument();
    expect(moduleBox('Ping')).toBeChecked();
    expect(moduleBox('Karaoke')).not.toBeChecked();
    expect(moduleBox('raw-name')).not.toBeChecked();
    expect(screen.getByText('Replies pong')).toBeInTheDocument();
    expect(screen.getByText('Core')).toBeInTheDocument();
    expect(screen.getByText('1 of 3 modules selected')).toBeInTheDocument();
  });

  it('shows an empty-catalogue message when there are no modules', async () => {
    tenantApi.getModules.mockResolvedValue({ data: { modules: [], allowedModuleIds: [] } });
    mount();
    expect(await screen.findByText('No modules available.')).toBeInTheDocument();
    expect(screen.queryByText(/modules selected/)).not.toBeInTheDocument();
  });

  it('toggling a module updates the summary and enables Save', async () => {
    await loaded([1]);
    fireEvent.click(moduleBox('Karaoke'));
    expect(moduleBox('Karaoke')).toBeChecked();
    expect(screen.getByText('2 of 3 modules selected')).toBeInTheDocument();
    expect(save()).toBeEnabled();

    fireEvent.click(moduleBox('Ping'));
    expect(screen.getByText('1 of 3 modules selected')).toBeInTheDocument();
  });

  it('saves the chosen ids', async () => {
    await loaded([1]);
    fireEvent.click(moduleBox('Karaoke'));
    fireEvent.click(save());

    expect(await screen.findByText('Module permissions saved successfully.')).toBeInTheDocument();
    expect(tenantApi.updateModules).toHaveBeenCalledWith('acme', [1, 2]);
    expect(save()).toBeDisabled();
  });
});

describe('TenantModules allow-all toggle', () => {
  it('switching to manual mode pre-selects every module', async () => {
    await loaded(null);
    fireEvent.click(allowAllBox());
    expect(screen.getByText('3 of 3 modules selected')).toBeInTheDocument();
    expect(screen.getByText(/Only selected modules/)).toBeInTheDocument();
    expect(save()).toBeEnabled();
  });

  it('switching back to allow-all saves a null allowlist', async () => {
    await loaded([1]);
    fireEvent.click(allowAllBox());
    fireEvent.click(save());
    await waitFor(() => expect(tenantApi.updateModules).toHaveBeenCalledWith('acme', null));
  });

  it('toggling modules from a null allowlist starts a fresh selection', async () => {
    await loaded(null);
    fireEvent.click(allowAllBox());
    fireEvent.click(moduleBox('Ping'));
    expect(screen.getByText('2 of 3 modules selected')).toBeInTheDocument();
  });
});

describe('TenantModules save feedback', () => {
  it.each([
    ['the server error string', { response: { data: { error: 'not allowed' } } }, 'not allowed'],
    ['a generic fallback', new Error('x'), 'Failed to save module settings.'],
  ])('shows %s when saving fails and stays dirty', async (_label, err, text) => {
    tenantApi.updateModules.mockRejectedValue(err);
    await loaded([1]);
    fireEvent.click(moduleBox('Karaoke'));
    fireEvent.click(save());
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(save()).toBeEnabled();
  });

  it('shows Saving... while in flight', async () => {
    let release;
    tenantApi.updateModules.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await loaded([1]);
    fireEvent.click(moduleBox('Karaoke'));
    fireEvent.click(save());
    expect(await screen.findByRole('button', { name: 'Saving...' })).toBeDisabled();
    await act(async () => release({ data: {} }));
    await screen.findByText('Module permissions saved successfully.');
  });

  it('clears the success banner after 3 seconds, or as soon as the user edits again', async () => {
    await loaded([1]);
    fireEvent.click(moduleBox('Karaoke'));
    vi.useFakeTimers({ toFake: ['setTimeout'] });
    await act(async () => {
      fireEvent.click(save());
    });
    expect(screen.getByText('Module permissions saved successfully.')).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.queryByText('Module permissions saved successfully.')).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(moduleBox('Ping'));
      fireEvent.click(save());
    });
    expect(screen.getByText('Module permissions saved successfully.')).toBeInTheDocument();
    fireEvent.click(moduleBox('Ping'));
    expect(screen.queryByText('Module permissions saved successfully.')).not.toBeInTheDocument();
  });

  it('clears the success banner when the allow-all toggle is changed', async () => {
    await loaded([1]);
    fireEvent.click(moduleBox('Karaoke'));
    fireEvent.click(save());
    await screen.findByText('Module permissions saved successfully.');
    fireEvent.click(allowAllBox());
    expect(screen.queryByText('Module permissions saved successfully.')).not.toBeInTheDocument();
  });
});

describe('TenantModules row rendering', () => {
  it('falls back to the machine name when there is no display name', async () => {
    await loaded([]);
    const row = within(screen.getByText('raw-name').closest('label'));
    expect(row.getByRole('checkbox')).not.toBeChecked();
  });
});
