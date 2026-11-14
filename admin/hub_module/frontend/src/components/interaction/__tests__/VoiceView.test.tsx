/**
 * Member voice view: rooms by label, join, raise/lower hand with state and live announcements, an honest
 * "media join coming" notice (no fabricated participants, no LiveKit claim), member-only controls, and the
 * flag-off / retryable-503 / not-found states.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import VoiceView from '../VoiceView';
import { createTestQueryClient } from '../../../test/renderWithProviders';
import { ADA, OTHER_ROOM_ID, ROOM_ID, envelope, handWire, httpError, roomWire, serve } from '../../../test/callsFixtures';

const BASE = '/api/v1/community/7/interact/voice/rooms';

const roomsOk = { success: true, rooms: [roomWire(), roomWire({ room_id: OTHER_ROOM_ID, label: 'Late night', locked: true, publisher_ids: [], subscriber_count: 1 })] };
const joinOk = {
  success: true,
  token: 'room-token-secret',
  url: 'https://media.example/whip',
  whip_url: 'https://media.example/whip/r',
  whep_url: 'https://media.example/whep/r',
  token_id: 'tok-1',
  participant_id: ADA,
  expires_at: 1760003600,
};

function renderView(channel: { communityId: number | string; name?: string; allowAdHocVoice?: boolean } = { communityId: '7', name: 'Voice lounge' }) {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <VoiceView channel={channel} />
    </QueryClientProvider>,
  );
}

async function joinTownHall() {
  fireEvent.click(await screen.findByRole('button', { name: 'Join Town hall' }));
  return screen.findByTestId('voice-in-call');
}

describe('VoiceView', () => {
  beforeEach(() => vi.spyOn(console, 'error').mockImplementation(() => {}));
  afterEach(() => vi.restoreAllMocks());

  it('lists rooms by label with counts, a locked badge, and never an id', async () => {
    serve({ [`GET ${BASE}`]: roomsOk });
    const { container } = renderView();
    expect(await screen.findByText('Town hall')).toBeInTheDocument();
    expect(screen.getByText('Late night')).toBeInTheDocument();
    expect(screen.getByText('3 participants')).toBeInTheDocument();
    expect(screen.getByText('1 participant')).toBeInTheDocument();
    expect(screen.getByText('Locked')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Join Late night' })).toBeDisabled();
    expect(container.textContent).not.toContain(ROOM_ID);
  });

  it('joins by UUID and shows the honest media notice, without fake participants or the token', async () => {
    const { post } = serve({ [`GET ${BASE}`]: roomsOk, [`POST ${BASE}/${ROOM_ID}/join`]: joinOk });
    const { container } = renderView();
    const call = await joinTownHall();
    expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/join`);
    const notice = within(call).getByTestId('media-join-coming');
    expect(notice).toHaveTextContent('Audio and video are not available yet');
    expect(notice).toHaveTextContent('no audio or video is sent or received');
    expect(container.textContent).not.toMatch(/LiveKit/i);
    expect(container.textContent).not.toContain('room-token-secret');
    expect(container.textContent).not.toMatch(/Participant \d|You \(local\)/);
    expect(within(call).getByText('Town hall')).toBeInTheDocument();
  });

  it('gives members raise/lower hand with state and announcements, and no moderator controls', async () => {
    const hand = handWire(ADA, 'Ada Lovelace', 1760000300);
    const { post } = serve({
      [`GET ${BASE}`]: roomsOk,
      [`POST ${BASE}/${ROOM_ID}/join`]: joinOk,
      [`POST ${BASE}/${ROOM_ID}/raise-hand`]: { success: true, hand },
      [`POST ${BASE}/${ROOM_ID}/lower-hand`]: { success: true, lowered: true },
    });
    renderView();
    const call = await joinTownHall();
    expect(within(call).getByTestId('hand-state')).toHaveTextContent('Your hand is down.');

    const raise = within(call).getByRole('button', { name: 'Raise hand' });
    expect(raise).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(raise);
    const lower = await within(call).findByRole('button', { name: 'Lower hand' });
    expect(lower).toHaveAttribute('aria-pressed', 'true');
    expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/raise-hand`);
    expect(within(call).getByTestId('hand-state')).toHaveTextContent('Your hand is raised.');
    expect(within(call).getByTestId('voice-live-region')).toHaveTextContent('You raised your hand.');

    fireEvent.click(lower);
    await within(call).findByRole('button', { name: 'Raise hand' });
    expect(within(call).getByTestId('voice-live-region')).toHaveTextContent('You lowered your hand.');
    expect(within(call).getByTestId('hand-state')).toHaveTextContent('Your hand is down.');

    for (const moderatorControl of [/Mute all/, /Clear all hands/, /Acknowledge/, /Remove .* from the room/, /^Kick/]) {
      expect(screen.queryByRole('button', { name: moderatorControl })).not.toBeInTheDocument();
    }
  });

  it('keeps the hand down and shows the server message when raising fails', async () => {
    serve({
      [`GET ${BASE}`]: roomsOk,
      [`POST ${BASE}/${ROOM_ID}/join`]: joinOk,
      [`POST ${BASE}/${ROOM_ID}/raise-hand`]: httpError(429, envelope('RATE_LIMITED', 'Too many hands, slow down')),
    });
    renderView();
    const call = await joinTownHall();
    fireEvent.click(within(call).getByRole('button', { name: 'Raise hand' }));
    expect(await within(call).findByText('Too many hands, slow down')).toBeInTheDocument();
    expect(within(call).getByTestId('hand-state')).toHaveTextContent('Your hand is down.');
    expect(within(call).getByRole('button', { name: 'Raise hand' })).toBeInTheDocument();
  });

  it('leaves the room and returns to the list; a failed leave stays in the call with an error', async () => {
    let failLeave = true;
    const { post } = serve({
      [`GET ${BASE}`]: roomsOk,
      [`POST ${BASE}/${ROOM_ID}/join`]: joinOk,
      [`POST ${BASE}/${ROOM_ID}/leave`]: () => (failLeave ? httpError(503, envelope('STREAMING_UNAVAILABLE', 'Cannot leave right now', { retryable: true })) : { success: true, message: 'Left room' }),
    });
    renderView();
    const call = await joinTownHall();
    fireEvent.click(within(call).getByRole('button', { name: 'Leave room' }));
    expect(await within(call).findByText('Cannot leave right now')).toBeInTheDocument();
    expect(within(call).getByText(/You can try again\./)).toBeInTheDocument();
    failLeave = false;
    fireEvent.click(within(call).getByRole('button', { name: 'Leave room' }));
    await waitFor(() => expect(screen.queryByTestId('voice-in-call')).not.toBeInTheDocument());
    expect(post).toHaveBeenCalledWith(`${BASE}/${ROOM_ID}/leave`);
    expect(await screen.findByText('Late night')).toBeInTheDocument();
  });

  it('shows why joining failed (room gone) and stays on the list', async () => {
    serve({ [`GET ${BASE}`]: roomsOk, [`POST ${BASE}/${ROOM_ID}/join`]: httpError(404, envelope('ROOM_NOT_FOUND', 'Room not found')) });
    renderView();
    fireEvent.click(await screen.findByRole('button', { name: 'Join Town hall' }));
    const banner = await screen.findByText('Room not found');
    expect(banner.closest('[role="alert"]')).toHaveTextContent('That room no longer exists');
    expect(screen.queryByTestId('voice-in-call')).not.toBeInTheDocument();
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

  it('shows the server message when creating a room fails, and keeps the form open', async () => {
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

  it('shows an error when lowering the hand fails and keeps it raised', async () => {
    serve({
      [`GET ${BASE}`]: roomsOk,
      [`POST ${BASE}/${ROOM_ID}/join`]: joinOk,
      [`POST ${BASE}/${ROOM_ID}/raise-hand`]: { success: true, hand: handWire(ADA, 'Ada Lovelace', 1760000300) },
      [`POST ${BASE}/${ROOM_ID}/lower-hand`]: httpError(503, envelope('STREAMING_UNAVAILABLE', 'Cannot lower right now', { retryable: true })),
    });
    renderView();
    const call = await joinTownHall();
    fireEvent.click(within(call).getByRole('button', { name: 'Raise hand' }));
    fireEvent.click(await within(call).findByRole('button', { name: 'Lower hand' }));
    expect(await within(call).findByText('Cannot lower right now')).toBeInTheDocument();
    expect(within(call).getByTestId('hand-state')).toHaveTextContent('Your hand is raised.');
  });

  it('reports a lower-hand that found no raised hand (moderator already cleared it)', async () => {
    serve({
      [`GET ${BASE}`]: roomsOk,
      [`POST ${BASE}/${ROOM_ID}/join`]: joinOk,
      [`POST ${BASE}/${ROOM_ID}/raise-hand`]: { success: true, hand: handWire(ADA, 'Ada Lovelace', 1760000300) },
      [`POST ${BASE}/${ROOM_ID}/lower-hand`]: { success: true, lowered: false },
    });
    renderView();
    const call = await joinTownHall();
    fireEvent.click(within(call).getByRole('button', { name: 'Raise hand' }));
    fireEvent.click(await within(call).findByRole('button', { name: 'Lower hand' }));
    await within(call).findByRole('button', { name: 'Raise hand' });
    expect(within(call).getByTestId('voice-live-region')).toHaveTextContent('Your hand was already lowered.');
  });

  it('hides room creation when the channel does not allow ad-hoc rooms', async () => {
    serve({ [`GET ${BASE}`]: roomsOk });
    renderView();
    await screen.findByText('Town hall');
    expect(screen.queryByRole('button', { name: 'Create room' })).not.toBeInTheDocument();
  });

  it('explains an empty room list differently by channel setting', async () => {
    serve({ [`GET ${BASE}`]: { success: true, rooms: [] } });
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
