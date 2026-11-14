/**
 * Member voice channel: rooms by label, open one into the real call view (lobby -> join -> leave), role-based controls
 * (moderators vs members), ad-hoc room creation, and the flag-off / retryable-503 / error states. The media stack itself
 * is covered by CallRoomView / media tests; here it runs on the same fakes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import VoiceView from '../VoiceView';
import { createTestQueryClient } from '../../../test/renderWithProviders';
import { ADA, OTHER_ROOM_ID, ROOM_ID, envelope, httpError, participantWire, roomWire, serve } from '../../../test/callsFixtures';
import { answerResponse, fakeDevices, fakeMediaDeps, resetFakeRtc } from '../../../test/fakeRtc';

const auth = vi.hoisted(() => ({ admin: false }));
vi.mock('../../../contexts/AuthContext', () => ({ useAuth: () => ({ isCommunityAdmin: () => auth.admin }) }));

const BASE = '/api/v1/community/7/interact/voice/rooms';
const SELF = '11111111-1111-4111-8111-111111111111';

const roomsOk = { success: true, rooms: [roomWire(), roomWire({ room_id: OTHER_ROOM_ID, label: 'Late night', locked: true, publisher_ids: [], subscriber_count: 1 })] };
const joinOk = {
  success: true,
  token: 'room-token-secret',
  url: 'https://media.example/rtc/rooms/r',
  whip_url: 'https://media.example/rtc/rooms/r/whip',
  whep_url: 'https://media.example/rtc/rooms/r/whep',
  token_id: 'tok-1',
  participant_id: SELF,
  expires_at: 1760003600,
};
const roster = { success: true, locked: false, participants: [participantWire(SELF, 'Me Myself'), participantWire(ADA, 'Ada Lovelace')] };

const callRoutes = {
  [`GET ${BASE}`]: roomsOk,
  [`POST ${BASE}/${ROOM_ID}/join`]: joinOk,
  [`POST ${BASE}/${ROOM_ID}/leave`]: { success: true, message: 'Left room' },
  [`GET ${BASE}/${ROOM_ID}/participants`]: roster,
  [`GET ${BASE}/${ROOM_ID}/hands/mine`]: { success: true, hand: null },
};

function renderView(channel: { communityId: number | string; name?: string; allowAdHocVoice?: boolean } = { communityId: '7', name: 'Voice lounge' }) {
  const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => (init.method === 'DELETE' ? new Response(null, { status: 204 }) : answerResponse('/rtc/rooms/r/sessions/s')));
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <VoiceView channel={channel} overrides={{ devices: fakeDevices(), media: fakeMediaDeps(fetchImpl as unknown as ReturnType<typeof vi.fn>) }} />
    </QueryClientProvider>,
  );
}

describe('VoiceView', () => {
  beforeEach(() => {
    resetFakeRtc();
    auth.admin = false;
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it('lists rooms by label with counts, a locked badge (not openable), and never an id', async () => {
    serve({ [`GET ${BASE}`]: roomsOk });
    const { container } = renderView();
    expect(await screen.findByText('Town hall')).toBeInTheDocument();
    expect(screen.getByText('Late night')).toBeInTheDocument();
    expect(screen.getByText('3 participants')).toBeInTheDocument();
    expect(screen.getByText('1 participant')).toBeInTheDocument();
    expect(screen.getByText('Locked')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open Late night' })).toBeDisabled();
    expect(container.textContent).not.toContain(ROOM_ID);
  });

  it('opens a room into the call view, joins with real media (no placeholder), and leaves back to the list', async () => {
    const { post } = serve(callRoutes);
    const { container } = renderView();
    fireEvent.click(await screen.findByRole('button', { name: 'Open Town hall' }));
    expect(await screen.findByTestId('call-lobby')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Join with microphone' }));
    const connected = await screen.findByTestId('call-connected');
    expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/join`);
    expect(container.textContent).not.toMatch(/LiveKit|not available yet|media join is coming/i);
    expect(container.textContent).not.toContain('room-token-secret');
    expect(within(connected).getByTestId('my-hand')).toBeInTheDocument();

    fireEvent.click(within(connected).getByRole('button', { name: 'Leave room' }));
    expect(await screen.findByText('Late night')).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/leave`);
    expect(screen.queryByTestId('call-room')).not.toBeInTheDocument();
  });

  it('gives community admins the moderator controls instead of the member hand toggle', async () => {
    auth.admin = true;
    serve({
      ...callRoutes,
      [`GET /api/v1/admin/7/calls/rooms/${ROOM_ID}/participants`]: roster,
      [`GET /api/v1/admin/7/calls/rooms/${ROOM_ID}/raised-hands`]: { success: true, count: 0, raised_hands: [] },
    });
    renderView();
    fireEvent.click(await screen.findByRole('button', { name: 'Open Town hall' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Join with microphone' }));
    const connected = await screen.findByTestId('call-connected');
    expect(await within(connected).findByRole('button', { name: 'Mute all' })).toBeInTheDocument();
    expect(within(connected).queryByTestId('my-hand')).not.toBeInTheDocument();
  });

  it('refreshes the list on demand and cancels the create form', async () => {
    const { get } = serve({ [`GET ${BASE}`]: roomsOk });
    renderView({ communityId: 7, allowAdHocVoice: true });
    await screen.findByText('Town hall');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: 'Create room' }));
    fireEvent.click(within(screen.getByRole('region', { name: 'Create a room' })).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('region', { name: 'Create a room' })).not.toBeInTheDocument();
  });

  it('creates an ad-hoc room with room_name when the channel allows it', async () => {
    const { post } = serve({ [`GET ${BASE}`]: roomsOk, [`POST ${BASE}`]: roomWire() });
    renderView({ communityId: 7, name: 'Voice lounge', allowAdHocVoice: true });
    fireEvent.click(await screen.findByRole('button', { name: 'Create room' }));
    fireEvent.change(screen.getByLabelText(/Room name/), { target: { value: 'hangout' } });
    fireEvent.click(within(screen.getByRole('region', { name: 'Create a room' })).getByRole('button', { name: 'Create room' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(BASE, { room_name: 'hangout' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Create a room' })).not.toBeInTheDocument());
  });

  it('shows the server message when creating a room fails, keeps the form open, and dismisses the banner', async () => {
    serve({ [`GET ${BASE}`]: roomsOk, [`POST ${BASE}`]: httpError(429, envelope('RATE_LIMITED', 'Room limit reached')) });
    renderView({ communityId: 7, allowAdHocVoice: true });
    fireEvent.click(await screen.findByRole('button', { name: 'Create room' }));
    fireEvent.change(screen.getByLabelText(/Room name/), { target: { value: 'one more' } });
    fireEvent.click(within(screen.getByRole('region', { name: 'Create a room' })).getByRole('button', { name: 'Create room' }));
    expect(await screen.findByText('Room limit reached')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Create a room' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss message' }));
    expect(screen.queryByText('Room limit reached')).not.toBeInTheDocument();
  });

  it('hides room creation when the channel does not allow ad-hoc rooms, and explains an empty list per setting', async () => {
    serve({ [`GET ${BASE}`]: { success: true, rooms: [] } });
    const { unmount } = renderView();
    expect(await screen.findByTestId('voice-no-rooms')).toHaveTextContent('A moderator can open a room for the community.');
    expect(screen.queryByRole('button', { name: 'Create room' })).not.toBeInTheDocument();
    unmount();
    renderView({ communityId: 7, allowAdHocVoice: true });
    expect(await screen.findByTestId('voice-no-rooms')).toHaveTextContent('Create a room to start a conversation.');
  });

  describe('failure states', () => {
    it('flag off (402) shows "not enabled for this community" and no create button', async () => {
      serve({ [`GET ${BASE}`]: httpError(402, envelope('FEATURE_NOT_ENABLED', 'Calls / RTC is not enabled for this plan')) });
      renderView({ communityId: 7, allowAdHocVoice: true });
      expect(await screen.findByTestId('calls-feature-disabled')).toHaveTextContent('Calls are not enabled for this community');
      expect(screen.queryByRole('button', { name: 'Create room' })).not.toBeInTheDocument();
      expect(screen.queryByTestId('voice-no-rooms')).not.toBeInTheDocument();
    });

    it('503 shows the message and a retry that recovers', async () => {
      let healthy = false;
      serve({ [`GET ${BASE}`]: () => (healthy ? roomsOk : httpError(503, envelope('STREAMING_UNAVAILABLE', 'Streaming service unavailable', { retryable: true }))) });
      renderView();
      const state = await screen.findByTestId('calls-unavailable');
      expect(state).toHaveTextContent('Streaming service unavailable');
      healthy = true;
      fireEvent.click(within(state).getByRole('button', { name: 'Try again' }));
      expect(await screen.findByText('Town hall')).toBeInTheDocument();
    });

    it('a failed list is never shown as an empty list', async () => {
      serve({ [`GET ${BASE}`]: httpError(500, envelope('INTERNAL', 'Something broke')) });
      renderView();
      expect(await screen.findByTestId('calls-error')).toHaveTextContent('Something broke');
      expect(screen.queryByTestId('voice-no-rooms')).not.toBeInTheDocument();
    });
  });

  it('rejects an invalid community id without calling the API', () => {
    const { get } = serve({});
    renderView({ communityId: 'nope' });
    expect(screen.getByText('This voice channel link is not valid.')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });

  it('defaults the heading to "Voice" when the channel has no name', async () => {
    serve({ [`GET ${BASE}`]: roomsOk });
    renderView({ communityId: 7 });
    expect(await screen.findByRole('heading', { name: 'Voice' })).toBeInTheDocument();
  });
});
