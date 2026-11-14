/**
 * PROVISIONAL contract: routes agreed with the hub-api owner (Town Hall ensure, own hand, member participants) that
 * are not in `openapi/v1.yaml` yet. These tests pin the AGREED shapes with mocked responses and are marked as such;
 * `callsApi.openapi.contract.test.ts` takes over (validating against the real spec) as soon as T4 publishes the routes.
 * When that happens, move the rows over and delete this file's route tests.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '../../lib/apiClient';
import { httpError, ok } from '../../test/api';
import { ensureTownHall, fetchMemberParticipants, fetchMyHand, fetchTownHall, joinRoom, parseIceServers } from '../callsApi';

const ROOM = '3f2b8c1e-9a4d-4e57-8b21-6c0d5e7f1a90';
const PERSON = '7a1d2c3b-5e6f-4a80-9b1c-2d3e4f5a6b7c';

afterEach(() => vi.restoreAllMocks());

describe('town hall (PROVISIONAL shape: room_id, label, participant_count)', () => {
  const wire = { success: true, room_id: ROOM, label: 'Town Hall', participant_count: 4 };

  it('GET reads the room id, label and participant count', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue(ok(wire));
    await expect(fetchTownHall(7)).resolves.toEqual({ roomId: ROOM, label: 'Town Hall', participantCount: 4 });
    expect(get).toHaveBeenCalledWith('/api/v1/communities/7/town-hall');
  });

  it('POST (ensure) is idempotent and returns the same shape; a null label is allowed', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue(ok({ ...wire, label: null }));
    await expect(ensureTownHall(7)).resolves.toMatchObject({ label: null });
    expect(post).toHaveBeenCalledWith('/api/v1/communities/7/town-hall');
  });

  it('a 404 is an error (not yet opened), 402 is feature-off, drifted shape fails loudly', async () => {
    vi.spyOn(apiClient, 'get').mockRejectedValueOnce(httpError(404, { success: false, error: { code: 'NOT_FOUND', message: 'No Town Hall' } }));
    await expect(fetchTownHall(7)).rejects.toMatchObject({ kind: 'not-found', message: 'No Town Hall' });
    vi.spyOn(apiClient, 'get').mockRejectedValueOnce(httpError(402, { success: false, error: { code: 'FEATURE_NOT_ENABLED', message: 'off' } }));
    await expect(fetchTownHall(7)).rejects.toMatchObject({ kind: 'feature-disabled' });
    vi.spyOn(apiClient, 'get').mockResolvedValueOnce(ok({ room: ROOM }));
    await expect(fetchTownHall(7)).rejects.toMatchObject({ code: 'CLIENT_ERROR' });
  });
});

describe('own hand (PROVISIONAL shape: { hand: HandDTO | null })', () => {
  it('returns the hand with its acknowledgement, or null when no hand is raised', async () => {
    const get = vi.spyOn(apiClient, 'get');
    get.mockResolvedValueOnce(ok({ success: true, hand: { participant_id: PERSON, display_name: 'Ada', raised_at: 10, acknowledged_at: 12, acknowledged_by: ROOM } }));
    await expect(fetchMyHand(7, ROOM)).resolves.toMatchObject({ displayName: 'Ada', acknowledgedAt: 12 });
    expect(get).toHaveBeenLastCalledWith(`/api/v1/community/7/interact/voice/rooms/${ROOM}/hands/mine`);
    get.mockResolvedValueOnce(ok({ success: true, hand: null }));
    await expect(fetchMyHand(7, ROOM)).resolves.toBeNull();
  });

  it('refuses a non-UUID room id before any request', async () => {
    const get = vi.spyOn(apiClient, 'get');
    await expect(fetchMyHand(7, 'town-hall')).rejects.toMatchObject({ code: 'INVALID_ROOM_ID' });
    expect(get).not.toHaveBeenCalled();
  });
});

describe('member participants (PROVISIONAL: same shape as the admin route)', () => {
  it('reads display names and the lock state from the member route', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue(
      ok({ success: true, locked: false, participants: [{ participant_id: PERSON, display_name: 'Ada', publisher: true, subscriber: false, muted: false, joined_at: 1 }] }),
    );
    const list = await fetchMemberParticipants(7, ROOM);
    expect(list.participants[0]).toMatchObject({ displayName: 'Ada', publisher: true });
    expect(get).toHaveBeenCalledWith(`/api/v1/community/7/interact/voice/rooms/${ROOM}/participants`);
  });

  it('is an error, not an empty roster, when the route fails', async () => {
    vi.spyOn(apiClient, 'get').mockRejectedValue(httpError(503, { success: false, error: { code: 'X', message: 'down', details: { retryable: true } } }));
    await expect(fetchMemberParticipants(7, ROOM)).rejects.toMatchObject({ kind: 'unavailable', retryable: true });
  });
});

describe('ice_servers in the join response (optional; TURN is v3.3.x)', () => {
  const joinWire = { success: true, token: 't', url: 'u', whip_url: 'w', whep_url: 'e', token_id: 'k', participant_id: PERSON, expires_at: 1 };

  it('defaults to none when absent or null', async () => {
    vi.spyOn(apiClient, 'post').mockResolvedValue(ok(joinWire));
    await expect(joinRoom(7, ROOM)).resolves.toMatchObject({ iceServers: [] });
    expect(parseIceServers(null)).toEqual([]);
  });

  it('parses STUN/TURN entries with credentials', async () => {
    vi.spyOn(apiClient, 'post').mockResolvedValue(
      ok({ ...joinWire, ice_servers: [{ urls: 'stun:stun.example:3478' }, { urls: ['turn:turn.example:3478'], username: 'u', credential: 'c' }] }),
    );
    const result = await joinRoom(7, ROOM);
    expect(result.iceServers).toEqual([{ urls: 'stun:stun.example:3478' }, { urls: ['turn:turn.example:3478'], username: 'u', credential: 'c' }]);
  });

  it('rejects malformed ice_servers loudly rather than silently dropping a TURN server', () => {
    expect(() => parseIceServers('stun:x')).toThrow('not a list');
    expect(() => parseIceServers([{ username: 'u' }])).toThrow('"urls"');
    expect(() => parseIceServers([{ urls: [] }])).toThrow('"urls"');
    expect(() => parseIceServers([{ urls: [1] }])).toThrow('"urls"');
  });
});
