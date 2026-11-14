/**
 * Town Hall page: the community's persistent room. Loading it (PROVISIONAL GET town-hall), opening it when it does not
 * exist (PROVISIONAL POST), the flag-off (402) / 503-retry / error states, role-based call controls, and the real
 * join flow on fakes. No "media join coming" placeholder remains.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import TownHall from '../TownHall';
import { renderWithProviders } from '../../../test/renderWithProviders';
import { ADA, ROOM_ID, envelope, httpError, participantWire, serve } from '../../../test/callsFixtures';
import { answerResponse, fakeDevices, fakeMediaDeps, resetFakeRtc } from '../../../test/fakeRtc';

const auth = vi.hoisted(() => ({ admin: false }));
vi.mock('../../../contexts/AuthContext', () => ({ useAuth: () => ({ isCommunityAdmin: () => auth.admin }) }));

const TOWN_HALL = '/api/v1/communities/7/town-hall';
const MEMBER = `/api/v1/community/7/interact/voice/rooms/${ROOM_ID}`;
const SELF = '11111111-1111-4111-8111-111111111111';
const hall = { success: true, room_id: ROOM_ID, label: 'Town Hall', participant_count: 2 };
const joinOk = {
  success: true,
  token: 'room-token-secret',
  url: 'https://media.example/rtc/rooms/r',
  whip_url: 'https://media.example/rtc/rooms/r/whip',
  whep_url: 'https://media.example/rtc/rooms/r/whep',
  token_id: 'tok',
  participant_id: SELF,
  expires_at: 4_000_000_000,
};
const roster = { success: true, locked: false, participants: [participantWire(SELF, 'Me Myself'), participantWire(ADA, 'Ada Lovelace')] };

function renderPage(route = '/community/7/town-hall') {
  const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => (init.method === 'DELETE' ? new Response(null, { status: 204 }) : answerResponse('/rtc/rooms/r/sessions/s')));
  return renderWithProviders(
    <TownHall overrides={{ devices: fakeDevices(), media: fakeMediaDeps(fetchImpl as unknown as ReturnType<typeof vi.fn>) }} />,
    { route, path: '/community/:communityId/town-hall' },
  );
}

describe('TownHall', () => {
  beforeEach(() => {
    resetFakeRtc();
    auth.admin = false;
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it('shows the Town Hall by its label, joins with real media, and has no placeholder', async () => {
    serve({
      [`GET ${TOWN_HALL}`]: hall,
      [`POST ${MEMBER}/join`]: joinOk,
      [`GET ${MEMBER}/participants`]: roster,
      [`GET ${MEMBER}/hands/mine`]: { success: true, hand: null },
    });
    const { container } = renderPage();
    expect(await screen.findByRole('heading', { level: 1, name: 'Town Hall' })).toBeInTheDocument();
    const lobby = await screen.findByTestId('call-lobby');
    expect(within(lobby).getByRole('heading', { name: 'Town Hall' })).toBeInTheDocument();
    fireEvent.click(within(lobby).getByRole('button', { name: 'Join with microphone' }));
    const connected = await screen.findByTestId('call-connected');
    expect(await within(connected).findByTestId('roster')).toHaveTextContent('Ada Lovelace');
    expect(container.textContent).not.toMatch(/LiveKit|media join is coming|not available yet/i);
    expect(container.textContent).not.toContain(ROOM_ID);
  });

  it('members get the hand toggle; community admins get moderator controls', async () => {
    serve({
      [`GET ${TOWN_HALL}`]: hall,
      [`POST ${MEMBER}/join`]: joinOk,
      [`GET ${MEMBER}/participants`]: roster,
      [`GET ${MEMBER}/hands/mine`]: { success: true, hand: null },
      [`GET /api/v1/admin/7/calls/rooms/${ROOM_ID}/participants`]: roster,
      [`GET /api/v1/admin/7/calls/rooms/${ROOM_ID}/raised-hands`]: { success: true, count: 0, raised_hands: [] },
    });
    const { unmount } = renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Join with microphone' }));
    expect(await screen.findByRole('button', { name: 'Raise hand' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Mute all' })).not.toBeInTheDocument();
    unmount();

    auth.admin = true;
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Join with microphone' }));
    expect(await screen.findByRole('button', { name: 'Mute all' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Raise hand' })).not.toBeInTheDocument();
  });

  it('when the Town Hall does not exist yet, offers to open it and then shows it', async () => {
    let opened = false;
    const { post } = serve({
      [`GET ${TOWN_HALL}`]: () => (opened ? hall : httpError(404, envelope('NOT_FOUND', 'No Town Hall'))),
      [`POST ${TOWN_HALL}`]: () => {
        opened = true;
        return hall;
      },
    });
    renderPage();
    expect(await screen.findByTestId('town-hall-not-opened')).toHaveTextContent('has not been opened yet');
    fireEvent.click(screen.getByRole('button', { name: 'Open Town Hall' }));
    expect(await screen.findByTestId('call-lobby')).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith(TOWN_HALL);
  });

  it('shows why opening failed and keeps the offer', async () => {
    serve({
      [`GET ${TOWN_HALL}`]: httpError(404, envelope('NOT_FOUND', 'No Town Hall')),
      [`POST ${TOWN_HALL}`]: httpError(403, envelope('FORBIDDEN', 'Only admins can open it')),
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Open Town Hall' }));
    expect(await screen.findByText('Only admins can open it')).toBeInTheDocument();
    expect(screen.getByTestId('town-hall-not-opened')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss message' }));
    await waitFor(() => expect(screen.queryByText('Only admins can open it')).not.toBeInTheDocument());
  });

  describe('failure states', () => {
    it('flag off (402): "not enabled for this community", no retry', async () => {
      serve({ [`GET ${TOWN_HALL}`]: httpError(402, envelope('FEATURE_NOT_ENABLED', 'Calls / RTC is not enabled for this plan')) });
      renderPage();
      expect(await screen.findByTestId('calls-feature-disabled')).toHaveTextContent('Calls are not enabled for this community');
      expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
      expect(screen.queryByTestId('town-hall-not-opened')).not.toBeInTheDocument();
    });

    it('503: shows the server message and a retry that recovers', async () => {
      let healthy = false;
      serve({ [`GET ${TOWN_HALL}`]: () => (healthy ? hall : httpError(503, envelope('UNAVAILABLE', 'Streaming service unavailable', { retryable: true }))) });
      renderPage();
      const state = await screen.findByTestId('calls-unavailable');
      expect(state).toHaveTextContent('Streaming service unavailable');
      healthy = true;
      fireEvent.click(within(state).getByRole('button', { name: 'Try again' }));
      expect(await screen.findByTestId('call-lobby')).toBeInTheDocument();
    });

    it('other errors are error states, never "not opened yet"', async () => {
      serve({ [`GET ${TOWN_HALL}`]: httpError(500, envelope('INTERNAL', 'Something broke')) });
      renderPage();
      expect(await screen.findByTestId('calls-error')).toHaveTextContent('Something broke');
      await waitFor(() => expect(screen.queryByTestId('town-hall-not-opened')).not.toBeInTheDocument());
    });
  });

  it('rejects an invalid community id without calling the API', () => {
    const { get } = serve({});
    renderPage('/community/abc/town-hall');
    expect(screen.getByText('This Town Hall link is not valid.')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
