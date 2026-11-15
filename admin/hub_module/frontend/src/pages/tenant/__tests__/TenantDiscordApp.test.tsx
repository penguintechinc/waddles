/**
 * Tests for the tenant "Discord app (your own bot)" page: write-only secret
 * inputs, authorize payload + redirect, error surfacing, status, default-tenant gate.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { parseInstallStatus, tenantDiscordInstallApi } from '../../../services/tenantDiscordInstallApi';
import TenantDiscordApp from '../TenantDiscordApp';

vi.mock('../../../services/tenantDiscordInstallApi', async (orig) => {
  const actual = await orig<typeof import('../../../services/tenantDiscordInstallApi')>();
  return { ...actual, tenantDiscordInstallApi: { authorize: vi.fn(), getStatus: vi.fn() } };
});

const api = vi.mocked(tenantDiscordInstallApi);
const assign = vi.fn();

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function mount(path = '/tenant/acme/discord-app') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/tenant/:tenantSlug/discord-app" element={<TenantDiscordApp />} />
      </Routes>
    </MemoryRouter>,
    { wrapper },
  );
}

const field = (label: string) => screen.getByLabelText(new RegExp(label, 'i')) as HTMLInputElement;

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('location', { ...window.location, origin: 'https://hub.example.test', assign });
  api.getStatus.mockResolvedValue({
    installed: false, applicationId: null, connectionStatus: null, updatedAt: null, redirectUri: null,
  });
});
afterEach(() => vi.unstubAllGlobals());

describe('TenantDiscordApp', () => {
  it('renders fields with secrets as empty password inputs', async () => {
    mount();
    expect(field('Application ID').type).toBe('text');
    expect(field('Client secret').type).toBe('password');
    expect(field('Bot token').type).toBe('password');
    for (const l of ['Application ID', 'Client secret', 'Bot token']) expect(field(l).value).toBe('');
    await waitFor(() => expect(api.getStatus).toHaveBeenCalled());
  });

  it('shows the computed redirect URI', async () => {
    mount();
    expect((await screen.findByTestId('discord-redirect-uri')).textContent).toContain(
      'https://hub.example.test/api/v1/discord/install/callback',
    );
  });

  it('prefers the API-supplied redirect URI', async () => {
    api.getStatus.mockResolvedValue({
      installed: false, applicationId: null, connectionStatus: null, updatedAt: null, redirectUri: 'https://api.x/cb',
    });
    mount();
    await waitFor(() => expect(screen.getByTestId('discord-redirect-uri').textContent).toContain('https://api.x/cb'));
  });

  it('submits the payload, redirects, and clears secrets', async () => {
    api.authorize.mockResolvedValue('https://discord.com/oauth2/authorize?x=1');
    mount();
    fireEvent.change(field('Application ID'), { target: { value: ' 123 ' } });
    fireEvent.change(field('Client secret'), { target: { value: 'sek' } });
    fireEvent.change(field('Bot token'), { target: { value: 'tok' } });
    fireEvent.click(screen.getByRole('button', { name: /install to discord/i }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://discord.com/oauth2/authorize?x=1'));
    expect(api.authorize).toHaveBeenCalledWith({ applicationId: '123', clientSecret: 'sek', botToken: 'tok' });
    await waitFor(() => expect(field('Client secret').value).toBe(''));
    expect(field('Bot token').value).toBe('');
  });

  it('sends a null bot token when blank', async () => {
    api.authorize.mockResolvedValue('https://d/x');
    mount();
    fireEvent.change(field('Application ID'), { target: { value: '9' } });
    fireEvent.change(field('Client secret'), { target: { value: 's' } });
    fireEvent.click(screen.getByRole('button', { name: /install to discord/i }));
    await waitFor(() => expect(api.authorize).toHaveBeenCalledWith({ applicationId: '9', clientSecret: 's', botToken: null }));
  });

  it('does not submit without required fields', () => {
    mount();
    fireEvent.submit(screen.getByRole('button', { name: /install to discord/i }).closest('form')!);
    expect(api.authorize).not.toHaveBeenCalled();
  });

  it('shows API errors and clears secrets', async () => {
    api.authorize.mockRejectedValue({ response: { data: { error: { code: 'BAD', message: 'Discord said no' } } } });
    mount();
    fireEvent.change(field('Application ID'), { target: { value: '1' } });
    fireEvent.change(field('Client secret'), { target: { value: 's' } });
    fireEvent.click(screen.getByRole('button', { name: /install to discord/i }));
    expect((await screen.findByTestId('discord-install-error')).textContent).toContain('Discord said no');
    expect(field('Client secret').value).toBe('');
    expect(assign).not.toHaveBeenCalled();
  });

  it('explains TENANT_DISCORD_APP_MISSING', async () => {
    api.authorize.mockRejectedValue({ response: { data: { error: { code: 'TENANT_DISCORD_APP_MISSING', message: 'x' } } } });
    mount();
    fireEvent.change(field('Application ID'), { target: { value: '1' } });
    fireEvent.change(field('Client secret'), { target: { value: 's' } });
    fireEvent.click(screen.getByRole('button', { name: /install to discord/i }));
    expect((await screen.findByTestId('discord-install-error')).textContent).toMatch(/no discord app is configured/i);
  });

  it('renders installed status', async () => {
    api.getStatus.mockResolvedValue({
      installed: true, applicationId: '555', connectionStatus: 'connected', updatedAt: '2026-11-12T00:00:00Z', redirectUri: null,
    });
    mount();
    const box = await screen.findByTestId('discord-status');
    expect(box.textContent).toContain('Installed');
    expect(box.textContent).toContain('555');
    expect(box.textContent).toContain('connected');
  });

  it('shows a status load error', async () => {
    api.getStatus.mockRejectedValue({ response: { data: { error: { message: 'status boom' } } } });
    mount();
    expect((await screen.findByTestId('discord-status-error')).textContent).toContain('status boom');
  });

  it('is replaced by a notice for the default tenant', () => {
    mount('/tenant/default/discord-app');
    expect(screen.getByText(/default tenant uses the shared Waddles Discord app/i)).toBeTruthy();
    expect(screen.queryByLabelText(/client secret/i)).toBeNull();
    expect(api.getStatus).not.toHaveBeenCalled();
  });
});

describe('parseInstallStatus', () => {
  it('parses snake_case, camelCase and junk', () => {
    expect(parseInstallStatus({ installed: true, application_id: 'a', connection_status: 'ok', updated_at: 't' }))
      .toMatchObject({ installed: true, applicationId: 'a', connectionStatus: 'ok', updatedAt: 't' });
    expect(parseInstallStatus({ data: { applicationId: 'b' } }).applicationId).toBe('b');
    expect(parseInstallStatus(null).installed).toBe(false);
  });
});
