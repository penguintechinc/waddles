/** regression: React error #31 -- hub-api's `{ error: { code, message } }` body was rendered as a child. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import CommunityPublicPage from '../CommunityPublicPage';
import { renderWithProviders } from '../../../test/renderWithProviders';

const profile = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: null, isAuthenticated: false }),
}));
vi.mock('../../../services/api', () => ({ publicApi: { getCommunityProfile: profile.get } }));
vi.mock('@penguintechinc/react-libs', () => ({ FormModalBuilder: () => null }));

const mount = () => renderWithProviders(<CommunityPublicPage />, { route: '/communities/abc', path: '/communities/:id' });

beforeEach(() => vi.clearAllMocks());

describe('CommunityPublicPage error body', () => {
  it('renders the message of an { error: { code, message } } body instead of the object', async () => {
    profile.get.mockRejectedValue({
      response: { status: 404, data: { success: false, error: { code: 'NOT_FOUND', message: 'No such community' } } },
    });
    mount();
    expect(await screen.findByText('Community Not Found')).toBeTruthy();
    expect(screen.getByText('No such community')).toBeTruthy();
  });

  it('still renders a plain string error body', async () => {
    profile.get.mockRejectedValue({ response: { status: 404, data: { error: 'Gone' } } });
    mount();
    expect(await screen.findByText('Gone')).toBeTruthy();
  });

  it('keeps the restricted state for 403', async () => {
    profile.get.mockRejectedValue({ response: { status: 403, data: { error: { code: 'X', message: 'no' } } } });
    mount();
    expect(await screen.findByText('Community Restricted')).toBeTruthy();
  });
});
