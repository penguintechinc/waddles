/**
 * CallRoomView end to end over fakes: lobby, join (devices -> hub-api join -> WHIP publish -> WHEP per remote),
 * participant tiles by name, mute/camera/device controls, member hand toggle, moderator controls, leave, and every
 * failure state (permission denied, 402 feature off, 503 retry, connection lost, remote problems, unmount cleanup).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { CallRoomView } from '../CallRoomView';
import { createTestQueryClient } from '../../../test/renderWithProviders';
import { ADA, GRACE, ROOM_ID, envelope, handWire, httpError, participantWire, serve } from '../../../test/callsFixtures';
import { FakePeerConnection, answerResponse, fakeDevices, fakeMediaDeps, resetFakeRtc } from '../../../test/fakeRtc';

const TOKEN = 'room-token-SECRET-value';
const MEMBER = `/api/v1/community/7/interact/voice/rooms/${ROOM_ID}`;
const ADMIN = `/api/v1/admin/7/calls/rooms/${ROOM_ID}`;
const SELF = '11111111-1111-4111-8111-111111111111';

const joinOk = {
  success: true,
  token: TOKEN,
  url: 'https://media.example/rtc/rooms/r',
  whip_url: 'https://media.example/rtc/rooms/r/whip',
  whep_url: 'https://media.example/rtc/rooms/r/whep',
  token_id: 'tok',
  participant_id: SELF,
  expires_at: 4_000_000_000,
};

const roster = {
  success: true,
  locked: false,
  participants: [
    participantWire(SELF, 'Me Myself'),
    participantWire(ADA, 'Ada Lovelace'),
    participantWire(GRACE, 'Grace Hopper', { publisher: false, subscriber: true, muted: true }),
  ],
};

function media(rules: { whip?: number; whep?: (source: string) => number } = {}) {
  return vi.fn(async (url: string, init: RequestInit) => {
    if (init.method === 'DELETE') return new Response(null, { status: 204 });
    if (url.includes('/whip')) return rules.whip && rules.whip !== 201 ? new Response('', { status: rules.whip }) : answerResponse('/rtc/rooms/r/sessions/pub');
    const source = new URL(url).searchParams.get('source') ?? '';
    const status = rules.whep?.(source) ?? 201;
    return status === 201 ? answerResponse(`/rtc/rooms/r/sessions/sub-${source}`) : new Response('', { status });
  });
}

type MediaFetch = ReturnType<typeof media>;

function memberRoutes(extra: Record<string, unknown> = {}) {
  return {
    [`POST ${MEMBER}/join`]: joinOk,
    [`POST ${MEMBER}/leave`]: { success: true, message: 'Left room' },
    [`GET ${MEMBER}/participants`]: roster,
    [`GET ${MEMBER}/hands/mine`]: { success: true, hand: null },
    [`POST ${MEMBER}/raise-hand`]: { success: true, hand: handWire(SELF, 'Me Myself', 100) },
    [`POST ${MEMBER}/lower-hand`]: { success: true, lowered: true },
    ...extra,
  };
}

function renderRoom(options: { moderator?: boolean; fetchImpl?: MediaFetch; devices?: ReturnType<typeof fakeDevices>; onLeft?: () => void } = {}) {
  const fetchImpl = options.fetchImpl ?? media();
  const devices = options.devices ?? fakeDevices({ devices: [
    { kind: 'audioinput', deviceId: 'mic-1', label: 'Built-in Mic' },
    { kind: 'audioinput', deviceId: 'mic-2', label: 'USB Mic' },
    { kind: 'videoinput', deviceId: 'cam-1', label: 'Webcam' },
  ] });
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <CallRoomView
        communityId={7}
        room={{ roomId: ROOM_ID, label: 'Town Hall' }}
        moderator={options.moderator ?? false}
        {...(options.onLeft ? { onLeft: options.onLeft } : {})}
        overrides={{ devices, media: fakeMediaDeps(fetchImpl as unknown as ReturnType<typeof vi.fn>) }}
      />
    </QueryClientProvider>,
  );
  return { ...view, fetchImpl, devices };
}

async function joinWithMic() {
  fireEvent.click(await screen.findByRole('button', { name: 'Join with microphone' }));
  return screen.findByTestId('call-connected');
}

const posts = (fetchImpl: MediaFetch, fragment: string): string[] =>
  fetchImpl.mock.calls.filter(([url, init]) => init.method === 'POST' && url.includes(fragment)).map(([url]) => String(url));

describe('CallRoomView', () => {
  beforeEach(() => {
    resetFakeRtc();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it('shows the lobby first and joins with the microphone, publishing and subscribing to each remote publisher by name', async () => {
    serve(memberRoutes());
    const { fetchImpl, container } = renderRoom();
    expect(await screen.findByTestId('call-lobby')).toHaveTextContent('Join with your microphone to talk');

    const connected = await joinWithMic();
    expect(screen.getByTestId('connection-status')).toHaveTextContent('Connected');
    expect(posts(fetchImpl, '/whip')).toEqual(['https://media.example/rtc/rooms/r/whip']);
    await waitFor(() => expect(posts(fetchImpl, '/whep')).toEqual([`https://media.example/rtc/rooms/r/whep?source=${ADA}`]));
    const tiles = await within(connected).findAllByTestId('media-tile');
    expect(tiles.map((t) => t.getAttribute('aria-label'))).toEqual(['You', 'Ada Lovelace']);
    const roomRoster = within(connected).getByTestId('roster');
    expect(within(roomRoster).getByText('Grace Hopper')).toBeInTheDocument();
    expect(within(roomRoster).getByText('Muted')).toBeInTheDocument();
    for (const secret of [TOKEN, SELF, ADA, GRACE, ROOM_ID]) expect(container.textContent).not.toContain(secret);
  });

  it('mutes the microphone, with state on the button and the local tile', async () => {
    serve(memberRoutes());
    renderRoom();
    const connected = await joinWithMic();
    const mute = within(connected).getByRole('button', { name: 'Mute microphone' });
    expect(mute).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(mute);
    const unmute = within(connected).getByRole('button', { name: 'Unmute microphone' });
    expect(unmute).toHaveAttribute('aria-pressed', 'true');
    expect(within(connected).getByText('Muted', { selector: 'figcaption span' })).toBeInTheDocument();
    fireEvent.click(unmute);
    expect(within(connected).getByRole('button', { name: 'Mute microphone' })).toBeInTheDocument();
  });

  it('shares the camera on request, toggles it, and lists devices to switch to', async () => {
    serve(memberRoutes());
    const { devices } = renderRoom();
    fireEvent.click(await screen.findByLabelText('Share my camera'));
    fireEvent.click(screen.getByRole('button', { name: 'Join with microphone' }));
    const connected = await screen.findByTestId('call-connected');
    fireEvent.click(within(connected).getByRole('button', { name: 'Turn camera off' }));
    expect(within(connected).getByRole('button', { name: 'Turn camera on' })).toHaveAttribute('aria-pressed', 'true');

    const mic = await within(connected).findByLabelText('Microphone');
    await waitFor(() => expect(within(mic).getAllByRole('option').map((o) => o.textContent)).toEqual(['Switch microphone', 'Built-in Mic', 'USB Mic']));
    expect(within(within(connected).getByLabelText('Camera')).getByRole('option', { name: 'Webcam' })).toBeInTheDocument();
    const callsBefore = devices.getUserMedia.mock.calls.length;
    fireEvent.change(mic, { target: { value: 'mic-2' } });
    await waitFor(() => expect(devices.getUserMedia.mock.calls.length).toBe(callsBefore + 1));
  });

  it('member: raises and lowers their hand with server state, acknowledgement and live announcements; no moderator controls', async () => {
    let hand: unknown = null;
    serve(
      memberRoutes({
        [`GET ${MEMBER}/hands/mine`]: () => ({ success: true, hand }),
        [`POST ${MEMBER}/raise-hand`]: () => {
          hand = handWire(SELF, 'Me Myself', 100);
          return { success: true, hand };
        },
        [`POST ${MEMBER}/lower-hand`]: () => {
          hand = null;
          return { success: true, lowered: true };
        },
      }),
    );
    renderRoom();
    const connected = await joinWithMic();
    const mine = within(connected).getByTestId('my-hand');
    expect(within(mine).getByTestId('hand-state')).toHaveTextContent('Your hand is down.');

    fireEvent.click(within(mine).getByRole('button', { name: 'Raise hand' }));
    const lower = await within(mine).findByRole('button', { name: 'Lower hand' });
    expect(lower).toHaveAttribute('aria-pressed', 'true');
    expect(within(mine).getByTestId('hand-state')).toHaveTextContent('Your hand is raised.');
    expect(within(mine).getByTestId('hand-live-region')).toHaveTextContent('You raised your hand.');

    hand = handWire(SELF, 'Me Myself', 100, { acknowledged_at: 120, acknowledged_by: ADA });
    await act(async () => {
      fireEvent.click(lower);
    });
    await waitFor(() => expect(within(mine).getByRole('button', { name: 'Raise hand' })).toBeInTheDocument());
    expect(within(mine).getByTestId('hand-live-region')).toHaveTextContent('You lowered your hand.');

    for (const control of [/Mute all/, /Clear all hands/, /^Acknowledge/, /Remove .* from the room/]) {
      expect(screen.queryByRole('button', { name: control })).not.toBeInTheDocument();
    }
  });

  it('member: shows the acknowledgement a moderator gave and announces it', async () => {
    const acknowledged = handWire(SELF, 'Me Myself', 100, { acknowledged_at: 120, acknowledged_by: ADA });
    serve(memberRoutes({ [`GET ${MEMBER}/hands/mine`]: { success: true, hand: acknowledged } }));
    renderRoom();
    const connected = await joinWithMic();
    const mine = within(connected).getByTestId('my-hand');
    await waitFor(() => expect(within(mine).getByTestId('hand-state')).toHaveTextContent('A moderator acknowledged your hand.'));
    expect(within(mine).getByTestId('hand-live-region')).toHaveTextContent('A moderator acknowledged your raised hand.');
  });

  it('member: a failing raise shows the server message and keeps the hand down; a failing own-hand read is a visible warning', async () => {
    serve(
      memberRoutes({
        [`POST ${MEMBER}/raise-hand`]: httpError(429, envelope('RATE_LIMITED', 'Slow down')),
        [`GET ${MEMBER}/hands/mine`]: httpError(404, envelope('NOT_FOUND', 'No such route')),
      }),
    );
    renderRoom();
    const connected = await joinWithMic();
    const mine = within(connected).getByTestId('my-hand');
    expect(await within(mine).findByText(/Could not check whether a moderator acknowledged your hand/)).toBeInTheDocument();
    fireEvent.click(within(mine).getByRole('button', { name: 'Raise hand' }));
    expect(await within(mine).findByText('Slow down')).toBeInTheDocument();
    expect(within(mine).getByTestId('hand-state')).toHaveTextContent('Your hand is down.');
  });

  it('moderator: gets the hands queue and participant moderation, not the member hand toggle, and sees action results and errors', async () => {
    serve({
      [`POST ${MEMBER}/join`]: joinOk,
      [`POST ${MEMBER}/leave`]: { success: true, message: 'Left room' },
      [`GET ${ADMIN}/participants`]: roster,
      [`GET ${ADMIN}/raised-hands`]: { success: true, count: 1, raised_hands: [handWire(GRACE, 'Grace Hopper', 200)] },
      [`POST ${ADMIN}/acknowledge-hand`]: { success: true, hand: handWire(GRACE, 'Grace Hopper', 200, { acknowledged_at: 250, acknowledged_by: ADA }) },
      [`POST ${ADMIN}/mute`]: httpError(403, envelope('FORBIDDEN', 'Moderator scope required')),
    });
    renderRoom({ moderator: true });
    const connected = await joinWithMic();
    expect(within(connected).queryByTestId('my-hand')).not.toBeInTheDocument();
    expect(await within(connected).findByTestId('hands-queue')).toHaveTextContent('Grace Hopper');

    fireEvent.click(within(connected).getByRole('button', { name: 'Acknowledge Grace Hopper' }));
    expect(await within(connected).findByText('Acknowledged Grace Hopper.')).toBeInTheDocument();

    fireEvent.click(await within(connected).findByRole('button', { name: 'Mute Ada Lovelace' }));
    const refused = await within(connected).findByText('Moderator scope required');
    expect(refused.closest('[role="alert"]')).toHaveTextContent('You do not have permission to do that');
    fireEvent.click(within(connected).getAllByRole('button', { name: 'Dismiss message' })[0] as HTMLElement);
  });

  it('leaves: ends media sessions, unregisters, returns to the lobby and calls onLeft', async () => {
    const { post } = serve(memberRoutes());
    const onLeft = vi.fn();
    const { fetchImpl } = renderRoom({ onLeft });
    const connected = await joinWithMic();
    await waitFor(() => expect(posts(fetchImpl, '/whep')).toHaveLength(1));
    fireEvent.click(within(connected).getByRole('button', { name: 'Leave room' }));
    await screen.findByTestId('call-lobby');
    expect(onLeft).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith(`${MEMBER}/leave`);
    expect(fetchImpl.mock.calls.filter(([, init]) => init.method === 'DELETE')).toHaveLength(2);
    expect(FakePeerConnection.instances.every((p) => p.closed)).toBe(true);
  });

  it('warns when the server did not confirm that the user left', async () => {
    serve(memberRoutes({ [`POST ${MEMBER}/leave`]: httpError(503, envelope('UNAVAILABLE', 'hub-api is busy', { retryable: true })) }));
    renderRoom();
    const connected = await joinWithMic();
    fireEvent.click(within(connected).getByRole('button', { name: 'Leave room' }));
    expect(await screen.findByText('You left, but the server did not confirm it')).toBeInTheDocument();
    expect(screen.getByText('hub-api is busy')).toBeInTheDocument();
    expect(screen.getByTestId('call-lobby')).toBeInTheDocument();
  });

  it('cleans up on unmount: closes peer connections, stops the microphone and unregisters', async () => {
    const { post } = serve(memberRoutes());
    const { unmount, fetchImpl } = renderRoom();
    await joinWithMic();
    await waitFor(() => expect(posts(fetchImpl, '/whep')).toHaveLength(1));
    unmount();
    await waitFor(() => expect(post).toHaveBeenCalledWith(`${MEMBER}/leave`));
    expect(FakePeerConnection.instances.every((p) => p.closed)).toBe(true);
  });

  describe('failure states', () => {
    it('blocked microphone: explains, offers retry and listen-only, and listen-only connects without publishing', async () => {
      serve(memberRoutes());
      const { fetchImpl } = renderRoom({ devices: fakeDevices({ failWith: { name: 'NotAllowedError' } }) });
      fireEvent.click(await screen.findByRole('button', { name: 'Join with microphone' }));
      const panel = await screen.findByTestId('call-device-error');
      expect(panel).toHaveTextContent('Microphone access is blocked');
      expect(within(panel).getByRole('button', { name: 'Try again' })).toBeInTheDocument();
      fireEvent.click(within(panel).getByRole('button', { name: 'Join listen-only' }));
      const connected = await screen.findByTestId('call-connected');
      expect(connected).toHaveTextContent('You are listening only');
      expect(posts(fetchImpl, '/whip')).toEqual([]);
      expect(within(connected).queryByRole('button', { name: /microphone/i })).not.toBeInTheDocument();
    });

    it('flag off (402) on join shows "not enabled for this community" with no retry', async () => {
      serve(memberRoutes({ [`POST ${MEMBER}/join`]: httpError(402, envelope('FEATURE_NOT_ENABLED', 'Calls / RTC is not enabled for this plan')) }));
      renderRoom();
      fireEvent.click(await screen.findByRole('button', { name: 'Join with microphone' }));
      expect(await screen.findByTestId('calls-feature-disabled')).toHaveTextContent('Calls are not enabled for this community');
      expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
    });

    it('503 on join shows the server message and a retry that connects', async () => {
      let healthy = false;
      serve(memberRoutes({ [`POST ${MEMBER}/join`]: () => (healthy ? joinOk : httpError(503, envelope('STREAMING_UNAVAILABLE', 'Streaming service unavailable', { retryable: true }))) }));
      renderRoom();
      fireEvent.click(await screen.findByRole('button', { name: 'Join with microphone' }));
      const state = await screen.findByTestId('calls-unavailable');
      expect(state).toHaveTextContent('Streaming service unavailable');
      healthy = true;
      fireEvent.click(within(state).getByRole('button', { name: 'Try again' }));
      expect(await screen.findByTestId('call-connected')).toBeInTheDocument();
    });

    it('a media server outage shows a retryable media error, and retry connects', async () => {
      let down = true;
      const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
        if (init.method === 'DELETE') return new Response(null, { status: 204 });
        if (url.includes('/whip') && down) return new Response('', { status: 503 });
        return answerResponse('/rtc/rooms/r/sessions/x');
      });
      serve(memberRoutes());
      renderRoom({ fetchImpl: fetchImpl as unknown as MediaFetch });
      fireEvent.click(await screen.findByRole('button', { name: 'Join with microphone' }));
      const panel = await screen.findByTestId('call-media-error');
      expect(panel).toHaveTextContent('The media server is unavailable');
      down = false;
      fireEvent.click(within(panel).getByRole('button', { name: 'Try again' }));
      expect(await screen.findByTestId('call-connected')).toBeInTheDocument();
    });

    it('a non-retryable media failure shows the reason without a retry button', async () => {
      serve(memberRoutes());
      renderRoom({ fetchImpl: media({ whip: 404 }) });
      fireEvent.click(await screen.findByRole('button', { name: 'Join with microphone' }));
      const panel = await screen.findByTestId('call-media-error');
      expect(panel).toHaveTextContent('no longer available');
      expect(within(panel).queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
    });

    it('a publish connection that drops shows "Connection lost" and reconnect rejoins', async () => {
      serve(memberRoutes());
      renderRoom();
      await joinWithMic();
      act(() => {
        FakePeerConnection.instances[0]?.emitConnectionState('connected');
        FakePeerConnection.instances[0]?.emitConnectionState('failed');
      });
      const lost = await screen.findByTestId('call-connection-lost');
      expect(lost).toHaveTextContent('The connection to the room was lost.');
      expect(screen.getByTestId('connection-status')).toHaveTextContent('Disconnected');
      fireEvent.click(within(lost).getByRole('button', { name: 'Try again' }));
      expect(await screen.findByTestId('call-connected')).toBeInTheDocument();
    });

    it('tells the user when audio/video from some participants could not be opened', async () => {
      serve(memberRoutes());
      renderRoom({ fetchImpl: media({ whep: () => 503 }) });
      const connected = await joinWithMic();
      expect(await within(connected).findByText(/Could not receive audio or video from 1 participant\. Retrying\./)).toBeInTheDocument();
    });

    it('without a publish grant (403) the member listens, with a notice', async () => {
      serve(memberRoutes());
      renderRoom({ fetchImpl: media({ whip: 403 }) });
      await joinWithMic();
      expect(await screen.findByText('You can listen in this room but not speak or share video.')).toBeInTheDocument();
    });

    it('a failing participants read keeps the call up and shows the error with retry in the side panel', async () => {
      let healthy = false;
      serve(memberRoutes({ [`GET ${MEMBER}/participants`]: () => (healthy ? roster : httpError(503, envelope('UNAVAILABLE', 'Roster unavailable', { retryable: true }))) }));
      renderRoom();
      const connected = await joinWithMic();
      const state = await within(connected).findByTestId('calls-unavailable');
      expect(state).toHaveTextContent('Roster unavailable');
      healthy = true;
      fireEvent.click(within(state).getByRole('button', { name: 'Try again' }));
      expect(await within(connected).findByTestId('roster')).toHaveTextContent('Grace Hopper');
    });
  });
});
