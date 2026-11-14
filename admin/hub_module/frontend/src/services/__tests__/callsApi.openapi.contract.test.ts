/**
 * Calls client vs `openapi/v1.yaml`: every client function is run against a recording adapter, the request it
 * emits must resolve to a documented operation (path, method, request-body keys), and its parser must accept
 * a response generated FROM the spec (all properties, and required-only). A client that calls a route the spec
 * lacks, sends an undeclared body key, or reads a field the spec does not guarantee fails here. The reverse is
 * checked too: every calls/voice operation in the spec must be covered by a client function.
 */
import { afterAll, describe, expect, it } from 'vitest';
import { apiClient } from '../../lib/apiClient';
import { recordRequests } from '../../test/apiContract';
import {
  checkRequestBody,
  findOperation,
  loadSpec,
  responseSchema,
  sample,
  successStatus,
} from '../../test/openapiContract';
import {
  acknowledgeHand,
  createRoom,
  deleteRoom,
  ensureTownHall,
  fetchMemberParticipants,
  fetchMyHand,
  fetchParticipants,
  fetchTownHall,
  fetchRaisedHands,
  fetchRoom,
  fetchRooms,
  joinRoom,
  kickParticipant,
  leaveRoom,
  lowerHand,
  muteAll,
  parseRoom,
  raiseHand,
  resetHands,
  setParticipantMuted,
  setRoomLocked,
} from '../callsApi';

const spec = loadSpec();
const rec = recordRequests(apiClient);
afterAll(rec.restore);

const COMMUNITY = 7;
const ROOM = '3f2b8c1e-9a4d-4e57-8b21-6c0d5e7f1a90';
const PERSON = '7a1d2c3b-5e6f-4a80-9b1c-2d3e4f5a6b7c';

const ADMIN = '/api/v1/admin/{community_id}/calls/rooms';
const MEMBER = '/api/v1/community/{community_id}/interact/voice/rooms';

interface Row {
  name: string;
  method: string;
  template: string;
  call: () => Promise<unknown>;
}

const ROWS: readonly Row[] = [
  { name: 'fetchRooms(admin)', method: 'GET', template: ADMIN, call: () => fetchRooms('admin', COMMUNITY) },
  { name: 'createRoom(admin)', method: 'POST', template: ADMIN, call: () => createRoom('admin', COMMUNITY, { roomName: 'Town hall', maxParticipants: 50 }) },
  { name: 'fetchRoom', method: 'GET', template: `${ADMIN}/{room_name}`, call: () => fetchRoom(COMMUNITY, ROOM) },
  { name: 'deleteRoom', method: 'DELETE', template: `${ADMIN}/{room_name}`, call: () => deleteRoom(COMMUNITY, ROOM) },
  { name: 'setRoomLocked(lock)', method: 'POST', template: `${ADMIN}/{room_name}/lock`, call: () => setRoomLocked(COMMUNITY, ROOM, true) },
  { name: 'setRoomLocked(unlock)', method: 'POST', template: `${ADMIN}/{room_name}/unlock`, call: () => setRoomLocked(COMMUNITY, ROOM, false) },
  { name: 'fetchParticipants', method: 'GET', template: `${ADMIN}/{room_name}/participants`, call: () => fetchParticipants(COMMUNITY, ROOM) },
  { name: 'kickParticipant', method: 'POST', template: `${ADMIN}/{room_name}/kick`, call: () => kickParticipant(COMMUNITY, ROOM, PERSON) },
  { name: 'setParticipantMuted(mute)', method: 'POST', template: `${ADMIN}/{room_name}/mute`, call: () => setParticipantMuted(COMMUNITY, ROOM, PERSON, true) },
  { name: 'setParticipantMuted(unmute)', method: 'POST', template: `${ADMIN}/{room_name}/unmute`, call: () => setParticipantMuted(COMMUNITY, ROOM, PERSON, false) },
  { name: 'muteAll', method: 'POST', template: `${ADMIN}/{room_name}/mute-all`, call: () => muteAll(COMMUNITY, ROOM) },
  { name: 'fetchRaisedHands', method: 'GET', template: `${ADMIN}/{room_name}/raised-hands`, call: () => fetchRaisedHands(COMMUNITY, ROOM) },
  { name: 'acknowledgeHand', method: 'POST', template: `${ADMIN}/{room_name}/acknowledge-hand`, call: () => acknowledgeHand(COMMUNITY, ROOM, PERSON) },
  { name: 'resetHands', method: 'POST', template: `${ADMIN}/{room_name}/reset-hands`, call: () => resetHands(COMMUNITY, ROOM) },
  { name: 'fetchRooms(member)', method: 'GET', template: MEMBER, call: () => fetchRooms('member', COMMUNITY) },
  { name: 'createRoom(member)', method: 'POST', template: MEMBER, call: () => createRoom('member', COMMUNITY, { roomName: 'Hangout' }) },
  { name: 'joinRoom', method: 'POST', template: `${MEMBER}/{room_name}/join`, call: () => joinRoom(COMMUNITY, ROOM) },
  { name: 'leaveRoom', method: 'POST', template: `${MEMBER}/{room_name}/leave`, call: () => leaveRoom(COMMUNITY, ROOM) },
  { name: 'raiseHand', method: 'POST', template: `${MEMBER}/{room_name}/raise-hand`, call: () => raiseHand(COMMUNITY, ROOM) },
  { name: 'lowerHand', method: 'POST', template: `${MEMBER}/{room_name}/lower-hand`, call: () => lowerHand(COMMUNITY, ROOM) },
];

/**
 * PROVISIONAL rows: routes agreed with the hub-api owner but not yet published in the spec (T4 follow-up). Each is
 * validated against the spec the moment it appears there (full conformance, same as ROWS); until then it is reported
 * as provisional and covered by `callsApi.provisional.test.ts`. Promote a row to ROWS once it is in the spec.
 */
const PROVISIONAL_ROWS: readonly Row[] = [
  { name: 'fetchTownHall', method: 'GET', template: '/api/v1/communities/{community_id}/town-hall', call: () => fetchTownHall(COMMUNITY) },
  { name: 'ensureTownHall', method: 'POST', template: '/api/v1/communities/{community_id}/town-hall', call: () => ensureTownHall(COMMUNITY) },
  { name: 'fetchMyHand', method: 'GET', template: `${MEMBER}/{room_name}/hands/mine`, call: () => fetchMyHand(COMMUNITY, ROOM) },
  { name: 'fetchMemberParticipants', method: 'GET', template: `${MEMBER}/{room_name}/participants`, call: () => fetchMemberParticipants(COMMUNITY, ROOM) },
];

const inSpec = (row: Row): boolean => spec.paths[row.template]?.[row.method.toLowerCase()] !== undefined;

/** Runs `call` once and returns the single request it emitted (a parse failure on the stub reply is ignored). */
async function capture(call: () => Promise<unknown>) {
  rec.calls.length = 0;
  rec.reply.data = { success: true };
  await call().catch(() => undefined);
  expect(rec.calls).toHaveLength(1);
  const sent = rec.calls[0];
  if (sent === undefined) throw new Error('no request captured');
  return sent;
}

describe('calls client conforms to openapi/v1.yaml', () => {
  it('examined a non-trivial spec and client surface (zero is a failure)', () => {
    expect(Object.keys(spec.paths).length).toBeGreaterThan(100);
    expect(ROWS.length).toBeGreaterThanOrEqual(20);
  });

  describe.each(ROWS)('$name', (row) => {
    it(`sends ${row.method} ${row.template} with a body the spec declares`, async () => {
      const sent = await capture(row.call);
      const found = findOperation(spec, sent.method ?? '', sent.url ?? '');
      expect(found, `${sent.method} ${sent.url} is not in openapi/v1.yaml`).not.toBeNull();
      expect(found?.method).toBe(row.method);
      expect(found?.template).toBe(row.template);
      const body = typeof sent.data === 'string' ? (JSON.parse(sent.data) as unknown) : sent.data;
      expect(checkRequestBody(spec, found?.operation ?? {}, body)).toEqual([]);
    });

    it.each([true, false])('parses a spec-generated response (all properties: %s)', async (all) => {
      const sent = await capture(row.call);
      const found = findOperation(spec, sent.method ?? '', sent.url ?? '');
      if (found === null) throw new Error('operation missing from spec');
      const status = successStatus(found.operation);
      rec.reply.data = sample(spec, responseSchema(spec, found.operation, status), all);
      await expect(row.call()).resolves.not.toThrow();
    });
  });

  describe.each(PROVISIONAL_ROWS)('PROVISIONAL $name', (row) => {
    it.runIf(inSpec(row))('is in the spec now: sends a documented request and parses a spec-generated response', async () => {
      const sent = await capture(row.call);
      const found = findOperation(spec, sent.method ?? '', sent.url ?? '');
      expect(found?.template).toBe(row.template);
      const body = typeof sent.data === 'string' ? (JSON.parse(sent.data) as unknown) : sent.data;
      expect(checkRequestBody(spec, found?.operation ?? {}, body)).toEqual([]);
      for (const all of [true, false]) {
        rec.reply.data = sample(spec, responseSchema(spec, found?.operation ?? {}, successStatus(found?.operation ?? {})), all);
        await expect(row.call()).resolves.not.toThrow();
      }
    });

    it.skipIf(inSpec(row))('is not in the spec yet: shape pinned by callsApi.provisional.test.ts (promote this row when T4 lands)', () => {
      expect(inSpec(row)).toBe(false);
    });
  });

  it('covers every calls/voice operation the spec documents, and nothing the spec lacks', () => {
    const documented = new Set<string>();
    for (const [template, item] of Object.entries(spec.paths)) {
      if (!template.includes('/calls/rooms') && !template.includes('/interact/voice/rooms')) continue;
      for (const method of Object.keys(item)) {
        if (['get', 'post', 'put', 'patch', 'delete'].includes(method)) documented.add(`${method.toUpperCase()} ${template}`);
      }
    }
    // Provisional rows that have reached the spec count as covered (their conformance test above runs for real).
    const covered = new Set([...ROWS, ...PROVISIONAL_ROWS.filter(inSpec)].map((row) => `${row.method} ${row.template}`));
    expect(documented.size).toBeGreaterThanOrEqual(20);
    expect([...documented].filter((op) => !covered.has(op))).toEqual([]);
    expect([...covered].filter((op) => !documented.has(op) && !op.includes('/town-hall'))).toEqual([]);
  });
});

describe('the contract checks can fail', () => {
  it('rejects a path the spec does not document', () => {
    expect(findOperation(spec, 'GET', '/api/v1/admin/7/calls/lobbies')).toBeNull();
  });

  it('rejects a method the spec does not document on a real path', () => {
    expect(findOperation(spec, 'PATCH', '/api/v1/admin/7/calls/rooms')).toBeNull();
  });

  it('rejects the legacy create body ({name}) and a missing required key', () => {
    const found = findOperation(spec, 'POST', '/api/v1/community/7/interact/voice/rooms');
    expect(found).not.toBeNull();
    expect(checkRequestBody(spec, found?.operation ?? {}, { name: 'x' })).toEqual([
      'body key "name" is not in the spec',
      'required body key "room_name" was not sent',
    ]);
  });

  it('rejects a body on an operation that declares none', () => {
    const found = findOperation(spec, 'POST', `/api/v1/admin/7/calls/rooms/${ROOM}/lock`);
    expect(checkRequestBody(spec, found?.operation ?? {}, { x: 1 })).toEqual(['client sent a body but the spec declares none']);
  });

  it('rejects a response in the legacy (name-keyed) shape', () => {
    expect(() => parseRoom({ name: 'room', participantCount: 1 })).toThrow();
  });
});
