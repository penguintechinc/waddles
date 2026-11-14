/**
 * Calls client: error envelope handling (402 flag-off, retryable 503, explicit 404 - never an empty list),
 * the UUID guard, display-name helpers and loud failures for drifted response shapes.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '../../lib/apiClient';
import { httpError, ok } from '../../test/api';
import {
  acknowledgeHand,
  CallsApiError,
  createRoom,
  fetchParticipants,
  fetchRaisedHands,
  fetchRooms,
  GUEST_LABEL,
  joinRoom,
  lowerHand,
  muteAll,
  personName,
  raiseHand,
  resetHands,
  roomTitle,
  setParticipantMuted,
  toCallsError,
  UNTITLED_ROOM,
} from '../callsApi';

const ROOM = '3f2b8c1e-9a4d-4e57-8b21-6c0d5e7f1a90';
const PERSON = '7a1d2c3b-5e6f-4a80-9b1c-2d3e4f5a6b7c';

const roomWire = (over: Record<string, unknown> = {}) => ({
  room_id: ROOM,
  community_id: '7',
  label: 'Town hall',
  max_publishers: 25,
  created_at: 1760000000,
  publisher_ids: [PERSON],
  subscriber_count: 3,
  locked: false,
  ...over,
});

const envelope = (code: string, message: string, details?: Record<string, unknown>) => ({
  success: false,
  error: { code, message, ...(details ? { details } : {}) },
});

afterEach(() => vi.restoreAllMocks());

describe('error envelope', () => {
  it('maps HTTP 402 FEATURE_NOT_ENABLED to a feature-disabled error that keeps the server message', async () => {
    vi.spyOn(apiClient, 'get').mockRejectedValue(httpError(402, envelope('FEATURE_NOT_ENABLED', 'Calls / RTC is not enabled for this plan')));
    const err = await fetchRooms('admin', 7).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(CallsApiError);
    expect(err).toMatchObject({ kind: 'feature-disabled', status: 402, code: 'FEATURE_NOT_ENABLED', retryable: false, message: 'Calls / RTC is not enabled for this plan' });
  });

  it('marks 503 with details.retryable as retryable and unavailable', async () => {
    vi.spyOn(apiClient, 'get').mockRejectedValue(httpError(503, envelope('STREAMING_UNAVAILABLE', 'Streaming service unavailable', { retryable: true })));
    await expect(fetchRooms('member', 7)).rejects.toMatchObject({ kind: 'unavailable', retryable: true, message: 'Streaming service unavailable' });
  });

  it('treats 504 as retryable even without details', async () => {
    vi.spyOn(apiClient, 'get').mockRejectedValue(httpError(504, envelope('TIMEOUT', 'Timed out')));
    await expect(fetchRooms('admin', 7)).rejects.toMatchObject({ kind: 'unavailable', retryable: true });
  });

  it('surfaces a 404 as an error, not an empty room list', async () => {
    vi.spyOn(apiClient, 'get').mockRejectedValue(httpError(404, envelope('ROOM_NOT_FOUND', 'Room not found')));
    await expect(fetchRooms('admin', 7)).rejects.toMatchObject({ kind: 'not-found', status: 404 });
    await expect(fetchRaisedHands(7, ROOM)).rejects.toMatchObject({ kind: 'not-found' });
  });

  it.each([
    [403, 'forbidden'],
    [429, 'rate-limited'],
    [400, 'invalid'],
    [500, 'unknown'],
  ])('classifies HTTP %i as %s', async (status, kind) => {
    vi.spyOn(apiClient, 'get').mockRejectedValue(httpError(status, envelope('X', 'nope')));
    await expect(fetchRooms('admin', 7)).rejects.toMatchObject({ kind, status });
  });

  it('falls back to a generic message and ERROR_<status> code when the body is not the envelope', () => {
    const err = toCallsError(httpError(500, '<html>oops</html>'));
    expect(err).toMatchObject({ code: 'ERROR_500', retryable: false });
    expect(err.message).toContain('HTTP 500');
  });

  it('treats a request that never got a response as a retryable network error', () => {
    const err = toCallsError(Object.assign(new Error('Network Error'), { isAxiosError: true }));
    expect(err).toMatchObject({ status: 0, code: 'NETWORK_ERROR', retryable: true, kind: 'unknown' });
  });

  it('wraps shape drift and unknown throwables as a non-retryable client error', () => {
    expect(toCallsError(new Error('boom'))).toMatchObject({ code: 'CLIENT_ERROR', retryable: false, message: 'boom' });
    expect(toCallsError('weird')).toMatchObject({ code: 'CLIENT_ERROR', message: 'Unexpected calls error.' });
    const same = new CallsApiError('m', 1, 'C', false);
    expect(toCallsError(same)).toBe(same);
  });
});

describe('request guards', () => {
  it('refuses a non-UUID room id before any request (legacy names are not addressable)', async () => {
    const post = vi.spyOn(apiClient, 'post');
    await expect(joinRoom(7, 'weekly-call')).rejects.toMatchObject({ status: 400, code: 'INVALID_ROOM_ID' });
    expect(post).not.toHaveBeenCalled();
  });

  it('refuses a non-UUID participant id', async () => {
    await expect(acknowledgeHand(7, ROOM, 'alice')).rejects.toMatchObject({ code: 'INVALID_ROOM_ID', message: 'Participant id must be a UUID' });
  });

  it('sends room_name (and max_participants only when set)', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue(ok(roomWire()));
    await createRoom('admin', 7, { roomName: 'Town hall' });
    expect(post).toHaveBeenLastCalledWith('/api/v1/admin/7/calls/rooms', { room_name: 'Town hall' });
    await createRoom('member', 7, { roomName: 'Hangout', maxParticipants: 12 });
    expect(post).toHaveBeenLastCalledWith('/api/v1/community/7/interact/voice/rooms', { room_name: 'Hangout', max_participants: 12 });
  });
});

describe('parsing', () => {
  it('parses a room by its label and UUID and tolerates a missing label', async () => {
    vi.spyOn(apiClient, 'get').mockResolvedValue(ok({ success: true, rooms: [roomWire(), roomWire({ label: null, room_id: PERSON })] }));
    const rooms = await fetchRooms('admin', 7);
    expect(rooms.map((r) => r.label)).toEqual(['Town hall', null]);
    expect(roomTitle(rooms[0] ?? { label: null })).toBe('Town hall');
    expect(roomTitle(rooms[1] ?? { label: null })).toBe(UNTITLED_ROOM);
    expect(roomTitle({ label: '   ' })).toBe(UNTITLED_ROOM);
  });

  it('fails loudly on a drifted room shape instead of rendering an empty page', async () => {
    vi.spyOn(apiClient, 'get').mockResolvedValue(ok({ rooms: [{ name: 'old', participantCount: 1 }] }));
    await expect(fetchRooms('admin', 7)).rejects.toMatchObject({ code: 'CLIENT_ERROR' });
    vi.spyOn(apiClient, 'get').mockResolvedValue(ok({ success: true }));
    await expect(fetchRooms('admin', 7)).rejects.toThrow('"rooms" is not a list');
    vi.spyOn(apiClient, 'get').mockResolvedValue(ok({ rooms: [roomWire({ publisher_ids: [1] })] }));
    await expect(fetchRooms('admin', 7)).rejects.toThrow('non-string');
  });

  it('shows participant display names, with Guest for blanks', async () => {
    vi.spyOn(apiClient, 'get').mockResolvedValue(
      ok({
        success: true,
        locked: true,
        participants: [
          { participant_id: PERSON, display_name: 'Ada', publisher: true, subscriber: false, muted: false, joined_at: 5 },
          { participant_id: ROOM, display_name: '  ', publisher: false, subscriber: true, muted: true, joined_at: null },
        ],
      }),
    );
    const list = await fetchParticipants(7, ROOM);
    expect(list.locked).toBe(true);
    expect(list.participants.map((p) => p.displayName)).toEqual(['Ada', GUEST_LABEL]);
    expect(list.participants[1]).toMatchObject({ muted: true, joinedAt: null });
    expect(personName('Grace')).toBe('Grace');
  });

  it('parses the ordered hands queue with acknowledgement state', async () => {
    vi.spyOn(apiClient, 'get').mockResolvedValue(
      ok({
        success: true,
        count: 2,
        raised_hands: [
          { participant_id: PERSON, display_name: 'Ada', raised_at: 10, acknowledged_at: null, acknowledged_by: null },
          { participant_id: ROOM, display_name: 'guest', raised_at: 20, acknowledged_at: 25, acknowledged_by: PERSON },
        ],
      }),
    );
    const hands = await fetchRaisedHands(7, ROOM);
    expect(hands.map((h) => h.displayName)).toEqual(['Ada', 'guest']);
    expect(hands[1]).toMatchObject({ acknowledgedAt: 25, acknowledgedBy: PERSON });
  });

  it('returns the result values of the action routes', async () => {
    const hand = { participant_id: PERSON, display_name: 'Ada', raised_at: 10, acknowledged_at: null, acknowledged_by: null };
    const post = vi.spyOn(apiClient, 'post');
    post.mockResolvedValueOnce(ok({ success: true, hand }));
    await expect(raiseHand(7, ROOM)).resolves.toMatchObject({ displayName: 'Ada' });
    post.mockResolvedValueOnce(ok({ success: true, lowered: false }));
    await expect(lowerHand(7, ROOM)).resolves.toBe(false);
    post.mockResolvedValueOnce(ok({ success: true, cleared: 3 }));
    await expect(resetHands(7, ROOM)).resolves.toBe(3);
    post.mockResolvedValueOnce(ok({ success: true, muted: 2 }));
    await expect(muteAll(7, ROOM)).resolves.toBe(2);
    post.mockResolvedValueOnce(ok({ success: true, participant_id: PERSON, muted: true }));
    await expect(setParticipantMuted(7, ROOM, PERSON, true)).resolves.toBe(true);
    post.mockResolvedValueOnce(ok({ success: true, hand }));
    await expect(acknowledgeHand(7, ROOM, PERSON)).resolves.toMatchObject({ participantId: PERSON });
  });

  it('parses a join response', async () => {
    vi.spyOn(apiClient, 'post').mockResolvedValue(
      ok({ success: true, token: 't', url: 'https://media.example/whip', whip_url: 'https://media.example/whip/r', whep_url: 'https://media.example/whep/r', token_id: 'k', participant_id: PERSON, expires_at: 99 }),
    );
    await expect(joinRoom(7, ROOM)).resolves.toMatchObject({ participantId: PERSON, expiresAt: 99, whepUrl: 'https://media.example/whep/r' });
  });
});
