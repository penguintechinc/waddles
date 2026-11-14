/**
 * Town Hall page: the community's persistent room (hub-api creates it lazily, so a load never means "not opened"):
 * the flag-off (402) / 503-retry / error states, role-based call controls, and the real join flow on fakes.
 * No "media join coming" placeholder remains.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import TownHall from '../TownHall';
import { renderWithProviders } from '../../../test/renderWithProviders';
import { ADA, ROOM_ID, envelope, httpError, participantWire, roomWire, serve } from '../../../test/callsFixtures';
import { answerResponse, fakeDevices, fakeMediaDeps, resetFakeRtc } from '../../../test/fakeRtc';

const auth = vi.hoisted(() => ({ admin: false }));
vi.mock('../../../contexts/AuthContext', () => ({ useAuth: () => ({ isCommunityAdmin: () => auth.admin }) }));

const TOWN_HALL = '/api/v1/communities/7/town-hall';
const MEMBER = `/api/v1/community/7/interact/voice/rooms/${ROOM_ID}`;
const SELF = '11111111-1111-4111-8111-111111111111';
const hall = {
  success: true,
  room_id: ROOM_ID,
  label: 'Town Hall',
  locked: false,
  participant_count: 2,
  created: false,
  join: { method: 'POST', path: `/api/v1/community/7/interact/voice/rooms/${ROOM_ID}/join` },
};
const MEMBER_ROOMS = '/api/v1/community/7/interact/voice/rooms';
const MY_HAND = `/api/v1/communities/7/calls/rooms/${ROOM_ID}/hands/mine`;
const handIdle = { success: true, participant_id: SELF, raised: false, acknowledged: false, raised_at: null, acknowledged_at: null };
const joinOk = {
  success: true,
  token: 'room-token-secret',
  url: 'https://media.example/rtc/rooms/r',
  whip_url: 'https://media.example/rtc/rooms/r/whip',
  whep_url: 'https://media.example/rtc/rooms/r/whep',
  token_id: 'tok',
  token_type: 'Bearer',
  participant_id: SELF,
  expires_at: 4_000_000_000,
  publish: true,
  subscribe: true,
  ice_servers: [],
};
const roster = { success: true, locked: false, participants: [participantWire(SELF, 'Me Myself'), participantWire(ADA, 'Ada Lovelace')] };
const memberRooms = { success: true, rooms: [roomWire({ room_id: ROOM_ID, publisher_ids: [SELF, ADA], subscriber_count: 0 })] };

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
      [`GET ${MEMBER_ROOMS}`]: memberRooms,
      [`GET ${MY_HAND}`]: handIdle,
    });
    const { container } = renderPage();
    expect(await screen.findByRole('heading', { level: 1, name: 'Town Hall' })).toBeInTheDocument();
    const lobby = await screen.findByTestId('call-lobby');
    expect(within(lobby).getByRole('heading', { name: 'Town Hall' })).toBeInTheDocument();
    fireEvent.click(within(lobby).getByRole('button', { name: 'Join with microphone' }));
    const connected = await screen.findByTestId('call-connected');
    expect(await within(connected).findByTestId('room-presence')).toHaveTextContent('2 people, 2 speaking');
    expect(container.textContent).not.toMatch(/LiveKit|media join is coming|not available yet/i);
    expect(container.textContent).not.toContain(ROOM_ID);
  });

  it('members get the hand toggle; community admins get moderator controls', async () => {
    serve({
      [`GET ${TOWN_HALL}`]: hall,
      [`POST ${MEMBER}/join`]: joinOk,
      [`GET ${MEMBER_ROOMS}`]: memberRooms,
      [`GET ${MY_HAND}`]: handIdle,
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

  describe('failure states', () => {
    it('flag off (402): "not enabled for this community", no retry', async () => {
      serve({ [`GET ${TOWN_HALL}`]: httpError(402, envelope('FEATURE_NOT_ENABLED', 'Calls / RTC is not enabled for this plan')) });
      renderPage();
      expect(await screen.findByTestId('calls-feature-disabled')).toHaveTextContent('Calls are not enabled for this community');
      expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
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

    it('other errors (404 included) are error states with the server message, never an empty room', async () => {
      serve({ [`GET ${TOWN_HALL}`]: httpError(404, envelope('COMMUNITY_NOT_FOUND', 'Community not found')) });
      renderPage();
      expect(await screen.findByTestId('calls-error')).toHaveTextContent('Community not found');
      await waitFor(() => expect(screen.queryByTestId('call-lobby')).not.toBeInTheDocument());
    });
  });

  it('rejects an invalid community id without calling the API', () => {
    const { get } = serve({});
    renderPage('/community/abc/town-hall');
    expect(screen.getByText('This Town Hall link is not valid.')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
