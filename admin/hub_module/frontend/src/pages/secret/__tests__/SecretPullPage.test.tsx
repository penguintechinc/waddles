import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import SecretPullPage from '../SecretPullPage';
import { useFeatureFlag } from '../../../lib/useFeatureFlag';
import { useAuth } from '../../../contexts/AuthContext';
import { oneTimeSecretApi } from '../../../services/oneTimeSecretApi';

vi.mock('../../../lib/useFeatureFlag', () => ({ useFeatureFlag: vi.fn() }));
vi.mock('../../../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../../../services/oneTimeSecretApi', () => ({
  oneTimeSecretApi: { pull: vi.fn() },
  pullErrorStatus: (e: { response?: { status?: number } }) => e?.response?.status,
}));

function setup() {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SecretPullPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('SecretPullPage', () => {
  beforeEach(() => {
    vi.mocked(useFeatureFlag).mockReturnValue(true);
    vi.mocked(useAuth).mockReturnValue({ user: { id: 'u' }, loading: false } as never);
    window.history.replaceState(null, '', '/secret#tok123');
  });

  it('does not pull on load, clears the fragment, pulls on click', async () => {
    vi.mocked(oneTimeSecretApi.pull).mockResolvedValue('hunter2');
    setup();
    await screen.findByText('Reveal secret');
    expect(window.location.hash).toBe('');
    expect(oneTimeSecretApi.pull).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Reveal secret'));
    expect(await screen.findByTestId('secret-value')).toHaveTextContent('hunter2');
    expect(oneTimeSecretApi.pull).toHaveBeenCalledWith('tok123');
    expect(window.localStorage.length).toBe(0);
  });

  it.each([
    [403, 'not the intended recipient'],
    [410, 'already viewed or has expired'],
  ])('shows message for %i', async (status, text) => {
    vi.mocked(oneTimeSecretApi.pull).mockRejectedValue({ response: { status } });
    setup();
    fireEvent.click(await screen.findByText('Reveal secret'));
    await waitFor(() => expect(screen.getByTestId('secret-error')).toHaveTextContent(text));
  });

  it('shows network error with retry', async () => {
    vi.mocked(oneTimeSecretApi.pull).mockRejectedValue(new Error('net'));
    setup();
    fireEvent.click(await screen.findByText('Reveal secret'));
    expect(await screen.findByText('Retry')).toBeInTheDocument();
  });

  it('is hidden when the flag is off', () => {
    vi.mocked(useFeatureFlag).mockReturnValue(false);
    setup();
    expect(screen.getByTestId('secret-disabled')).toBeInTheDocument();
  });

  it('prompts login and keeps the fragment when logged out', () => {
    vi.mocked(useAuth).mockReturnValue({ user: null, loading: false } as never);
    setup();
    expect(screen.getByTestId('secret-login')).toBeInTheDocument();
    expect(window.location.hash).toBe('#tok123');
  });
});
